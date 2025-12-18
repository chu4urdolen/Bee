#!/bin/bash
set -euo pipefail

SSID="${DUSHICH_SSID:-Dushich}"
CONN="${DUSHICH_CONN:-Dushich}"
PHONE_MAC="${DUSHICH_BT_MAC:-A8:CC:6F:0A:A0:00}"
BT_IFACE="${DUSHICH_BT_IFACE:-bnep0}"
BT_METRIC="${DUSHICH_BT_METRIC:-50}"

# Static BT IP (set empty to go back to DHCP)
BT_STATIC_IP="${DUSHICH_BT_STATIC_IP:-192.168.44.55/24}"
BT_GW="${DUSHICH_BT_GW:-192.168.44.1}"

log(){ echo "[dushich-watch] $*" >&2; }

nmcli radio wifi on >/dev/null 2>&1 || true
modprobe bnep >/dev/null 2>&1 || true

get_wdev(){
  (nmcli -t -f DEVICE,TYPE dev status 2>/dev/null || true) | awk -F: '$2=="wifi"{print $1; exit}'
}

bt_pan_up(){
  bluetoothctl >/dev/null 2>&1 <<EOF || true
power on
trust $PHONE_MAC
connect $PHONE_MAC
EOF

  if ! ip link show "$BT_IFACE" >/dev/null 2>&1; then
    if command -v bt-network >/dev/null 2>&1; then
      pgrep -f "bt-network -c $PHONE_MAC nap" >/dev/null 2>&1 || (bt-network -c "$PHONE_MAC" nap >/dev/null 2>&1 &)
    elif command -v bt-pan >/dev/null 2>&1; then
      pgrep -f "bt-pan client $PHONE_MAC" >/dev/null 2>&1 || (bt-pan client "$PHONE_MAC" >/dev/null 2>&1 &)
    else
      log "no bt-network/bt-pan found (install bluez-tools)"
      return 0
    fi

    for _ in $(seq 1 20); do
      ip link show "$BT_IFACE" >/dev/null 2>&1 && break
      sleep 1
    done
    ip link show "$BT_IFACE" >/dev/null 2>&1 || return 0
  fi

  ip link set "$BT_IFACE" up >/dev/null 2>&1 || true

  if [ -n "${BT_STATIC_IP:-}" ]; then
    # Pin a fixed identity on the tether
    ip addr flush dev "$BT_IFACE" >/dev/null 2>&1 || true
    ip addr add "$BT_STATIC_IP" dev "$BT_IFACE" >/dev/null 2>&1 || true
  else
    # DHCP fallback if you ever want it back
    if ! ip -4 addr show dev "$BT_IFACE" | grep -q "inet "; then
      dhclient -1 -v "$BT_IFACE" >/dev/null 2>&1 || true
    fi
  fi

  ip route replace default via "$BT_GW" dev "$BT_IFACE" metric "$BT_METRIC" >/dev/null 2>&1 || true

  # Optional DNS wiring if systemd-resolved exists
  if command -v resolvectl >/dev/null 2>&1; then
    resolvectl dns "$BT_IFACE" "$BT_GW" 1.1.1.1 8.8.8.8 >/dev/null 2>&1 || true
    resolvectl domain "$BT_IFACE" "~." >/dev/null 2>&1 || true
  fi
}

wifi_hotspot_up(){
  local wdev cur list
  wdev="$(get_wdev)"
  [ -z "${wdev:-}" ] && return 0

  cur="$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}' || true)"
  [ "${cur:-}" = "$SSID" ] && return 0

  list="$(nmcli -t -f SSID dev wifi list --rescan yes 2>/dev/null || true)"
  echo "$list" | grep -Fxq "$SSID" || return 0

  log "seeing Wi-Fi  — connecting (cur=none)"
  nmcli con up id "$CONN" >/dev/null 2>&1 || true
  nmcli dev wifi connect "$SSID" ifname "$wdev" >/dev/null 2>&1 || true
}

while true; do
  bt_pan_up || true
  wifi_hotspot_up || true
  sleep 10
done
