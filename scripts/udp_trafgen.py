#!/usr/bin/env python3
"""
udp_trafgen.py -- loopback-to-loopback UDP traffic generator (scapy)

Used by the scale-across lab hosts (alpine-srv6-scapy image) to emulate
east-west flows across the SRv6 fabric. Each host owns 8 loopbacks
(::1 .. ::8 out of a /48) and sprays UDP from every local loopback to
every remote loopback -- i.e. a full 8x8 mesh -- so the fabric sees
64 distinct 5-tuples to hash across the ECMP links.

Two profiles set sensible defaults:

  training   large-ish payloads, higher rate  (emulates all-to-all collective)
  inference  small payloads, lower rate        (emulates lighter request traffic)

Everything a profile sets can be overridden on the command line.

Examples
--------
  # training host a000 -> peer b000 (full mesh, defaults for the profile)
  udp_trafgen.py --profile training  --src-prefix 2001:db8:a000:: --dst-prefix 2001:db8:b000::

  # inference host c000 -> peer d000
  udp_trafgen.py --profile inference --src-prefix 2001:db8:c000:: --dst-prefix 2001:db8:d000::

The source addresses (::1 .. ::count) MUST already be assigned locally
(the containerlab exec block adds them to lo / lo2 .. lo8). Each flow gets
an ordinary UDP socket bound to its source address and port, so the kernel
routing table picks the outbound interface / nexthop -- ECMP across the
fabric uplinks is handled by the kernel + fabric, we just supply the flow
entropy.

Note: scapy's L3 socket is deliberately NOT used here. Scapy resolves the
outbound route itself, against a table it parses in userspace, and that
lookup does not understand multipath (ECMP) routes -- it reports "No route
found for IPv6 destination" for destinations the kernel routes fine.
"""

import argparse
import ipaddress
import signal
import socket
import sys
import time

# Per-profile defaults: (payload_bytes, per_flow_pps, udp_dport)
PROFILES = {
    "training":  {"size": 1200, "pps": 20, "dport": 5001},
    "inference": {"size": 128,  "pps": 5,  "dport": 5002},
}

_running = True


def _stop(signum, _frame):
    global _running
    _running = False


def parse_args():
    p = argparse.ArgumentParser(
        description="loopback-to-loopback UDP mesh traffic generator (scapy)"
    )
    p.add_argument("--profile", choices=PROFILES, required=True,
                   help="preset defaults for payload size / rate / dport")
    p.add_argument("--src-prefix", required=True,
                   help="base of local loopback /48, e.g. 2001:db8:a000::")
    p.add_argument("--dst-prefix", required=True,
                   help="base of remote loopback /48, e.g. 2001:db8:b000::")
    p.add_argument("--count", type=int, default=8,
                   help="loopbacks per host, addresses ::1 .. ::count (default 8)")
    p.add_argument("--pattern", choices=("mesh", "paired"), default="mesh",
                   help="mesh = every src to every dst (default); "
                        "paired = ::i -> ::i only")
    p.add_argument("--size", type=int,
                   help="UDP payload bytes (overrides profile)")
    p.add_argument("--pps", type=float,
                   help="packets per second, per flow (overrides profile)")
    p.add_argument("--dport", type=int,
                   help="UDP destination port (overrides profile)")
    p.add_argument("--sport-base", type=int, default=20000,
                   help="first UDP source port; each flow gets a distinct "
                        "sport for ECMP entropy (default 20000)")
    p.add_argument("--duration", type=float, default=0,
                   help="seconds to run, 0 = run forever (default 0)")
    return p.parse_args()


def build_flows(args):
    """Return a list of (src, dst, sport) tuples for the chosen pattern."""
    src_base = ipaddress.IPv6Address(args.src_prefix)
    dst_base = ipaddress.IPv6Address(args.dst_prefix)
    srcs = [str(src_base + i) for i in range(1, args.count + 1)]
    dsts = [str(dst_base + i) for i in range(1, args.count + 1)]

    pairs = []
    if args.pattern == "mesh":
        for s in srcs:
            for d in dsts:
                pairs.append((s, d))
    else:  # paired
        pairs = list(zip(srcs, dsts))

    # Distinct source port per flow so the fabric hashes them onto
    # different ECMP members.
    return [(s, d, args.sport_base + i) for i, (s, d) in enumerate(pairs)]


def open_flow_socket(src, sport):
    """UDP socket pinned to one source 5-tuple half; the kernel routes it.

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


def main():
    args = parse_args()
    size = args.size if args.size is not None else PROFILES[args.profile]["size"]
    pps = args.pps if args.pps is not None else PROFILES[args.profile]["pps"]
    dport = args.dport if args.dport is not None else PROFILES[args.profile]["dport"]

    flows = build_flows(args)
    payload = b"\xa5" * size
    for dst in sorted({d for (_s, d, _sp) in flows}):
        check_route(dst, dport)
    socks = [(open_flow_socket(s, sp), (d, dport)) for (s, d, sp) in flows]

    round_interval = 1.0 / pps if pps > 0 else 0.0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"[trafgen] profile={args.profile} pattern={args.pattern} "
          f"flows={len(socks)} size={size}B per_flow_pps={pps} "
          f"dport={dport} duration={args.duration or 'inf'}",
          flush=True)
    print(f"[trafgen] {args.src_prefix}[1..{args.count}] -> "
          f"{args.dst_prefix}[1..{args.count}]  "
          f"(~{len(socks) * pps:.0f} pps aggregate, "
          f"~{len(socks) * pps * (size + 48) * 8 / 1e6:.2f} Mbps)",
          flush=True)

    start = time.monotonic()
    deadline = start + args.duration if args.duration > 0 else None
    sent = 0
    errors = 0
    last_report = start

    while _running:
        t0 = time.monotonic()
        for sk, peer in socks:
            try:
                sk.sendto(payload, peer)
            except OSError:
                errors += 1
            else:
                sent += 1

        now = time.monotonic()
        if now - last_report >= 10:
            print(f"[trafgen] t={now - start:6.0f}s sent={sent} "
                  f"rate={sent / (now - start):.0f} pps errors={errors}",
                  flush=True)
            last_report = now

        if deadline and now >= deadline:
            break

        dt = now - t0
        if round_interval > dt:
            time.sleep(round_interval - dt)

    for sk, _peer in socks:
        sk.close()
    print(f"[trafgen] stopped, total sent={sent} errors={errors}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # keep the container log useful on crash
        print(f"[trafgen] fatal: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
