#!/usr/bin/env python3
# Bee OLED UI: Page 0 shows a smiley + IPs on all UP interfaces; K1 opens a tools menu from /mnt/data/bee/bee_tools; K2 up, K3 down, K1 select, last item "Go back".
import os, time, signal, threading, subprocess, socket
import bakebit_128_64_oled as oled
from PIL import Image, ImageFont, ImageDraw

WIDTH, HEIGHT = 128, 64
TOOLS_DIR = "/mnt/data/bee/bee_tools"

# Buttons on NanoHat OLED (wPi numbering)
K1_PIN, K2_PIN, K3_PIN = 0, 2, 3

# ---------- OLED setup ----------
oled.init()
oled.setNormalDisplay()
oled.setHorizontalMode()

image = Image.new("1", (WIDTH, HEIGHT))
draw = ImageDraw.Draw(image)

font14 = ImageFont.truetype("DejaVuSansMono.ttf", 14)
font11 = ImageFont.truetype("DejaVuSansMono.ttf", 11)
fontb14 = ImageFont.truetype("DejaVuSansMono-Bold.ttf", 14)

lock = threading.Lock()
drawing = False

# ---------- UI state ----------
MODE_HOME = "home"
MODE_MENU = "menu"

state = {
    "mode": MODE_HOME,
    "menu_items": [],
    "menu_idx": 0,
}

# ---------- helpers ----------
def run(cmd):
    return subprocess.check_output(cmd, text=True).strip()

def gpio_setup():
    for p in (K1_PIN, K2_PIN, K3_PIN):
        subprocess.call(["gpio", "mode", str(p), "in"])
        subprocess.call(["gpio", "mode", str(p), "up"])

def gpio_read(pin):
    return run(["gpio", "read", str(pin)])

def list_tools():
    items = []
    try:
        for name in sorted(os.listdir(TOOLS_DIR)):
            path = os.path.join(TOOLS_DIR, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                items.append(name)
    except FileNotFoundError:
        pass
    items.append("Go back")
    return items

def ip_lines():
    # show IPv4 for all interfaces that are UP and have an address
    # format: "192.168.1.10"
    out = []
    try:
        txt = run(["ip", "-o", "-4", "addr", "show", "up"])
        for line in txt.splitlines():
            # e.g. "2: wlan0    inet 192.168.1.10/24 brd ..."
            parts = line.split()
            ifname = parts[1]
            cidr = parts[3]
            ip = cidr.split("/")[0]
            out.append(f"{ip}")
    except Exception:
        pass
    if not out:
        out.append("no net :(")
    return out[:4]  # fits the screen

def draw_home():
    draw.rectangle((0, 0, WIDTH, HEIGHT), outline=0, fill=0)
    draw.text((2, 0), ":)", font=fontb14, fill=255)

    y = 16
    for line in ip_lines():
        draw.text((2, y), line, font=font11, fill=255)
        y += 12

def draw_menu():
    items = state["menu_items"]
    idx = state["menu_idx"]

    draw.rectangle((0, 0, WIDTH, HEIGHT), outline=0, fill=0)
    draw.text((2, 0), "Bee Tools", font=fontb14, fill=255)

    # show 4 rows of menu, centered around idx
    start = max(0, idx - 1)
    start = min(start, max(0, len(items) - 4))
    visible = items[start:start+4]

    y = 16
    for i, name in enumerate(visible):
        actual = start + i
        if actual == idx:
            draw.rectangle((0, y-1, WIDTH-1, y+10), outline=255, fill=255)
            draw.text((2, y), name[:20], font=font11, fill=0)
        else:
            draw.text((2, y), name[:20], font=font11, fill=255)
        y += 12

def draw_page():
    global drawing
    with lock:
        if drawing:
            return
        drawing = True

    try:
        if state["mode"] == MODE_HOME:
            draw_home()
        else:
            draw_menu()
        oled.drawImage(image)
    finally:
        with lock:
            drawing = False

# ---------- button actions ----------
def on_k1():
    if state["mode"] == MODE_HOME:
        state["mode"] = MODE_MENU
        state["menu_items"] = list_tools()
        state["menu_idx"] = 0
        draw_page()
        return

    # MODE_MENU select
    choice = state["menu_items"][state["menu_idx"]]
    if choice == "Go back":
        state["mode"] = MODE_HOME
        draw_page()
        return

    # run selected tool (detached), then return home
    tool_path = os.path.join(TOOLS_DIR, choice)
    try:
        subprocess.Popen([tool_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    state["mode"] = MODE_HOME
    draw_page()

def on_k2():
    if state["mode"] != MODE_MENU:
        return
    state["menu_idx"] = (state["menu_idx"] - 1) % len(state["menu_items"])
    draw_page()

def on_k3():
    if state["mode"] != MODE_MENU:
        return
    state["menu_idx"] = (state["menu_idx"] + 1) % len(state["menu_items"])
    draw_page()

# ---------- GPIO polling thread ----------
def buttons_thread():
    gpio_setup()
    last = {K1_PIN: gpio_read(K1_PIN), K2_PIN: gpio_read(K2_PIN), K3_PIN: gpio_read(K3_PIN)}

    while True:
        k1 = gpio_read(K1_PIN)
        k2 = gpio_read(K2_PIN)
        k3 = gpio_read(K3_PIN)

        # falling edge 1 -> 0
        if last[K1_PIN] == "1" and k1 == "0":
            on_k1()
        if last[K2_PIN] == "1" and k2 == "0":
            on_k2()
        if last[K3_PIN] == "1" and k3 == "0":
            on_k3()

        last[K1_PIN], last[K2_PIN], last[K3_PIN] = k1, k2, k3
        time.sleep(0.03)

def main():
    threading.Thread(target=buttons_thread, daemon=True).start()
    draw_page()

    # refresh the home page periodically (IP changes)
    while True:
        if state["mode"] == MODE_HOME:
            draw_page()
        time.sleep(1.0)

if __name__ == "__main__":
    main()
