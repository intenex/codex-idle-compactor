#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 install.py --enable --max-per-day 50 --max-per-month none
