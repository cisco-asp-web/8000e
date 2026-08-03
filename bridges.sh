#!/bin/bash
set -euo pipefail

# Create bridges
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