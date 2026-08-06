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
(the containerlab exec block adds them to lo / lo2 .. lo8). Sending is
done via a raw IPv6 L3 socket, so the kernel routing table picks the
outbound interface / nexthop -- ECMP across the fabric uplinks is handled
by the kernel + fabric, we just supply the flow entropy.
"""

import argparse
import ipaddress
import signal
import sys
import time

from scapy.all import IPv6, UDP, Raw, conf

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


def main():
    args = parse_args()
    size = args.size if args.size is not None else PROFILES[args.profile]["size"]
    pps = args.pps if args.pps is not None else PROFILES[args.profile]["pps"]
    dport = args.dport if args.dport is not None else PROFILES[args.profile]["dport"]

    flows = build_flows(args)
    payload = Raw(load=b"\xa5" * size)
    pkts = [IPv6(src=s, dst=d) / UDP(sport=sp, dport=dport) / payload
            for (s, d, sp) in flows]

    round_interval = 1.0 / pps if pps > 0 else 0.0
    sock = conf.L3socket6()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"[trafgen] profile={args.profile} pattern={args.pattern} "
          f"flows={len(pkts)} size={size}B per_flow_pps={pps} "
          f"dport={dport} duration={args.duration or 'inf'}",
          flush=True)
    print(f"[trafgen] {args.src_prefix}[1..{args.count}] -> "
          f"{args.dst_prefix}[1..{args.count}]  "
          f"(~{len(pkts) * pps:.0f} pps aggregate, "
          f"~{len(pkts) * pps * (size + 48) * 8 / 1e6:.2f} Mbps)",
          flush=True)

    start = time.monotonic()
    deadline = start + args.duration if args.duration > 0 else None
    sent = 0
    last_report = start

    while _running:
        t0 = time.monotonic()
        for pkt in pkts:
            sock.send(pkt)
        sent += len(pkts)

        now = time.monotonic()
        if now - last_report >= 10:
            print(f"[trafgen] t={now - start:6.0f}s sent={sent} "
                  f"rate={sent / (now - start):.0f} pps", flush=True)
            last_report = now

        if deadline and now >= deadline:
            break

        dt = now - t0
        if round_interval > dt:
            time.sleep(round_interval - dt)

    sock.close()
    print(f"[trafgen] stopped, total sent={sent}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # keep the container log useful on crash
        print(f"[trafgen] fatal: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
