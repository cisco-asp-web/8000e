#!/bin/bash
set -euo pipefail

# Create bridges
sudo brctl addbr gp1-l0
sudo brctl addbr gp1-l1
sudo brctl addbr gp1-l2
sudo brctl addbr gp1-l3
sudo brctl addbr gp3-l0
sudo brctl addbr gp3-l1
sudo brctl addbr gp3-l2
sudo brctl addbr gp3-l3