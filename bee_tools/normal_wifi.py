#!/usr/bin/env python3
"""
bee_managed_on.py — Put IFACE into managed mode (default: wlan0) and let NetworkManager auto-connect.
Usage: sudo python3 bee_managed_on.py [IFACE]
"""
import os, sys, shutil, subprocess

def run(cmd, check=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=True)

def main():
    iface = sys.argv[1] if len(sys.argv) > 1 else "wlan0"
    if os.geteuid() != 0:
        print("Run as root (sudo).", file=sys.stderr)
        return 1
    for binname in ("ip", "iw"):
        if not shutil.which(binname):
            print(f"Missing: {binname}", file=sys.stderr)
            return 1

    run(["ip", "link", "set", iface, "down"], check=False)
    run(["iw", "dev", iface, "set", "type", "managed"], check=True)
    run(["ip", "link", "set", iface, "up"], check=False)

    nmcli = shutil.which("nmcli")
    if nmcli:
        run([nmcli, "networking", "on"], check=False)
        run([nmcli, "radio", "wifi", "on"], check=False)
        run([nmcli, "dev", "set", iface, "managed", "yes"], check=False)
        run([nmcli, "dev", "connect", iface], check=False)
        print(f"[OK] {iface} is MANAGED. NetworkManager will auto-connect if possible.")
        st = run([nmcli, "-g", "GENERAL.STATE,GENERAL.CONNECTION", "dev", "show", iface], check=False)
        if st.stdout.strip():
            print(st.stdout.strip())
    else:
        print(f"[OK] {iface} is MANAGED, but nmcli/NetworkManager is not available.")

    addr = run(["ip", "-br", "addr", "show", iface], check=False)
    if addr.stdout.strip():
        print(addr.stdout.strip())
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
