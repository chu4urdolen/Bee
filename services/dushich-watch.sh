#!/bin/bash
set -euo pipefail

export PATH="/usr/sbin:/sbin:/usr/bin:/bin"

PHONE_MAC="${DUSHICH_BT_MAC:-A8:CC:6F:0A:A0:00}"
BT_IFACE="${DUSHICH_BT_IFACE:-}"          # optional, leave empty to auto-detect bnep*
BT_METRIC="${DUSHICH_BT_METRIC:-50}"

DEBUG="${DUSHICH_DEBUG:-1}"
T_BTCTL="${DUSHICH_T_BTCTL:-8}"
T_DHCP="${DUSHICH_T_DHCP:-30}"
T_SCAN="${DUSHICH_T_SCAN:-30}"

BTNETWORK_BIN="/usr/bin/bt-network"
DHCLIENT_BIN="/usr/sbin/dhclient"
TIMEOUT_BIN="/usr/bin/timeout"

log(){
  [ "${DEBUG}" = "0" ] && return 0
  echo "[dushich-watch] $(date '+%F %T') $*" >&2
}

die(){
  echo "[dushich-watch] FATAL: $*" >&2
  exit 1
}

[ "${EUID}" -eq 0 ] || die "run me as root (sudo -E /bee/services/dushich-watch.sh)"

[ -x "$TIMEOUT_BIN" ]   || die "missing: $TIMEOUT_BIN (coreutils)"
[ -x "$BTNETWORK_BIN" ] || die "missing: $BTNETWORK_BIN (bluez-tools)"
[ -x "$DHCLIENT_BIN" ]  || die "missing: $DHCLIENT_BIN (isc-dhcp-client)"
command -v bluetoothctl >/dev/null 2>&1 || die "missing: bluetoothctl (bluez)"
command -v ip >/dev/null 2>&1 || die "missing: ip (iproute2)"

run(){
  local t="$1"; shift
  log "RUN: $*"
  "$TIMEOUT_BIN" "${t}s" "$@" 2>&1 | sed 's/^/[dushich-watch]   /' >&2
}

modprobe bnep >/dev/null 2>&1 || true

detect_bnep(){
  if [ -n "${BT_IFACE:-}" ]; then
    ip link show "$BT_IFACE" >/dev/null 2>&1 && { echo "$BT_IFACE"; return 0; }
    return 1
  fi
  ip -o link show | awk -F': ' '$2 ~ /^bnep[0-9]+/ {print $2; exit}'
}

ensure_bt_pan(){
  log "=== ensure_bt_pan (mac=$PHONE_MAC) ==="

  run "$T_BTCTL" bluetoothctl <<EOF
power on
trust $PHONE_MAC
quit
EOF

  if ! pgrep -f "bt-network -c $PHONE_MAC nap" >/dev/null 2>&1; then
    log "Starting bt-network NAP in background"
    ("$BTNETWORK_BIN" -c "$PHONE_MAC" nap >/dev/null 2>&1 &)
  else
    log "bt-network already running"
  fi

  local ifc=""
  for i in $(seq 1 "$T_SCAN"); do
    ifc="$(detect_bnep 2>/dev/null || true)"
    [ -n "${ifc:-}" ] && break
    log "Waiting for bnep* (${i}/${T_SCAN})..."
    sleep 1
  done
  [ -n "${ifc:-}" ] || { log "No bnep interface appeared."; return 0; }

  log "Using iface: $ifc"
  ip link set "$ifc" up >/dev/null 2>&1 || true
  ip -br addr show dev "$ifc" | sed 's/^/[dushich-watch]   /' >&2 || true

  if ! ip -4 addr show dev "$ifc" | grep -q "inet "; then
    log "No IPv4 yet -> DHCP on $ifc"
    run "$T_DHCP" "$DHCLIENT_BIN" -1 -v "$ifc" || true
  else
    log "IPv4 already present on $ifc"
  fi

  ip -br addr show dev "$ifc" | sed 's/^/[dushich-watch]   /' >&2 || true

  local gw=""
  gw="$(ip -4 route show dev "$ifc" default 2>/dev/null | awk '{print $3; exit}' || true)"
  if [ -n "${gw:-}" ]; then
    log "Default GW on $ifc is $gw -> enforcing metric=$BT_METRIC"
    ip route replace default via "$gw" dev "$ifc" metric "$BT_METRIC" >/dev/null 2>&1 || true
  else
    log "No default route seen on $ifc (phone tether not handing one out yet)."
  fi

  ip -4 route | sed 's/^/[dushich-watch]   /' >&2 || true
}

while true; do
  ensure_bt_pan || true
  sleep 10
done
