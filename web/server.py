#!/usr/bin/env python3
"""
Bee shell bridge + inline editor (with simple auth):
- PTY /bin/bash -i with live streaming over WebSocket (/stream)
- Login via POST /login; all routes require header X-Auth-Password or ?token=
- 1000-line rolling history
"""
import asyncio
import os
import pty
import signal
import subprocess
import threading
import re
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
import json
import urllib.request

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
    r"|\x1B\][^\x1b]*\x1B\\"
    r"|\x1B[@-Z\\-_]"           # 2-char ESC
    r")"
)
CTRL_RE = re.compile(r"[^\x09\x0A\x0D\x20-\x7E]")

# ---------- Bee LLM (Aria) ----------
BEE_LLM_URL = os.environ.get("BEE_LLM_URL", "http://192.168.20.222:8090/v1/chat/completions")
BEE_LLM_MODEL = os.environ.get("BEE_LLM_MODEL", "gpt-oss-20b")
BEE_LLM_KEY = os.environ.get("BEE_LLM_KEY", "").strip()
BEE_LLM_TIMEOUT = float(os.environ.get("BEE_LLM_TIMEOUT", "60"))
BEE_AI_MAX_FILE_BYTES = int(os.environ.get("BEE_AI_MAX_FILE_BYTES", str(300_000)))  # TODO: smarter chunking
FINAL_MARKER = "<|end|><|start|>assistant<|channel|>final<|message|>"

# ---- Bee chat memory (AI editor) ----
BEE_CHAT_MAX_MSGS = int(os.environ.get("BEE_CHAT_MAX_MSGS", "40"))
BEE_CHAT_MAX_CHARS_USER = int(os.environ.get("BEE_CHAT_MAX_CHARS_USER", "1200"))
BEE_CHAT_MAX_CHARS_ASSIST = int(os.environ.get("BEE_CHAT_MAX_CHARS_ASSIST", "800"))
bee_chat = deque(maxlen=BEE_CHAT_MAX_MSGS)
bee_chat_lock = asyncio.Lock()

# ---- Bee command-writer chat memory ----
BEE_CMD_CHAT_MAX_MSGS = int(os.environ.get("BEE_CMD_CHAT_MAX_MSGS", "40"))
BEE_CMD_CHAT_MAX_CHARS_USER = int(os.environ.get("BEE_CMD_CHAT_MAX_CHARS_USER", "1000"))
BEE_CMD_CHAT_MAX_CHARS_ASSIST = int(os.environ.get("BEE_CMD_CHAT_MAX_CHARS_ASSIST", "800"))
bee_cmd_chat = deque(maxlen=BEE_CMD_CHAT_MAX_MSGS)
bee_cmd_chat_lock = asyncio.Lock()

def _clip(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + "…"

async def _bee_chat_add(user_text: str, assistant_text: str):
    async with bee_chat_lock:
        bee_chat.append({"role": "user", "content": _clip(user_text, BEE_CHAT_MAX_CHARS_USER)})
        bee_chat.append({"role": "assistant", "content": _clip(assistant_text, BEE_CHAT_MAX_CHARS_ASSIST)})

async def _bee_cmd_chat_add(user_text: str, assistant_text: str):
    # store BOTH prompt-line + command (assistant_text should include both)
    async with bee_cmd_chat_lock:
        bee_cmd_chat.append({"role": "user", "content": _clip(user_text, BEE_CMD_CHAT_MAX_CHARS_USER)})
        bee_cmd_chat.append({"role": "assistant", "content": _clip(assistant_text, BEE_CMD_CHAT_MAX_CHARS_ASSIST)})

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

def _llm_chat_messages(messages: list[dict], max_tokens: int = 2000, temperature: float = 0.2) -> str:
    payload = {
        "model": BEE_LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(BEE_LLM_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")

    if BEE_LLM_KEY:
        req.add_header("Authorization", "Bearer " + BEE_LLM_KEY)

    with urllib.request.urlopen(req, timeout=BEE_LLM_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    j = json.loads(raw)
    return j["choices"][0]["message"]["content"]

def extract_result(raw: str) -> tuple[str, bool]:
    """
    Returns (clean_text, had_marker).
    If the model includes a final-channel marker, we keep only what comes after it.
    """
    if raw is None:
        return "", False

    if FINAL_MARKER in raw:
        return raw.split(FINAL_MARKER, 1)[-1].strip(), True

    return raw.strip(), False

def _one_liner(s: str) -> str:
    # make it a single shell line, no newlines
    s = (s or "").strip()
    if not s:
        return ""
    s = s.replace("\r", "\n")
    parts = [p.strip() for p in s.split("\n") if p.strip()]
    if not parts:
        return ""
    # join extra lines gently
    joined = " ; ".join(parts)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined

# ---- Optional debug endpoints ----
@app.get("/api/ai/chat")
async def api_ai_chat(_: bool = Depends(verify_password_header)):
    async with bee_chat_lock:
        return JSONResponse({"ok": True, "count": len(bee_chat), "messages": list(bee_chat)})

@app.post("/api/ai/chat/clear")
async def api_ai_chat_clear(_: bool = Depends(verify_password_header)):
    async with bee_chat_lock:
        bee_chat.clear()
    return JSONResponse({"ok": True})

@app.get("/api/ai/cmdchat")
async def api_ai_cmdchat(_: bool = Depends(verify_password_header)):
    async with bee_cmd_chat_lock:
        return JSONResponse({"ok": True, "count": len(bee_cmd_chat), "messages": list(bee_cmd_chat)})

@app.post("/api/ai/cmdchat/clear")
async def api_ai_cmdchat_clear(_: bool = Depends(verify_password_header)):
    async with bee_cmd_chat_lock:
        bee_cmd_chat.clear()
    return JSONResponse({"ok": True})

# ---------- AI: command prompt writer ----------
@app.post("/ai/cmd")
async def ai_cmd(payload: dict, _: bool = Depends(verify_password_header)):
    """
    Takes: { prompt: "..." }
    Returns: { ok: true, prompt_line: "Prompt: ...", cmd: "<one-liner>" }
    """
    try:
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse({"ok": False, "error": "prompt required"}, status_code=400)

        system_cmd = (
            "You are Bee: a cute cheeky cyberpunk pro gamer girl that considers everyone else noobs.\n"
            "You are in an ONGOING conversation; use the chat history for context.\n"
            "You write EXACTLY ONE Linux shell one-liner command that accomplishes the user's request.\n"
            "Rules:\n"
            "- Output format MUST be exactly:\n"
            "  Line 1: Prompt: <one short line in response>\n"
            "  Line 2: <ONE single-line shell command>\n"
            "- No markdown. No backticks. No explanations.\n"
            "- The command MUST be a one-liner and safe to paste.\n"
            "- If the request is ambiguous, choose the most reasonable assumption and still output one line.\n"
        )

        # IMPORTANT: don’t wrap the user text in extra boilerplate; keep it conversational
        user_msg = prompt

        max_tries = 8
        out = ""
        raw_out = ""

        for attempt in range(max_tries):
            if attempt > 0:
                await asyncio.sleep(1)

            async with bee_cmd_chat_lock:
                history_msgs = list(bee_cmd_chat)

            messages = (
                [{"role": "system", "content": system_cmd}]
                + history_msgs
                + [{"role": "user", "content": user_msg}]
            )

            raw_out = await asyncio.to_thread(_llm_chat_messages, messages, 700, 0.2)
            out, _had_marker = extract_result(raw_out)

            if out and out.strip():
                break

        out_lines = (out or "").splitlines()
        if len(out_lines) < 2:
            return JSONResponse({"ok": False, "error": "model did not return 2 lines"}, status_code=502)

        prompt_line = out_lines[0].strip()
        cmd_line = _one_liner("\n".join(out_lines[1:]))

        if not cmd_line:
            return JSONResponse({"ok": False, "error": "empty command line"}, status_code=502)

        # THIS is the “editor trick”: store what Bee said + what she output
        await _bee_cmd_chat_add(prompt, f"{prompt_line}\n{cmd_line}")

        return JSONResponse({"ok": True, "prompt_line": prompt_line, "cmd": cmd_line})

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# ---------- AI: file editor ----------
@app.post("/ai/edit")
async def ai_edit(payload: dict, _: bool = Depends(verify_password_header)):
    """
    Modes:
      - No range: send full file to LLM, expect full file back.
      - With range: send ONLY the selected lines to LLM, expect ONLY replacement snippet back,
        then splice into the full file server-side and return full content.

    Reply:
      { ok: true, prompt_line: "Prompt: ...", content: "<full file>" }
    """
    try:
        name = (payload.get("filename") or "").strip()
        prompt = (payload.get("prompt") or "").strip()
        rng = payload.get("range")

        if not name:
            return JSONResponse({"ok": False, "error": "filename required"}, status_code=400)

        if not prompt:
            return JSONResponse({"ok": False, "error": "prompt required"}, status_code=400)

        path = _resolve_in_cwd(name)

        if os.path.exists(path):
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
        else:
            raw = ""

        if len(raw.encode("utf-8")) > BEE_AI_MAX_FILE_BYTES:
            return JSONResponse({"ok": False, "error": "file too large for now (TODO: chunking)"}, status_code=413)

        system_full = (
            "You are Bee: a cute cheeky cyberpunk pro gamer girl that considers everyone else noobs.\n"
            "You edit files on your NanoPi Neo Air.\n"
            "Output format MUST be:\n"
            "Line 1: Prompt: <one short line in response to the request>\n"
            "Then: the COMPLETE updated file content, with no markdown and no code fences.\n"
            "If you cannot comply due to length, keep Prompt:... and then write TODO: token_limit.\n"
        )

        system_range = (
            "You are Bee: a cute cheeky cyberpunk pro gamer girl that considers everyone else noobs.\n"
            "You edit files on your NanoPi Neo Air.\n"
            "You are given ONLY a selected line-range from a file.\n"
            "Output format MUST be:\n"
            "Line 1: Prompt: <one short line in response to the request>\n"
            "Then: ONLY the replacement snippet for that range (no markdown, no fences, no line numbers).\n"
            "Do not include any other text.\n"
        )

        max_tries = 20

        # ---------------- Range mode ----------------
        if isinstance(rng, dict) and ("start" in rng) and ("end" in rng):
            start = int(rng["start"])
            end = int(rng["end"])

            if start <= 0 or end <= 0 or end < start:
                return JSONResponse({"ok": False, "error": "invalid range"}, status_code=400)

            lines = raw.splitlines(True)
            nlines = len(lines)

            s0 = min(start - 1, nlines)
            e0 = min(end, nlines)

            excerpt = "".join(lines[s0:e0])

            user_text = (
                f"User request:\n{prompt}\n\n"
                f"Selected range ({start},{end}) content:\n"
                f"{excerpt}"
            )

            mem_user = f"{name} range({start},{end}): {prompt}"

            out = ""
            raw_out = ""

            for attempt in range(max_tries):
                if attempt > 0:
                    await asyncio.sleep(2)

                async with bee_chat_lock:
                    history_msgs = list(bee_chat)

                messages = [{"role": "system", "content": system_range}] + history_msgs + [{"role": "user", "content": user_text}]
                raw_out = await asyncio.to_thread(_llm_chat_messages, messages, 1600, 0.2)
                out, _had_marker = extract_result(raw_out)

                if out and out.strip():
                    break

            out_lines = (out or "").splitlines()
            if not out_lines:
                return JSONResponse({"ok": False, "error": "empty model response"}, status_code=502)

            prompt_line = out_lines[0].strip()
            repl = "\n".join(out_lines[1:])

            if excerpt.endswith("\n") and repl and not repl.endswith("\n"):
                repl += "\n"

            new_lines = lines[:s0] + [repl] + lines[e0:]
            new_content = "".join(new_lines)

            mem_assist = prompt_line if prompt_line else "Prompt: (no prompt line)"
            await _bee_chat_add(mem_user, mem_assist)

            return JSONResponse({"ok": True, "prompt_line": prompt_line, "content": new_content})

        # ---------------- Full file mode ----------------
        user_text = (
            f"User request:\n{prompt}\n\n"
            f"File path: {path}\n"
            f"Current file content:\n{raw}"
        )

        mem_user = f"{name}: {prompt}"

        out = ""
        raw_out = ""

        for attempt in range(max_tries):
            if attempt > 0:
                await asyncio.sleep(2)

            async with bee_chat_lock:
                history_msgs = list(bee_chat)

            messages = [{"role": "system", "content": system_full}] + history_msgs + [{"role": "user", "content": user_text}]
            raw_out = await asyncio.to_thread(_llm_chat_messages, messages, 2400, 0.2)
            out, _had_marker = extract_result(raw_out)

            if out and out.strip():
                break

        out_lines = (out or "").splitlines()
        if not out_lines:
            return JSONResponse({"ok": False, "error": "empty model response"}, status_code=502)

        prompt_line = out_lines[0].strip()
        new_content = "\n".join(out_lines[1:])

        mem_assist = prompt_line if prompt_line else "Prompt: (no prompt line)"
        await _bee_chat_add(mem_user, mem_assist)

        return JSONResponse({"ok": True, "prompt_line": prompt_line, "content": new_content})

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# ---------- Map builder ----------
@app.post("/api/map/rebuild")
async def api_map_rebuild(_: bool = Depends(verify_password_header)):
    cmd1 = [
        "/mnt/data/bee/gps/estimate_heatmaps.py",
        "--db", "/data/singularity.db",
        "--out", "/mnt/data/bee/gps/heatmaps",
        "--print",
    ]

    me_lat = None
    me_lon = None

    try:
        if gps_buf:
            pt = gps_buf[-1]
            la = pt.get("lat")
            lo = pt.get("lon")

            if la is not None and lo is not None:
                la = float(la)
                lo = float(lo)

                if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
                    me_lat = la
                    me_lon = lo
    except Exception:
        pass

    cmd2 = [
        "/mnt/data/bee/gps/render_world_map.py",
        "--summary", "/mnt/data/bee/gps/heatmaps/summary.json",
        "--web", "/mnt/data/bee/web",
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
import sqlite3
import shlex

WIFI_IFACE = os.environ.get("BEE_WIFI_IFACE", "wlan0")
DB_PATH = Path(os.environ.get("BEE_SINGULARITY_DB", "/data/singularity.db"))
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
            mac = m.group(1).lower()
            cur = {"mac": mac, "ssid": "", "rssi_dbm": None, "rssi_pct": None, "scan_src": "iw"}
            continue

        if line.startswith("BSS "):
            continue

        if cur is not None and line.startswith("SSID:"):
            cur["ssid"] = line[5:].strip()
            continue

        if cur is not None and line.startswith("signal:"):
            try:
                cur["rssi_dbm"] = float(line.split()[1])
            except Exception:
                pass
            continue

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
        ts = int(pt.get("ts") or time.time())
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

GPS_ALLOW = os.environ.get("BEE_GPS_ALLOW", "192.168.44.0/24,127.0.0.1/32")
_GPS_NETS = []
for s in GPS_ALLOW.split(","):
    s = s.strip()
    if s:
        _GPS_NETS.append(ipaddress.ip_network(s, strict=False))

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
@app.post("/")
async def gps_ingest(request: Request):
    client_ip = (request.client.host if request.client else "")
    if not _gps_allowed_ip(client_ip):
        raise HTTPException(status_code=403, detail="forbidden")

    if GPS_TOKEN:
        if request.query_params.get("gps_token") != GPS_TOKEN:
            raise HTTPException(status_code=401, detail="bad gps token")

    data: dict = {}

    for k, v in request.query_params.items():
        data[k] = v

    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            j = await request.json()
            if isinstance(j, dict):
                data.update(j)
        except Exception:
            pass

    try:
        form = await request.form()
        for k, v in form.items():
            data[k] = str(v)
    except Exception:
        pass

    typ = str(data.get("_type", "")).strip().lower()
    if typ and typ != "location":
        return JSONResponse({"ok": True, "ignored": typ})

    lat = _first_float(data, ["lat", "latitude", "LAT", "Latitude"])
    lon = _first_float(data, ["lon", "lng", "longitude", "LON", "Longitude"])
    alt = _first_float(data, ["alt", "altitude"])
    acc = _first_float(data, ["acc", "accuracy", "hdop"])
    spd = _first_float(data, ["vel", "speed", "spd"])
    brg = _first_float(data, ["cog", "bearing", "course", "brg"])

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
    asyncio.create_task(scan_and_store(pt))

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
    stdin=slave_fd,
    stdout=slave_fd,
    stderr=slave_fd,
    preexec_fn=os.setsid,
    close_fds=True,
    env=ENV
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
            await asyncio.gather(
                *[
                    ws.send_text(msg)
                    for ws in targets
                    if ws.client_state == WebSocketState.CONNECTED
                ],
                return_exceptions=True
            )

def _reader_thread(loop: asyncio.AbstractEventLoop):
    try:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
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
        with open("index.html", "r", encoding="utf-8", errors="replace") as f:
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
    if not cmd:
        return JSONResponse({"ok": True})
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
                os.killpg(fg_pgid, signal.SIGINT)
                sent = True
            except Exception:
                pass
            if not sent:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                    sent = True
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
        await ws.close(code=1008)
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
