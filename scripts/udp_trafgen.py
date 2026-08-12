#!/usr/bin/env python3
"""
udp_trafgen.py -- loopback-to-loopback UDP traffic generator

Used by the scale-across lab hosts (alpine-srv6-scapy image) to emulate
east-west flows across the SRv6 fabric. Sources are the host's own loopbacks;
destinations are arbitrary addresses inside the peer's /48 and need no
listener, because nothing is expected to answer. That asymmetry is what lets a
handful of local loopbacks fan out to hundreds of destinations, handing the
fabric a wide 5-tuple space to hash across the ECMP members.

Two profiles set sensible defaults:

  training   large payloads, bursty on/off cycles (emulates collectives)
  inference  small payloads, steady rate          (emulates request traffic)

The bursty-versus-steady contrast is deliberate: it makes the two traffic
classes trivial to tell apart on a throughput graph.

Everything a profile sets can be overridden on the command line.

Examples
--------
  # training host a000 -> peer b000 (4 sources x 256 destinations, bursty)
  udp_trafgen.py --profile training  --src-prefix 2001:db8:a000:: --dst-prefix 2001:db8:b000::

  # inference host c000 -> peer d000 (steady)
  udp_trafgen.py --profile inference --src-prefix 2001:db8:c000:: --dst-prefix 2001:db8:d000::

  # training rate, but continuous instead of bursty
  udp_trafgen.py --profile training --burst "" --src-prefix ... --dst-prefix ...

Source addresses (::1 .. ::src-count) MUST already be assigned locally -- the
containerlab exec block adds them to lo / lo2 .. lo8. Destinations must not be.
One UDP socket is bound per source address and the destination varies per
send, so a 4x256 fan-out costs 4 file descriptors rather than 1024.

Note: scapy's L3 socket is deliberately NOT used here. Scapy resolves the
outbound route itself, against a table it parses in userspace, and that lookup
does not understand multipath (ECMP) routes -- it reports "No route found for
IPv6 destination" for destinations the kernel routes perfectly well.
"""

import argparse
import ipaddress
import signal
import socket
import sys
import time

# Per-profile defaults. `pps` is the AGGREGATE rate across every flow, not a
# per-flow rate: with ~1000 flows a per-flow figure is impossible to reason
# about (20 pps each would be 20k pps and ~200 Mbps).
PROFILES = {
    "training": {
        "size": 1200, "pps": 1280, "dport": 5001,
        "src_count": 4, "dst_count": 256, "pattern": "mesh",
        "burst": "60,5,45,5",
    },
    "inference": {
        "size": 128, "pps": 160, "dport": 5002,
        "src_count": 4, "dst_count": 256, "pattern": "mesh",
        "burst": "",
    },
}

# Scheduling granularity for the send loop. Small enough that bursts start and
# stop crisply, large enough to batch sends instead of sleeping per packet.
TICK = 0.005

_running = True


def _stop(signum, _frame):
    global _running
    _running = False


def parse_args():
    p = argparse.ArgumentParser(
        description="loopback-to-loopback UDP traffic generator"
    )
    p.add_argument("--profile", choices=PROFILES, required=True,
                   help="preset defaults for size / rate / fan-out / bursts")
    p.add_argument("--src-prefix", required=True,
                   help="base of local loopback /48, e.g. 2001:db8:a000::")
    p.add_argument("--dst-prefix", required=True,
                   help="base of remote /48, e.g. 2001:db8:b000::")
    p.add_argument("--src-count", type=int,
                   help="local loopbacks used, ::1 .. ::src-count "
                        "(must be assigned locally)")
    p.add_argument("--dst-count", type=int,
                   help="remote destinations, ::1 .. ::dst-count "
                        "(no listener required)")
    p.add_argument("--pattern", choices=("mesh", "paired"),
                   help="mesh = every src to every dst; paired = ::i -> ::i")
    p.add_argument("--size", type=int,
                   help="UDP payload bytes (overrides profile)")
    p.add_argument("--pps", type=float,
                   help="AGGREGATE packets per second across all flows "
                        "(overrides profile)")
    p.add_argument("--dport", type=int,
                   help="UDP destination port (overrides profile)")
    p.add_argument("--sport-base", type=int, default=20000,
                   help="first UDP source port; one port per source address "
                        "(default 20000)")
    p.add_argument("--burst",
                   help="on/off seconds, alternating and repeating, e.g. "
                        "'60,5,45,5'. Empty string = run continuously.")
    p.add_argument("--duration", type=float, default=0,
                   help="seconds to run, 0 = run forever (default 0)")
    return p.parse_args()


def parse_burst(spec):
    """'60,5,45,5' -> [(True, 60.0), (False, 5.0), (True, 45.0), (False, 5.0)].

    Durations alternate on/off starting with on, and the whole sequence
    repeats. An empty spec means send continuously.
    """
    spec = (spec or "").strip()
    if not spec:
        return []
    segments = [seg.strip() for seg in spec.split(",") if seg.strip()]
    schedule = []
    for i, part in enumerate(segments):
        try:
            secs = float(part)
        except ValueError:
            raise SystemExit(f"[trafgen] bad --burst segment {part!r}: "
                             f"expected a number of seconds")
        if secs <= 0:
            raise SystemExit(f"[trafgen] --burst segment {part!r} must be > 0")
        schedule.append((i % 2 == 0, secs))
    return schedule


def describe_burst(schedule):
    parts = " / ".join(f"{secs:g}s {'on' if on else 'off'}"
                       for on, secs in schedule)
    on_time = sum(secs for on, secs in schedule if on)
    total = sum(secs for _on, secs in schedule)
    return f"{parts}  (duty {on_time / total * 100:.0f}%, cycle {total:g}s)"


def build_flows(src_prefix, dst_prefix, src_count, dst_count, pattern):
    """Return (sources, [(src, dst), ...]) for the chosen pattern."""
    src_base = ipaddress.IPv6Address(src_prefix)
    dst_base = ipaddress.IPv6Address(dst_prefix)
    srcs = [str(src_base + i) for i in range(1, src_count + 1)]
    dsts = [str(dst_base + i) for i in range(1, dst_count + 1)]
    if pattern == "paired":
        n = min(len(srcs), len(dsts))
        return srcs[:n], list(zip(srcs[:n], dsts[:n]))
    return srcs, [(s, d) for s in srcs for d in dsts]


def open_flow_socket(src, sport):
    """UDP socket bound to one source address/port; the kernel routes it.

    Left unconnected on purpose: the peers run no listener, so a connected
    socket would surface their ICMPv6 port-unreachable as ECONNREFUSED on the
    next send and stop the generator. Unconnected sockets ignore ICMP errors.
    """
    sk = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        sk.bind((src, sport))
    except OSError as exc:
        sk.close()
        raise SystemExit(
            f"[trafgen] cannot bind {src}:{sport} ({exc}); "
            f"is the loopback assigned? check `ip -6 addr show`"
        )
    return sk


def check_route(dst, dport):
    """Fail fast if the kernel has no route; connect() only does the lookup."""
    probe = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        probe.connect((dst, dport))
    except OSError as exc:
        raise SystemExit(
            f"[trafgen] no route to {dst} ({exc}); check `ip -6 route get {dst}`"
        )
    finally:
        probe.close()


def sample_destinations(dsts, limit=4):
    """A few evenly spread destinations -- enough to prove the route covers
    the whole range without probing hundreds of addresses at startup."""
    if len(dsts) <= limit:
        return list(dsts)
    step = (len(dsts) - 1) / (limit - 1)
    return [dsts[round(i * step)] for i in range(limit)]


def main():
    args = parse_args()
    prof = PROFILES[args.profile]

    def pick(name):
        value = getattr(args, name)
        return prof[name] if value is None else value

    size = pick("size")
    pps = pick("pps")
    dport = pick("dport")
    pattern = pick("pattern")
    src_count = pick("src_count")
    dst_count = pick("dst_count")
    schedule = parse_burst(prof["burst"] if args.burst is None else args.burst)

    if pps <= 0:
        raise SystemExit("[trafgen] --pps must be > 0")

    srcs, pairs = build_flows(args.src_prefix, args.dst_prefix,
                              src_count, dst_count, pattern)
    if not pairs:
        raise SystemExit("[trafgen] no flows to send; check --src-count/--dst-count")

    payload = b"\xa5" * size
    for dst in sample_destinations(sorted({d for _s, d in pairs})):
        check_route(dst, dport)

    socks = {s: open_flow_socket(s, args.sport_base + i)
             for i, s in enumerate(srcs)}
    flows = [(socks[s], (d, dport)) for (s, d) in pairs]

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    mbps = pps * (size + 48) * 8 / 1e6
    print(f"[trafgen] profile={args.profile} pattern={pattern} "
          f"flows={len(flows)} ({len(srcs)} src x {len(pairs) // len(srcs)} dst) "
          f"size={size}B dport={dport} duration={args.duration or 'inf'}",
          flush=True)
    print(f"[trafgen] {args.src_prefix}[1..{src_count}] -> "
          f"{args.dst_prefix}[1..{dst_count}]  "
          f"({pps:g} pps aggregate, ~{mbps:.2f} Mbps while on, "
          f"{pps / len(flows):.2f} pps per flow)", flush=True)
    print(f"[trafgen] burst: {describe_burst(schedule) if schedule else 'continuous'}",
          flush=True)

    start = time.monotonic()
    deadline = start + args.duration if args.duration > 0 else None
    sent = 0
    errors = 0
    last_report = start
    credit = 0.0
    idx = 0
    phase = 0
    phase_until = start + schedule[0][1] if schedule else None
    last_tick = start

    while _running:
        now = time.monotonic()
        # Accrue against measured time, not the nominal tick: time.sleep()
        # routinely overshoots, and accruing nominally undershoots the rate.
        # Clamped so a scheduling stall cannot release one huge burst.
        delta = min(now - last_tick, 0.5)
        last_tick = now

        if schedule and now >= phase_until:
            phase = (phase + 1) % len(schedule)
            phase_until = now + schedule[phase][1]
            print(f"[trafgen] t={now - start:6.0f}s burst -> "
                  f"{'on' if schedule[phase][0] else 'off'}", flush=True)

        sending = schedule[phase][0] if schedule else True
        if sending:
            # Fractional credit carries across ticks so the aggregate rate
            # holds even when pps * delta is not a whole number of packets.
            credit += pps * delta
            burst_count = int(credit)
            credit -= burst_count
            for _ in range(burst_count):
                sk, peer = flows[idx]
                idx += 1
                if idx == len(flows):
                    idx = 0
                try:
                    sk.sendto(payload, peer)
                except OSError:
                    errors += 1
                else:
                    sent += 1
        else:
            credit = 0.0

        if now - last_report >= 10:
            print(f"[trafgen] t={now - start:6.0f}s sent={sent} "
                  f"avg={sent / (now - start):.0f} pps errors={errors}",
                  flush=True)
            last_report = now

        if deadline and now >= deadline:
            break

        elapsed = time.monotonic() - now
        if elapsed < TICK:
            time.sleep(TICK - elapsed)

    for sk in socks.values():
        sk.close()
    print(f"[trafgen] stopped, total sent={sent} errors={errors}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # keep the container log useful on crash
        print(f"[trafgen] fatal: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
