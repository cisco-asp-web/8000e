# Traffic generation — scale-across lab

`scripts/udp_trafgen.py` sprays loopback-to-loopback UDP across the SRv6
fabric. It runs inside the `alpine-srv6-scapy` host containers, which
bind-mount `scripts/` at `/scripts` (see `topology.clab.yaml`).

## Auto-start on deploy

Each host's `exec:` block launches the generator in the background as its
last step, logging to `/var/log/trafgen.log`:

| host       | role      | flow                        | profile   |
|------------|-----------|-----------------------------|-----------|
| `dc01-trn` | training  | a000 → b000 (8×8 mesh)      | training  |
| `dc02-trn` | training  | b000 → a000 (8×8 mesh)      | training  |
| `dc01-inf` | inference | c000 → d000 (paired ::i→::i) | inference |
| `dc02-inf` | inference | d000 → c000 (paired ::i→::i) | inference |

Traffic starts as soon as the container is up; packets drop until the
fabric (BGP / SRv6 policies) converges, then flow — no action needed.

## Profiles

| profile   | payload | per-flow pps | dport | aggregate (8×8) |
|-----------|---------|--------------|-------|-----------------|
| training  | 1200 B  | 20           | 5001  | 8×8 mesh → ~1280 pps, ~13 Mbps |
| inference | 128 B   | 5            | 5002  | 8 paired flows → ~40 pps, ~0.06 Mbps |

Any preset can be overridden: `--size`, `--pps`, `--dport`, `--count`,
`--pattern {mesh,paired}`, `--duration`. Distinct UDP source ports per
flow give the fabric ECMP hashing entropy.

## Operating it by hand

```sh
# watch a running generator
docker exec -it dc01-trn tail -f /var/log/trafgen.log

# stop it
docker exec dc01-trn pkill -f udp_trafgen.py

# restart / re-tune (e.g. hotter training run, 60s burst)
docker exec dc01-trn python3 /scripts/udp_trafgen.py \
    --profile training --src-prefix 2001:db8:a000:: --dst-prefix 2001:db8:b000:: \
    --pps 100 --size 1400 --duration 60

# confirm it's landing at the far end
docker exec -it dc02-trn tcpdump -ni any udp port 5001
```
