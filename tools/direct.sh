#!/bin/bash
# direct.sh - is the phone's UI server reachable straight over the tailnet
# (its port 9008), without adb in between, and how long does a call take?
#
# The helper reaches the server through adb's streams (see tunnel.sh for
# the tunnel to the phone's adb port): each request crosses the link as
# adb packets with their acknowledgements. A plain TCP connection to the
# server would cross it once per request. This times GET /ping both ways
# a few times, through the same proxy tunnel.sh uses (none on a computer
# that is on the tailnet itself).
#
#     tools/direct.sh            # three pings each way
set -u
cd "$(dirname "$0")/.."
source ./config.env
: "${PHONE_TAILSCALE_IP:?PHONE_TAILSCALE_IP not set in config.env}"
PORT="${U2_PORT:-9008}"

if [ -n "${HTTPS_PROXY:-}" ]; then
  PROXY_HOST="${HTTPS_PROXY#http://}"
  PROXY_HOST="${PROXY_HOST#*@}"
  PROXY_HOST="${PROXY_HOST%%:*}"
  PROXY_AUTH="${HTTPS_PROXY#http://}"
  PROXY_AUTH="${PROXY_AUTH%%@*}"
  TO="PROXY:${PROXY_HOST}:${PHONE_TAILSCALE_IP}:${PORT},proxyport=3130,proxyauth=${PROXY_AUTH}"
else
  TO="TCP:${PHONE_TAILSCALE_IP}:${PORT}"
fi

now_ms() { date +%s%N | cut -b1-13; }

echo "direct TCP to ${PHONE_TAILSCALE_IP}:${PORT} (each a new connection):"
for i in 1 2 3; do
  t0=$(now_ms)
  reply=$(printf 'GET /ping HTTP/1.0\r\nHost: phone\r\n\r\n' | socat -T 8 - "$TO" 2>&1 | tr -d '\r' | tail -n 1)
  echo "  ${reply:-no reply} ($(( $(now_ms) - t0 ))ms)"
done

ADB="${ADB:-$HOME/burner/.android-tools/platform-tools/adb}"
[ -x "$ADB" ] || ADB=adb
SERIAL="127.0.0.1:${LOCAL_PORT:-15555}"
echo "through adb (forward to the same port, each a new connection):"
"$ADB" -s "$SERIAL" forward tcp:19008 tcp:"$PORT" >/dev/null 2>&1 || echo "  (adb forward failed)"
for i in 1 2 3; do
  t0=$(now_ms)
  reply=$(curl -s --max-time 8 "http://127.0.0.1:19008/ping")
  echo "  ${reply:-no reply} ($(( $(now_ms) - t0 ))ms)"
done
"$ADB" -s "$SERIAL" forward --remove tcp:19008 >/dev/null 2>&1
