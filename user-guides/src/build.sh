#!/usr/bin/env bash
# Print one guide to PDF with headless Chrome:  ./build.sh student-guide GCES-NoDue-Student-User-Guide
set -e
cd "$(dirname "$0")"
SRC="$1"; OUT="$2"
"/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --disable-gpu --no-pdf-header-footer \
  --run-all-compositor-stages-before-draw --virtual-time-budget=8000 \
  --print-to-pdf="$(cd .. && pwd -W)/$OUT.pdf" "file:///$(pwd -W)/$SRC.html" 2>&1 | tail -1
