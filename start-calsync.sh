#!/bin/sh
# Linux: run with  sh start-calsync.sh   (add --phone to allow phones on the same Wi-Fi)
cd "$(dirname "$0")" && exec python3 launcher.py "$@"
