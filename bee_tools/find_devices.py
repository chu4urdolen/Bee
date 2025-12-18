#!/usr/bin/env python3
"""
find_devices.py — enter monitor on wlan0, capture a pcap on the *current* tuned channel, restore managed mode, parse pcap via bettercap offline.

Usage:
  sudo python3 find_devices.py --seconds 5 --log --detach

Args:
  --iface wlan0
  --seconds N
  --pcap PATH
  --out PATH
  --log        write to ./find_devices.log
  --detach     keep running even if SSH drops (recommended for monitor-mode flip)
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import signal
from datetime import datetime
from pathlib import Path


class Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def run(cmd, check=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("Run as root (sudo).")


def ensure_bin(name):
    if shutil.which(name) is None:
        raise SystemExit(f"Missing '{name}'.")


def enable_log(flag: bool):
    if not flag:
        return None
    tools_dir = Path(__file__).resolve().parent
    log_path = tools_dir / "find_devices.log"
    fh = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = Tee(sys.stdout, fh)
    sys.stderr = Tee(sys.stderr, fh)

    print("\n" + "=" * 70)
    print(f"[LOG] {datetime.now().isoformat(sep=' ', timespec='seconds')}  pid={os.getpid()}")
    print(f"[LOG] argv: {' '.join(sys.argv)}")
    print(f"[LOG] writing to: {log_path}")
    print("=" * 70 + "\n")
    return fh


def detach_to_background(log_path: Path):
    # Double-fork daemonize, redirect stdio to log (so we keep writing after SSH dies).
    pid = os.fork()
    if pid > 0:
        # Parent exits immediately (SSH command returns).
        os._exit(0)

    os.setsid()

    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Redirect stdin to /dev/null; stdout/stderr to log file.
    sys.stdin.flush()
    sys.stdout.flush()
    sys.stderr.flush()

    devnull = os.open("/dev/null", os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    lf = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(lf, 1)
    os.dup2(lf, 2)
    os.close(lf)


def show_iface_info(iface: str, tag: str):
    print(f"[+] {tag}: iw dev {iface} info")
    r = run(["iw", "dev", iface, "info"], check=False)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)

    print(f"[+] {tag}: iw dev {iface} link")
    r = run(["iw", "dev", iface, "link"], check=False)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)


def set_monitor_mode(iface: str):
    run(["ip", "link", "set", iface, "down"], check=False)
    r = run(["iw", "dev", iface, "set", "type", "monitor"], check=False)
    if r.returncode != 0:
        raise SystemExit(f"Could not set {iface} to monitor mode:\n{(r.stderr or r.stdout).strip()}")
    run(["ip", "link", "set", iface, "up"], check=False)


def tcpdump_capture(iface: str, seconds: int, pcap_path: Path):
    pcap_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["tcpdump", "-I", "-i", iface, "-U", "-s", "256", "-w", str(pcap_path)]
    print(f"[+] tcpdump capture on {iface} for {seconds}s -> {pcap_path}")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    try:
        time.sleep(max(1, seconds))
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=2.0)
            except Exception:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()

    stderr = ""
    if proc.stderr:
        try:
            stderr = (proc.stderr.read() or "").strip()
        except Exception:
            stderr = ""

    if stderr:
        print(f"[+] tcpdump stderr:\n{stderr}")

    print("[+] tcpdump done")


def bettercap_parse_pcap(pcap_path: Path, out_txt: Path, iface_hint: str = "wlan0") -> str:
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    eval_cmd = "; ".join([
        f"set wifi.interface {iface_hint}",
        f"set wifi.source.file {pcap_path}",
        "wifi.recon on",
        "wifi.show",
        "wifi.recon off",
        "quit",
    ])

    print(f"[+] bettercap offline parse -> {out_txt}")
    p = subprocess.run(
        ["bettercap", "-no-colors", "-eval", eval_cmd],
        text=True,
        capture_output=True
    )
    txt = (p.stdout or "") + (p.stderr or "")
    out_txt.write_text(txt, encoding="utf-8", errors="replace")
    print("[+] bettercap parse done")
    return txt


def restore_wifi(iface: str):
    tools_dir = Path(__file__).resolve().parent
    normal = tools_dir / "normal_wifi.py"
    if not normal.exists():
        print(f"[WARN] restore requested but {normal} not found.", file=sys.stderr)
        return
    subprocess.run([sys.executable, str(normal), iface], text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--pcap", default="", help="PCAP output path (default: ./sniff-<ts>.pcap)")
    ap.add_argument("--out", default="", help="bettercap text output path (default: ./bettercap-<ts>.txt)")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--detach", action="store_true")
    args = ap.parse_args()

    tools_dir = Path(__file__).resolve().parent
    log_path = tools_dir / "find_devices.log"

    # If detaching, do it BEFORE touching wlan0.
    if args.detach:
        # Ensure log file exists and then daemonize into it.
        log_path.parent.mkdir(parents=True, exist_ok=True)
        open(log_path, "a").close()
        detach_to_background(log_path)
        # After redirect, we want logging behavior even if --log wasn't set.
        args.log = True

    log_fh = enable_log(args.log)

    try:
        require_root()
        ensure_bin("iw")
        ensure_bin("ip")
        ensure_bin("tcpdump")
        ensure_bin("bettercap")

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        pcap_path = Path(args.pcap).expanduser() if args.pcap else (tools_dir / f"sniff-{ts}.pcap")
        out_txt = Path(args.out).expanduser() if args.out else (tools_dir / f"bettercap-{ts}.txt")

        print(f"[+] will write pcap to: {pcap_path}")

        show_iface_info(args.iface, "before monitor")

        print(f"[+] switching {args.iface} -> monitor (no channel set; sniff current tuned channel)")
        set_monitor_mode(args.iface)

        show_iface_info(args.iface, "in monitor")

        tcpdump_capture(args.iface, args.seconds, pcap_path)

        print("\n[+] restoring managed WiFi (normal_wifi.py)...")
        restore_wifi(args.iface)

        show_iface_info(args.iface, "after restore")

        txt = bettercap_parse_pcap(pcap_path, out_txt, iface_hint=args.iface)

        print("\n=== bettercap wifi.show (from pcap) ===")
        print(txt)

        return 0

    finally:
        if log_fh is not None:
            try:
                print(f"\n[LOG] done @ {datetime.now().isoformat(sep=' ', timespec='seconds')}")
                log_fh.flush()
                log_fh.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
