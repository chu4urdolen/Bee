#!/usr/bin/env python3
"""
Bee shell bridge + inline editor (with simple auth):
- PTY /bin/bash -i with live streaming over WebSocket (/stream)
- Login via POST /login; all routes require header X-Auth-Password or ?token=
- 1000-line rolling history
"""
import asyncio, os, pty, signal, subprocess, threading, re
from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException, status, Query
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketState
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
import time
import traceback
from fastapi.staticfiles import StaticFiles

# ---------- Auth ----------
PASSWORD = os.environ.get("BEE_PASSWORD", "changeme")

def verify_password_header(request: Request):
    token = request.headers.get("X-Auth-Password") or request.query_params.get("token")
    if token != PASSWORD:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return True

# ---------- Config ----------
SHELL = "/bin/bash"
SHELL_ARGS = ["-i"]
ENV = dict(os.environ, TERM="dumb")
HISTORY_LINES = 1000
BSS_MAC_RE = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})\b")

# Strip ANSI/OSC/control sequences
ANSI_RE = re.compile(
    r"("                       
    r"\x1B\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1B\][^\x07\x1b]*\x07"  # OSC ... BEL
    r"|\x1B\][^\x1b]*\x1B\\"    # OSC ... ST
    r"|\x1B[@-Z\\-_]"           # 2-char ESC
    r")"
)
CTRL_RE = re.compile(r"[^\x09\x0A\x0D\x20-\x7E]")

def scrub(s: str) -> str:
    s = s.replace("\x1b[?2004h", "").replace("\x1b[?2004l", "")
    s = ANSI_RE.sub("", s)
    s = CTRL_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

def _run_capture(cmd: list[str], timeout: int = 180) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or ""), (r.stderr or "")

@app.post("/api/map/rebuild")
async def api_map_rebuild(_: bool = Depends(verify_password_header)):
    # 1) estimate heatmaps
    cmd1 = [
        "/mnt/data/bee/gps/estimate_heatmaps.py",
        "--db",  "/mnt/data/bee/gps/singularity.db",
        "--out", "/mnt/data/bee/gps/heatmaps",
        "--print",
    ]

    # Optional: add latest GPS point to the map render
    me_lat = None
    me_lon = None
    try:
        if gps_buf:
            pt = gps_buf[-1]
            la = pt.get("lat")
            lo = pt.get("lon")
            if la is not None and lo is not None:
                la = float(la); lo = float(lo)
                if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
                    me_lat, me_lon = la, lo
    except Exception:
        pass

    # 2) render map into /mnt/data/bee/web (map.html + static/aps.json)
    cmd2 = [
        "/mnt/data/bee/gps/render_world_map.py",
        "--summary", "/mnt/data/bee/gps/heatmaps/summary.json",
        "--web",     "/mnt/data/bee/web",
    ]

    if me_lat is not None and me_lon is not None:
        cmd2 += ["--me-lat", str(me_lat), "--me-lon", str(me_lon)]    

    try:
        try:
            await broadcast_queue.put("[map] rebuild: estimate_heatmaps...\n")
        except Exception:
            pass

        rc1, out1, err1 = await asyncio.to_thread(_run_capture, cmd1, 240)

        try:
            if out1.strip():
                await broadcast_queue.put(out1 + ("\n" if not out1.endswith("\n") else ""))
            if err1.strip():
                await broadcast_queue.put("[map] estimate_heatmaps stderr:\n" + err1 + ("\n" if not err1.endswith("\n") else ""))
        except Exception:
            pass

        if rc1 != 0:
            return JSONResponse({"ok": False, "step": "estimate_heatmaps", "rc": rc1, "stderr": err1[-2000:]}, status_code=500)

        try:
            await broadcast_queue.put("[map] rebuild: render_world_map...\n")
        except Exception:
            pass

        rc2, out2, err2 = await asyncio.to_thread(_run_capture, cmd2, 120)

        try:
            if out2.strip():
                await broadcast_queue.put(out2 + ("\n" if not out2.endswith("\n") else ""))
            if err2.strip():
                await broadcast_queue.put("[map] render_world_map stderr:\n" + err2 + ("\n" if not err2.endswith("\n") else ""))
        except Exception:
            pass

        if rc2 != 0:
            return JSONResponse({"ok": False, "step": "render_world_map", "rc": rc2, "stderr": err2[-2000:]}, status_code=500)

        return JSONResponse({"ok": True, "map": "/map.html"})
    except subprocess.TimeoutExpired as e:
        return JSONResponse({"ok": False, "error": f"timeout: {e}"}, status_code=504)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/map.html", response_class=HTMLResponse)
async def map_html():
    if os.path.exists("map.html"):
        with open("map.html", "r", encoding="utf-8", errors="replace") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h3>map.html not generated yet — hit “RSSI map”</h3>", status_code=404)

# ---------- WiFi scan -> SQLite ----------
import sqlite3, shlex

WIFI_IFACE = os.environ.get("BEE_WIFI_IFACE", "wlan0")
DB_PATH = Path(os.environ.get("BEE_SINGULARITY_DB", "/mnt/data/bee/gps/singularity.db"))
SCAN_MIN_INTERVAL = float(os.environ.get("BEE_SCAN_MIN_INTERVAL", "8.0"))

_scan_lock = asyncio.Lock()
_last_scan_wall = 0.0

def _db_init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("""
          CREATE TABLE IF NOT EXISTS wifi_obs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT NOT NULL,
            ssid TEXT,
            ts  INTEGER NOT NULL,
            lat REAL,
            lon REAL,
            rssi_dbm REAL,
            rssi_pct INTEGER,
            src TEXT,
            iface TEXT,
            scan_src TEXT
          );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_wifi_obs_mac_ts ON wifi_obs(mac, ts);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_wifi_obs_ts     ON wifi_obs(ts);")
        con.commit()
    finally:
        con.close()

def _scan_iw() -> list[dict]:
    cmd = ["/usr/sbin/iw", "dev", WIFI_IFACE, "scan"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        print("[scan] iw: not found at /usr/sbin/iw", flush=True)
        return []
    except Exception as e:
        print(f"[scan] iw: exception: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        return []

    if r.returncode != 0:
        # This is the big one: often "Operation not permitted" when running as pi.
        print(f"[scan] iw: rc={r.returncode} iface={WIFI_IFACE} stderr={r.stderr.strip()[:300]}", flush=True)
        return []

    out = []
    cur = None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        m = BSS_MAC_RE.match(line)
        if m:
            if cur and cur.get("mac"):
                out.append(cur)
            mac = m.group(1).lower()  # clean 17-char MAC, no "(on ..."
            cur = {"mac": mac, "ssid": "", "rssi_dbm": None, "rssi_pct": None, "scan_src": "iw"}
        elif line.startswith("BSS "):
            # e.g. "BSS Load:" and other non-AP sections -> ignore
            continue
        elif cur is not None and line.startswith("SSID:"):
            cur["ssid"] = line[5:].strip()
        elif cur is not None and line.startswith("signal:"):
            try:
                cur["rssi_dbm"] = float(line.split()[1])
            except Exception:
                pass

    if cur and cur.get("mac"):
        out.append(cur)

    print(f"[scan] iw: found {len(out)} APs on iface={WIFI_IFACE}", flush=True)
    return out

def wifi_scan() -> list[dict]:
    print("[scan] wifi_scan: nmcli empty -> fallback to iw", flush=True)
    return _scan_iw()

def _db_insert_obs(pt: dict, rows: list[dict]) -> int:
    if not rows:
        print("[db] insert: 0 rows (nothing to write)", flush=True)
        return 0

    try:
        con = sqlite3.connect(str(DB_PATH))
    except Exception as e:
        print(f"[db] open failed: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        return 0

    try:
        cur = con.cursor()
        ts  = int(pt.get("ts") or time.time())
        lat = pt.get("lat")
        lon = pt.get("lon")
        src = pt.get("src")

        n = 0
        for r in rows:
            cur.execute(
                """INSERT INTO wifi_obs(mac, ssid, ts, lat, lon, rssi_dbm, rssi_pct, src, iface, scan_src)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    r.get("mac"),
                    r.get("ssid"),
                    ts,
                    lat,
                    lon,
                    r.get("rssi_dbm"),
                    r.get("rssi_pct"),
                    src,
                    WIFI_IFACE,
                    r.get("scan_src"),
                ),
            )
            n += 1

        con.commit()
        print(f"[db] insert: wrote {n} rows @ ts={ts} lat={lat} lon={lon}", flush=True)
        return n
    except Exception as e:
        print(f"[db] insert failed: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()

async def scan_and_store(pt: dict):
    global _last_scan_wall

    try:
        now = time.time()
        if (now - _last_scan_wall) < SCAN_MIN_INTERVAL:
            print("[scan] throttled (too soon)", flush=True)
            return

        async with _scan_lock:
            now = time.time()
            if (now - _last_scan_wall) < SCAN_MIN_INTERVAL:
                print("[scan] throttled inside lock (too soon)", flush=True)
                return
            _last_scan_wall = now

            print(f"[scan] START ts={pt.get('ts')} lat={pt.get('lat')} lon={pt.get('lon')} src={pt.get('src')}", flush=True)
            rows = await asyncio.to_thread(wifi_scan)
            print(f"[scan] rows={len(rows)} -> inserting", flush=True)
            n = await asyncio.to_thread(_db_insert_obs, pt, rows)
            print(f"[scan] DONE inserted={n}", flush=True)

    except Exception as e:
        print(f"[scan] scan_and_store exception: {e}", flush=True)
        print(traceback.format_exc(), flush=True)

# ---------- GPS ingest ----------
GPS_MAX = int(os.environ.get("BEE_GPS_MAX", "5000"))

# Restrict who can post GPS (default: your PAN subnet + localhost)
GPS_ALLOW = os.environ.get("BEE_GPS_ALLOW", "192.168.44.0/24,127.0.0.1/32")
_GPS_NETS = []
for s in GPS_ALLOW.split(","):
    s = s.strip()
    if s:
        _GPS_NETS.append(ipaddress.ip_network(s, strict=False))

# Optional extra shared secret (so even same-subnet noobs can’t spoof)
GPS_TOKEN = os.environ.get("BEE_GPS_TOKEN", "").strip()

gps_buf = deque(maxlen=GPS_MAX)
gps_lock = asyncio.Lock()

def _gps_allowed_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except Exception:
        return False
    return any(ip in net for net in _GPS_NETS)

def _first_float(d: dict, keys: list[str]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            try:
                return float(str(d[k]).strip())
            except Exception:
                pass
    return None

def _first_int(d: dict, keys: list[str]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            try:
                return int(float(str(d[k]).strip()))
            except Exception:
                pass
    return None

@app.api_route("/gps", methods=["GET", "POST"])
@app.post("/")  # keep GET / serving index.html, but accept POST / as GPS too
async def gps_ingest(request: Request):
    # Lock it to your PAN subnet by default
    client_ip = (request.client.host if request.client else "")
    if not _gps_allowed_ip(client_ip):
        raise HTTPException(status_code=403, detail="forbidden")

    # Optional shared secret
    if GPS_TOKEN:
        if request.query_params.get("gps_token") != GPS_TOKEN:
            raise HTTPException(status_code=401, detail="bad gps token")

    data: dict = {}

    # Query params always count (good for simple URL templates)
    for k, v in request.query_params.items():
        data[k] = v

    # Try JSON
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            j = await request.json()
            if isinstance(j, dict):
                data.update(j)
        except Exception:
            pass

    # Try form (GPSLogger often does x-www-form-urlencoded)
    try:
        form = await request.form()
        for k, v in form.items():
            data[k] = str(v)
    except Exception:
        pass

    # OwnTracks sends other message types too; only treat _type=location as GPS
    typ = str(data.get("_type", "")).strip().lower()
    if typ and typ != "location":
        return JSONResponse({"ok": True, "ignored": typ})    

    # Extract common coordinate keys (GPSLogger / OwnTracks / random webhooks)
    lat = _first_float(data, ["lat", "latitude", "LAT", "Latitude"])
    lon = _first_float(data, ["lon", "lng", "longitude", "LON", "Longitude"])
    alt = _first_float(data, ["alt", "altitude"])
    acc = _first_float(data, ["acc", "accuracy", "hdop"])
    spd = _first_float(data, ["vel", "speed", "spd"])
    brg = _first_float(data, ["cog", "bearing", "course", "brg"])    

    # Timestamp if present, else now
    ts = _first_int(data, ["tst", "timestamp", "time", "ts"])
    if ts is None:
        ts = int(time.time())

    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    pt = {
        "ts": ts,
        "iso": iso,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "acc": acc,
        "speed": spd,
        "bearing": brg,
        "src": client_ip,
        "ua": request.headers.get("user-agent", ""),
        "raw": data,
    }

    gps_buf.append(pt)

    # Trigger scan in background (don’t block the HTTP response)
    asyncio.create_task(scan_and_store(pt))    

    # Echo into terminal stream (so you “see” it live)
    #try:
    #    await broadcast_queue.put(f"[gps] {iso} lat={lat} lon={lon} acc={acc}\n")
    #except Exception:
    #    pass # TODO: awesome cyberpunk monitor thingy

    return JSONResponse({"ok": True})

@app.get("/api/gps/latest")
async def gps_latest(_: bool = Depends(verify_password_header)):
    if not gps_buf:
        return JSONResponse({"ok": True, "point": None})
    return JSONResponse({"ok": True, "point": gps_buf[-1]})

@app.get("/api/gps/samples")
async def gps_samples(limit: int = 200, _: bool = Depends(verify_password_header)):
    limit = max(1, min(int(limit), 5000))
    pts = list(gps_buf)[-limit:]
    return JSONResponse({"ok": True, "count": len(pts), "points": pts})

# ---------- PTY + Bash ----------
master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(
    [SHELL] + SHELL_ARGS,
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    preexec_fn=os.setsid, close_fds=True, env=ENV
)
os.close(slave_fd)

# ---------- Broadcast + History ----------
clients_lock = threading.Lock()
clients: set[WebSocket] = set()
broadcast_queue: asyncio.Queue[str] = asyncio.Queue()
history = deque(maxlen=HISTORY_LINES)
_hist_buf = ""

def _ingest_text(txt: str):
    global _hist_buf
    _hist_buf += txt
    parts = _hist_buf.split('\n')
    _hist_buf = parts[-1]
    for line in parts[:-1]:
        history.append(line + '\n')

async def _broadcaster():
    while True:
        msg = await broadcast_queue.get()
        _ingest_text(msg)
        with clients_lock:
            targets = list(clients)
        if targets:
            await asyncio.gather(*[
                ws.send_text(msg) for ws in targets
                if ws.client_state == WebSocketState.CONNECTED
            ], return_exceptions=True)

def _reader_thread(loop: asyncio.AbstractEventLoop):
    try:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
                if not chunk: break
                text = chunk.decode("utf-8", errors="replace")
                clean = scrub(text)
                if clean:
                    asyncio.run_coroutine_threadsafe(broadcast_queue.put(clean), loop)
            except OSError:
                break
    finally:
        asyncio.run_coroutine_threadsafe(broadcast_queue.put("\n[pty closed]\n"), loop)

@app.on_event("startup")
async def _startup():
    _db_init()    
    loop = asyncio.get_running_loop()
    asyncio.create_task(_broadcaster())
    t = threading.Thread(target=_reader_thread, args=(loop,), daemon=True)
    t.start()

# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def index():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h3>Put index.html next to server.py</h3>")

@app.post("/login")
async def login(payload: dict):
    return {"ok": payload.get("password") == PASSWORD}

def _cwd() -> str:
    return os.readlink(f"/proc/{proc.pid}/cwd")

def _resolve_in_cwd(name: str) -> str:
    base = os.path.realpath(_cwd())
    target = os.path.realpath(os.path.join(base, name))
    if not (target == base or target.startswith(base + os.sep)):
        raise ValueError("invalid path")
    return target

@app.get("/cwd")
async def get_cwd(_: bool = Depends(verify_password_header)):
    try:
        return JSONResponse({"cwd": _cwd()})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/open")
async def open_file(request: Request, _: bool = Depends(verify_password_header)):
    try:
        name = request.query_params.get("filename", "")
        if not name:
            return JSONResponse({"ok": False, "error": "filename required"}, status_code=400)
        path = _resolve_in_cwd(name)
        if not os.path.exists(path):
            return JSONResponse({"ok": True, "exists": False, "path": path, "bytes": 0, "content": ""})
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return JSONResponse({"ok": True, "exists": True, "path": path, "bytes": len(data), "content": data})
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/run")
async def run_cmd(payload: dict, _: bool = Depends(verify_password_header)):
    cmd = (payload.get("cmd") or "").rstrip("\r\n")
    if not cmd: return JSONResponse({"ok": True})
    try:
        os.write(master_fd, (cmd + "\n").encode())
        return JSONResponse({"ok": True})
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/signal")
async def send_signal(payload: dict, _: bool = Depends(verify_password_header)):
    sig = (payload.get("sig") or "").upper()
    sig_map = {"INT": signal.SIGINT, "TERM": signal.SIGTERM, "HUP": signal.SIGHUP}
    if sig not in sig_map:
        return JSONResponse({"ok": False, "error": "unknown signal"}, status_code=400)
    try:
        if sig == "INT":
            sent = False
            try:
                fg_pgid = os.tcgetpgrp(master_fd)
                os.killpg(fg_pgid, signal.SIGINT); sent = True
            except Exception:
                pass
            if not sent:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGINT); sent = True
                except Exception:
                    pass
            try:
                os.write(master_fd, b"\x03")
            except Exception:
                pass
            if not sent:
                return JSONResponse({"ok": False, "error": "could not deliver SIGINT"}, status_code=500)
            return JSONResponse({"ok": True})
        os.killpg(os.getpgid(proc.pid), sig_map[sig])
        return JSONResponse({"ok": True})
    except ProcessLookupError:
        return JSONResponse({"ok": False, "error": "process not running"}, status_code=410)

@app.post("/save")
async def save_file(payload: dict, _: bool = Depends(verify_password_header)):
    name = payload.get("filename")
    content = payload.get("content", "")
    if not name or not isinstance(name, str):
        return JSONResponse({"ok": False, "error": "filename required"}, status_code=400)
    try:
        path = _resolve_in_cwd(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return JSONResponse({"ok": True, "path": path, "bytes": len(content)})
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.websocket("/stream")
async def stream(ws: WebSocket, token: str | None = Query(default=None)):
    if token != PASSWORD:
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    with clients_lock:
        clients.add(ws)
    try:
        if history:
            await ws.send_text("".join(history))
        while True:
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        pass
    finally:
        with clients_lock:
            clients.discard(ws)

@app.on_event("shutdown")
async def _shutdown():
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
