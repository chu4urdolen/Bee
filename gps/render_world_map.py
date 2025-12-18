#!/usr/bin/env python3
import argparse, json
from pathlib import Path

def _clean_ssid(s):
    if s is None:
        return "nosssid"
    s = str(s)
    if s in ("", "nosssid", "\\\\x00", "\\x00", "\x00", "_x00"):
        return "nosssid"
    s = s.replace("\x00", "").replace("\\\\x00", "").replace("\\x00", "")
    s = "".join(ch if 32 <= ord(ch) <= 126 else "_" for ch in s).strip()
    return s if s else "nosssid"

def _load_summary(summary_path: Path):
    data = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
    aps = []
    for a in data:
        try:
            lat = float(a.get("lat"))
            lon = float(a.get("lon"))
            rad = float(a.get("radius_m"))
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue
            if rad < 0.0:
                continue
            aps.append({
                "mac": (a.get("mac") or "").lower(),
                "ssid": _clean_ssid(a.get("ssid")),
                "lat": lat,
                "lon": lon,
                "radius_m": rad,
            })
        except Exception:
            continue
    return aps

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", required=True, help="heatmaps/summary.json")
    ap.add_argument("--web", default="/mnt/data/bee/web", help="Bee web root (writes map.html + static/aps.json here)")
    ap.add_argument("--me-lat", type=float, default=None, help="Optional current position latitude")
    ap.add_argument("--me-lon", type=float, default=None, help="Optional current position longitude")    
    args = ap.parse_args()

    summary_path = Path(args.summary)
    web_root = Path(args.web)
    static_dir = web_root / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    aps = _load_summary(summary_path)
    me = None
    if args.me_lat is not None and args.me_lon is not None:
        la = float(args.me_lat)
        lo = float(args.me_lon)
        if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
            me = {"lat": la, "lon": lo, "label": "Bee (current)"}    

    # Write the data file the map will fetch
    aps_path = static_dir / "aps.json"
    aps_path.write_text(json.dumps(aps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Write the map page
    map_path = web_root / "map.html"
    map_path.write_text(f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Bee — Singularity Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html, body {{ height:100%; margin:0; background:#0b0f12; }}
    #map {{ height:100%; width:100%; }}
    .hud {{
      position:absolute; top:10px; right:10px; z-index:9999;
      background:rgba(0,0,0,0.65); color:#e7f7f0;
      padding:10px 12px; border-radius:10px; font:13px system-ui;
      border:1px solid rgba(50,255,156,0.25);
    }}
    .hud b {{ color:#32ff9c; }}
  </style>
</head>
<body>
  <div class="hud"><b>Bee</b> · fog circles · loading…</div>
  <div id="map"></div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const hud = document.querySelector(".hud");
    const ME = {json.dumps(me, ensure_ascii=False)};

    async function loadJson(url) {{
      const r = await fetch(url, {{ cache: "no-store" }});
      if (!r.ok) throw new Error(url + " -> " + r.status);
      return await r.json();
    }}

    (async () => {{
      const map = L.map("map");
      L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
        maxZoom: 19, attribution: "&copy; OpenStreetMap"
      }}).addTo(map);

      const aps = await loadJson("/static/aps.json");

      const bounds = L.latLngBounds([]);
      if (ME && Number.isFinite(ME.lat) && Number.isFinite(ME.lon)) {{
        const meLL = L.latLng(ME.lat, ME.lon);
        bounds.extend(meLL);

        L.circleMarker(meLL, {{
          radius: 7,
          color: "red",
          weight: 2,
          fillColor: "red",
          fillOpacity: 0.85
        }}).addTo(map).bindPopup(ME.label || "Bee (current)");
      }}
      let n = 0;

      for (const a of aps) {{
        const lat = Number(a.lat), lon = Number(a.lon), rad = Number(a.radius_m);
        if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(rad)) continue;

        const ll = L.latLng(lat, lon);
        bounds.extend(ll);
        n++;

        const label = `${{a.mac}} · ${{a.ssid}} · r=${{rad.toFixed(1)}}m`;

        // radius -> green intensity (smaller radius = darker green)
        // clamp range so one crazy value doesn't ruin the palette
        const rMin = 5.0, rMax = 60.0;
        const t = Math.max(0, Math.min(1, (rad - rMin) / (rMax - rMin))); // 0=small, 1=big
        const light = 25 + Math.round(t * 45); // 25%..70% (dark..light)
        const color = `hsl(140 100% ${{light}}%)`;

        // fixed-size dot (meters) so we see points, not fog rings
        const dotR = 4.0;
        L.circleMarker(ll, {{
          radius: dotR,
          color: color,
          weight: 2,
          fillColor: color,
          fillOpacity: 0.9
        }}).addTo(map).bindPopup(label);        
      }}

      hud.textContent = "Bee · APs: " + n + (ME ? " · me" : "");

      if (n === 0 || !bounds.isValid()) {{
        map.setView([0,0], 2);
      }} else if (n === 1) {{
        map.setView(bounds.getCenter(), 18);
      }} else {{
        map.fitBounds(bounds, {{ padding: [40,40] }});
      }}
    }})().catch(e => {{
      hud.textContent = "Bee · map load failed";
      alert("Map load failed: " + e);
      console.error(e);
    }});
  </script>
</body>
</html>
""", encoding="utf-8")

    print(str(map_path))
    print(str(aps_path))

if __name__ == "__main__":
    main()
