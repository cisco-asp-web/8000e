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