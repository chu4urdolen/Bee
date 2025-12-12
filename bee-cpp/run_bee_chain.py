#!/usr/bin/env python3
import argparse, subprocess, time, os, signal, sys

def launch(cmd):
    return subprocess.Popen(cmd, preexec_fn=os.setsid)

def main():
    ap = argparse.ArgumentParser(description="Run Bee display/spectrum/bridge chain.")
    # debug flags
    ap.add_argument("-debug_display",  action="store_true", help="pass -debug to bee_display_driver")
    ap.add_argument("-debug_spectrum", action="store_true", help="pass -debug to bee_spectrum")
    ap.add_argument("-debug_udp",      action="store_true", help="pass -debug to bee_udp_server")
    ap.add_argument("-debug_bridge",   action="store_true", help="pass -debug to bee_bt_bridge")

    # transport (default = BT bridge)
    tg = ap.add_mutually_exclusive_group()
    tg.add_argument("--bt",  action="store_true", help="use Bluetooth bridge (default)")
    tg.add_argument("--udp", action="store_true", help="use UDP bridge instead of Bluetooth")

    ap.add_argument("--no-sudo", action="store_true", help="run binaries without sudo")
    args = ap.parse_args()

    use_udp = args.udp
    sudo = [] if args.no_sudo else ["sudo"]

    procs = []
    try:
        # 1) display
        cmd1 = [*sudo, "./bee_display_driver"]
        if args.debug_display: cmd1.append("-debug")
        print("+", " ".join(cmd1)); procs.append(launch(cmd1))
        time.sleep(2)

        # 2) spectrum
        cmd2 = [*sudo, "./bee_spectrum"]
        if args.debug_spectrum: cmd2.append("-debug")
        print("+", " ".join(cmd2)); procs.append(launch(cmd2))
        time.sleep(2)

        # 3) transport
        if use_udp:
            cmd3 = [*sudo, "./bee_udp_server"]
            if args.debug_udp: cmd3.append("-debug")
            print("+ (UDP)", " ".join(cmd3)); procs.append(launch(cmd3))
        else:
            cmd3 = [*sudo, "./bee_bt_bridge"]
            if args.debug_bridge: cmd3.append("-debug")
            print("+ (BT) ", " ".join(cmd3)); procs.append(launch(cmd3))

        print("\nrunning via {}. press Ctrl+C to stop everything.\n".format("UDP" if use_udp else "Bluetooth"))

        while True:
            exited = [p for p in procs if p.poll() is not None]
            if exited:
                print("note: a process exited:",
                      [(i, p.returncode) for i, p in enumerate(procs, 1) if p in exited])
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        for p in procs:
            if p.poll() is None:
                try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except ProcessLookupError: pass
        time.sleep(1)
        for p in procs:
            if p.poll() is None:
                try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError: pass

if __name__ == "__main__":
    sys.exit(main())
