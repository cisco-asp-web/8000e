#!/bin/bash
set -euo pipefail

# Create ovs bridges
sudo ovs-vsctl add-br p0l0
sudo ovs-vsctl add-br p0l1
sudo ovs-vsctl add-br p0l2
sudo ovs-vsctl add-br p0l3
sudo ovs-vsctl add-br p0l4
sudo ovs-vsctl add-br p0l5
sudo ovs-vsctl add-br p0l6
sudo ovs-vsctl add-br p0l7
sudo ovs-vsctl add-br p0l8
sudo ovs-vsctl add-br p0l9
sudo ovs-vsctl add-br p0l10
sudo ovs-vsctl add-br p0l11
sudo ovs-vsctl add-br p0l12
sudo ovs-vsctl add-br p0l13
sudo ovs-vsctl add-br p0l14
sudo ovs-vsctl add-br p0l15

sudo ip link set p0l0 up
sudo ip link set p0l1 up
sudo ip link set p0l2 up
sudo ip link set p0l3 up
sudo ip link set p0l4 up
sudo ip link set p0l5 up
sudo ip link set p0l6 up
sudo ip link set p0l7 up
sudo ip link set p0l8 up
sudo ip link set p0l9 up
sudo ip link set p0l10 up
sudo ip link set p0l11 up
sudo ip link set p0l12 up
sudo ip link set p0l13 up
sudo ip link set p0l14 up
sudo ip link set p0l15 up

# Create linux bridges
sudo brctl addbr dc0-host00-l0
sudo brctl addbr dc0-host00-l1
sudo brctl addbr dc0-host01-l0
sudo brctl addbr dc0-host01-l1
sudo brctl addbr dc1-host00-l0
sudo brctl addbr dc1-host00-l1
sudo brctl addbr dc1-host01-l0
sudo brctl addbr dc1-host01-l1

sudo ip link set dc0-host00-l0 up
sudo ip link set dc0-host00-l1 up
sudo ip link set dc0-host01-l0 up
sudo ip link set dc0-host01-l1 up
sudo ip link set dc1-host00-l0 up
sudo ip link set dc1-host00-l1 up
sudo ip link set dc1-host01-l0 up
sudo ip link set dc1-host01-l1 up