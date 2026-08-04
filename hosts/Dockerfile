# alpine-srv6-scapy:1.0
#
# Lab host image. Provides:
#   - kernel SRv6 utilities (from upstream alpine-srv6 base)
#   - iperf3 (throughput/latency benchmarking between hosts)
#   - Python 3 + scapy (for basic IP/UDP and SRv6/uSID packet gen)
#   - the srv6_mrc Python package, pip-installed so its console
#     scripts (`spray`, `routes`, `run-scenario`) land in /usr/local/bin
#
# Build (from repo root — context MUST be repo root, not host-image/):
#
#   docker build -f host-image/Dockerfile -t alpine-srv6-scapy:1.0 .
#

FROM iejalapeno/alpine-srv6:1.0

RUN apk add --no-cache \
        python3 \
        py3-pip \
        tcpdump \
        iperf3

# Install srv6_mrc and its runtime deps (scapy, PyYAML).
# Copy the minimal set: pyproject + package source + a README stub
# (referenced by [project].readme in pyproject.toml).
WORKDIR /tmp/srv6_mrc_build
COPY pyproject.toml README.md ./
COPY srv6_mrc ./srv6_mrc

RUN pip3 install --no-cache-dir --break-system-packages \
        '.[runtime]' \
 && cd / && rm -rf /tmp/srv6_mrc_build

# Point the package at the (containerlab-bind-mounted) topo.yaml.
RUN mkdir -p /etc/srv6_mrc
ENV SRV6_TOPO=/etc/srv6_mrc/topo.yaml

# Sanity: surface versions and confirm CLI entry points landed on PATH.
# We intentionally do NOT import srv6_mrc.topo here — there's no
# topo.yaml inside the image at build time; that file shows up at
# container runtime via the bind mount.
RUN python3 -c "import scapy; print('scapy', scapy.__version__)" \
 && which spray routes run-scenario
