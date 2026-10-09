#!/bin/bash
# drag.sh - long-press a point, carry it to another, drop it (a home screen icon).
# Usage: drag.sh <x1> <y1> <x2> <y2> [hold_ms]
# Android 10+: `input motionevent` steps; Android 8-9: `input draganddrop`.
set -u
cd "$(dirname "$0")"
[ $# -ge 4 ] || { echo "usage: drag.sh <x1> <y1> <x2> <y2> [hold_ms]"; exit 1; }
HOLD=$(awk "BEGIN { print ${5:-800} / 1000 }")
MX=$(( ($1 + $3) / 2 )); MY=$(( ($2 + $4) / 2 ))
./adb.sh shell "input keyevent 224" >/dev/null 2>&1
sleep 1
./adb.sh shell "v=\$(getprop ro.build.version.sdk); if [ \"\$v\" -ge 29 ]; then \
input motionevent DOWN $1 $2; sleep $HOLD; input motionevent MOVE $MX $MY; \
input motionevent MOVE $3 $4; sleep 0.6; input motionevent UP $3 $4; \
else input draganddrop $1 $2 $3 $4 1300; fi"
