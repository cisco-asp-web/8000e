# Traffic generation — scale-across lab

`scripts/udp_trafgen.py` sprays loopback-to-loopback UDP across the SRv6
fabric. It runs inside the `alpine-srv6-scapy` host containers, which
bind-mount `scripts/` at `/scripts` (see `topology.clab.yaml`).

## Auto-start on deploy

Each host's `exec:` block launches the generator in the background as its
last step, logging to `/var/log/trafgen.log`:

| host       | role      | flow                   | profile   |
|------------|-----------|------------------------|-----------|
| `dc01-trn` | training  | a000 → b000 (4×256)    | training  |
| `dc02-trn` | training  | b000 → a000 (4×256)    | training  |
| `dc01-inf` | inference | c000 → d000 (4×256)    | inference |
| `dc02-inf` | inference | d000 → c000 (4×256)    | inference |

Traffic starts as soon as the container is up; packets drop until the
fabric (BGP / SRv6 policies) converges, then flow — no action needed.

## Profiles

| profile   | payload | aggregate pps | dport | shape | flows |
|-----------|---------|---------------|-------|-------|-------|
| training  | 1200 B  | 1280 (~13 Mbps while on) | 5001 | bursty `60,5,45,5` | 4×256 = 1024 |
| inference | 128 B   | 160 (~0.23 Mbps)         | 5002 | continuous         | 4×256 = 1024 |

`--pps` is the **aggregate** rate across all flows, not per flow; the
generator rotates through the flow list one packet at a time. Training's
on/off cycle is what makes it visually distinct from inference on a
throughput graph.

Only the 4 source addresses have to exist locally. Destinations are never
answered, so any address inside the peer's `/48` is fair game — that is what
buys 256 destinations for free. One socket is bound per source address and
the destination varies per send, so 1024 flows cost 4 file descriptors.

Any preset can be overridden: `--size`, `--pps`, `--dport`, `--src-count`,
`--dst-count`, `--pattern {mesh,paired}`, `--burst`, `--duration`. Pass
`--burst ""` to run a profile continuously.

## Operating it by hand

```sh
# watch a running generator
docker exec -it dc01-trn tail -f /var/log/trafgen.log

# stop it
docker exec dc01-trn pkill -f udp_trafgen.py

# restart / re-tune (e.g. hotter training run, steady, for 60s)
docker exec dc01-trn python3 /scripts/udp_trafgen.py \
    --profile training --src-prefix 2001:db8:a000:: --dst-prefix 2001:db8:b000:: \
    --pps 5000 --size 1400 --burst "" --duration 60

# widen the flow space further (4 sources × 1024 destinations)
docker exec dc01-trn python3 /scripts/udp_trafgen.py \
    --profile training --src-prefix 2001:db8:a000:: --dst-prefix 2001:db8:b000:: \
    --dst-count 1024 --duration 60

# confirm it's landing at the far end
docker exec -it dc02-trn tcpdump -ni any udp port 5001
```
