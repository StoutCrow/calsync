#!/bin/bash
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  python3 launcher.py "$@"
else
  echo "Python is not installed. Get it from https://www.python.org/downloads/ and try again."
  read -r -p "Press Enter to close."
fi
