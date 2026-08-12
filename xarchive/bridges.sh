#!/bin/bash
set -euo pipefail

# Create ovs bridges
sudo brctl addbr p0l0
sudo brctl addbr p0l1
sudo brctl addbr p0l2
sudo brctl addbr p0l3
sudo brctl addbr p0l4
sudo brctl addbr p0l5
sudo brctl addbr p0l6
sudo brctl addbr p0l7
sudo brctl addbr p0l8
sudo brctl addbr p0l9
sudo brctl addbr p0l10
sudo brctl addbr p0l11
sudo brctl addbr p0l12
sudo brctl addbr p0l13
sudo brctl addbr p0l14
sudo brctl addbr p0l15

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
sudo brctl addbr dc01-trn-l0
sudo brctl addbr dc01-trn-l1
sudo brctl addbr dc01-inf-l0
sudo brctl addbr dc01-inf-l1
sudo brctl addbr dc02-trn-l0
sudo brctl addbr dc02-trn-l1
sudo brctl addbr dc02-inf-l0
sudo brctl addbr dc02-inf-l1

sudo ip link set dc01-trn-l0 up
sudo ip link set dc01-trn-l1 up
sudo ip link set dc01-inf-l0 up
sudo ip link set dc01-inf-l1 up
sudo ip link set dc02-trn-l0 up
sudo ip link set dc02-trn-l1 up
sudo ip link set dc02-inf-l0 up
sudo ip link set dc02-inf-l1 up