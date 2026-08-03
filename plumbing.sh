#!/bin/bash
set -euo pipefail

CEOS_IMAGE="ceosimage:4.35.2F"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "=== DRY RUN — no changes will be made ==="
fi

run() {
    echo "  -> $*"
    if [[ "$DRY_RUN" == false ]]; then
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Phase 1: Move orchestrator management interfaces from virbr0 to br0
# ---------------------------------------------------------------------------
echo "=== Phase 1: Move management interfaces virbr0 -> br0 ==="

SRC_BRIDGE="virbr0"
DST_BRIDGE="br0"
BRIF_DIR="/sys/class/net/${SRC_BRIDGE}/brif"

if [[ ! -d "$BRIF_DIR" ]]; then
    echo "Warning: bridge $SRC_BRIDGE does not exist, skipping phase 1" >&2
else
    moved=0
    for iface in "$BRIF_DIR"/*; do
        iface=$(basename "$iface")

        # Match orchestrator-generated interfaces: T + 14 alphanumeric chars
        if [[ ! "$iface" =~ ^T[A-Za-z0-9]{14}$ ]]; then
            continue
        fi

        echo "Moving $iface: $SRC_BRIDGE -> $DST_BRIDGE"
        run sudo ip link set "$iface" nomaster
        run sudo ip link set "$iface" master "$DST_BRIDGE"
        ((++moved))
    done
    echo "Phase 1 complete. $moved interface(s) moved."
fi

# ---------------------------------------------------------------------------
# Phase 2: Create cEOS bridges, containers, and veth plumbing
# ---------------------------------------------------------------------------
echo ""
echo "=== Phase 2: Arista cEOS containers ==="

declare -A CEOS_BRIDGES=(
    [ceos1]="ceos1-l01"
    [ceos2]="ceos2-l02"
)

CEOS_ENV=(
    -e INTFTYPE=eth
    -e ETBA=1
    -e SKIP_ZEROTOUCH_BARRIER_IN_SYSDBINIT=1
    -e CEOS=1
    -e EOS_PLATFORM=ceoslab
    -e container=docker
)

CEOS_SYSENV=(
    systemd.setenv=INTFTYPE=eth
    systemd.setenv=ETBA=1
    systemd.setenv=SKIP_ZEROTOUCH_BARRIER_IN_SYSDBINIT=1
    systemd.setenv=CEOS=1
    systemd.setenv=EOS_PLATFORM=ceoslab
    systemd.setenv=container=docker
)

# Step 1: Ensure bridges exist
echo "--- Ensuring cEOS bridges exist ---"
for bridge in "${CEOS_BRIDGES[@]}"; do
    if ip link show "$bridge" &>/dev/null; then
        echo "Bridge $bridge already exists"
    else
        echo "Creating bridge $bridge"
        run sudo ip link add name "$bridge" type bridge
    fi
    run sudo ip link set "$bridge" up
done

# Step 2: Create and start containers
echo "--- Creating and starting cEOS containers ---"
for name in "${!CEOS_BRIDGES[@]}"; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
        echo "Container $name already exists"
    else
        echo "Creating container $name"
        run docker create --name="$name" --privileged \
            "${CEOS_ENV[@]}" \
            -i -t "$CEOS_IMAGE" \
            /sbin/init "${CEOS_SYSENV[@]}"
    fi

    if docker ps --format '{{.Names}}' | grep -qx "$name"; then
        echo "Container $name already running"
    else
        echo "Starting container $name"
        run docker start "$name"
    fi
done

if [[ "$DRY_RUN" == true ]]; then
    echo ""
    echo "=== DRY RUN complete — skipping veth/namespace steps ==="
    exit 0
fi

# Step 3: Get container PIDs and set up veth plumbing
echo "--- Setting up veth plumbing ---"
for name in "${!CEOS_BRIDGES[@]}"; do
    bridge="${CEOS_BRIDGES[$name]}"
    veth_ctr="veth-${name}"
    veth_br="veth-${name}-br"

    pid=$(docker inspect -f '{{.State.Pid}}' "$name")
    echo "Container $name PID: $pid"

    if ip link show "$veth_br" &>/dev/null; then
        echo "veth pair $veth_ctr <-> $veth_br already exists"
    else
        echo "Creating veth pair: $veth_ctr <-> $veth_br"
        sudo ip link add "$veth_ctr" type veth peer name "$veth_br"
    fi

    echo "Moving $veth_ctr into container $name (netns $pid)"
    sudo ip link set "$veth_ctr" netns "$pid"

    echo "Attaching $veth_br to bridge $bridge"
    sudo ip link set "$veth_br" master "$bridge"
    sudo ip link set "$veth_br" up

    echo "Bringing up interface inside $name and renaming to eth1"
    sudo nsenter -t "$pid" -n ip link set "$veth_ctr" name eth1
    sudo nsenter -t "$pid" -n ip link set eth1 up

    echo ""
done

echo "=== All done ==="
echo "Verify with:"
echo "  ip link show type bridge"
echo "  docker exec -it ceos1 Cli "
echo "  then type 'enable' to go into enable mode"
echo "  then type 'show ip interface brief' etc."