#!/usr/bin/env python3
"""
estimate_heatmaps.py

Hard-overlap "ink disks" estimator per (mac, ssid).

Each observation i has:
  sample point p_i = (lat_i, lon_i)
  RSSI rssi_i (dBm)

We DO NOT assume a path-loss model. We only map RSSI -> radius by ranking within
the kept sample set:
  strongest RSSI -> r_min meters
  weakest   RSSI -> r_max meters
(linear interpolation)

Then we search a local meter grid and score each cell x by combining all samples:

  s_i(x) = clamp(1 - dist(x, p_i)/R_i, 0, 1)      # linear fade disk
  S(x)   = min_i s_i(x)                            # "ALL overlap" (AND)

If strict overlap is empty (max S ~ 0), we auto-fallback to a relaxed combiner:
  S(x) = quantile(s_i(x), q)  with q default 0.2
(meaning: "most samples overlap", still pretty strict)

Output:
  summary.json: [{mac, ssid, lat, lon, radius_m, score, mode, n_used}, ...]

Usage:
  ./estimate_heatmaps.py --db /mnt/data/bee/gps/singularity.db --out /mnt/data/bee/gps/heatmaps --print

Important knobs:
  --r-min / --r-max : radius range in meters for strongest..weakest RSSI
  --grid-step       : meters per grid step (smaller = slower, more precise)
"""

import argparse, json, math, re, sqlite3
from pathlib import Path
from collections import defaultdict

def sanitize_ssid(ssid: str) -> str:
    s = (ssid or "").replace("\x00", "").strip()
    if not s:
        return ""
    s = re.sub(r"[^\x20-\x7E]+", "", s)
    return s[:64]

def latlon_to_xy_m(lat: float, lon: float, lat0: float, lon0: float):
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    x = (lon - lon0) * m_per_deg_lon
    y = (lat - lat0) * m_per_deg_lat
    return x, y

def xy_m_to_latlon(x: float, y: float, lat0: float, lon0: float):
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    lat = lat0 + (y / m_per_deg_lat)
    lon = lon0 + (x / m_per_deg_lon if m_per_deg_lon != 0 else 0.0)
    return lat, lon

def fetch_rows_iw(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        sql = """
          SELECT mac, COALESCE(ssid,'') AS ssid, ts, lat, lon, rssi_dbm
          FROM wifi_obs
          WHERE lat IS NOT NULL AND lon IS NOT NULL AND rssi_dbm IS NOT NULL
            AND scan_src='iw'
        """
        return con.execute(sql).fetchall()
    finally:
        con.close()

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def rssi_to_radius_linear(rssi_list, r_min, r_max):
    """
    Map RSSI values to radii in [r_min, r_max] by linear normalization within this AP's kept set.
    strongest (max) -> r_min
    weakest   (min) -> r_max
    """
    rmax = max(rssi_list)
    rminv = min(rssi_list)
    denom = (rmax - rminv)
    out = []
    for r in rssi_list:
        if abs(denom) < 1e-9:
            t = 0.5
        else:
            # strongest: t=0, weakest: t=1
            t = (rmax - r) / denom
        out.append(r_min + clamp(t, 0.0, 1.0) * (r_max - r_min))
    return out

def quantile(vals, q):
    if not vals:
        return 0.0
    q = clamp(float(q), 0.0, 1.0)
    s = sorted(vals)
    idx = int(round(q * (len(s) - 1)))
    return s[idx]

def estimate_one(obs_xy_rssi, lat0, lon0, r_min, r_max, grid_step, grid_max, q_fallback):
    """
    obs_xy_rssi: list of (x_m, y_m, rssi_dbm)
    returns: (best_lat, best_lon, radius_m, score, mode)
    """
    xs = [x for x,_,_ in obs_xy_rssi]
    ys = [y for _,y,_ in obs_xy_rssi]
    rs = [r for *_,r in obs_xy_rssi]

    radii = rssi_to_radius_linear(rs, r_min, r_max)
    maxR = max(radii) if radii else r_max

    minx = min(xs) - maxR
    maxx = max(xs) + maxR
    miny = min(ys) - maxR
    maxy = max(ys) + maxR

    spanx = maxx - minx
    spany = maxy - miny

    step = float(grid_step)
    if step <= 0:
        step = 3.0

    # keep grid sane: if too big, coarsen step
    nx = max(1, int(math.ceil(spanx / step)) + 1)
    ny = max(1, int(math.ceil(spany / step)) + 1)
    max_side = max(nx, ny)
    if max_side > grid_max:
        scale = max_side / float(grid_max)
        step *= scale
        nx = max(1, int(math.ceil(spanx / step)) + 1)
        ny = max(1, int(math.ceil(spany / step)) + 1)

    # prepack for speed
    pts = [(x, y, R) for (x,y,_), R in zip(obs_xy_rssi, radii)]

    def score_at(x, y, mode, q=None):
        vals = []
        for (px, py, R) in pts:
            dx = x - px
            dy = y - py
            d = math.hypot(dx, dy)
            si = 1.0 - (d / R) if R > 1e-9 else 0.0
            if si <= 0.0:
                if mode == "min":
                    return 0.0
                vals.append(0.0)
            else:
                vals.append(si if si < 1.0 else 1.0)
        if mode == "min":
            return min(vals) if vals else 0.0
        return quantile(vals, q if q is not None else 0.2)

    # 1) strict AND overlap
    bestS = -1.0
    bestXY = (0.0, 0.0)
    for iy in range(ny):
        y = miny + iy * step
        for ix in range(nx):
            x = minx + ix * step
            S = score_at(x, y, mode="min")
            if S > bestS:
                bestS = S
                bestXY = (x, y)

    mode = "min"
    if bestS <= 1e-6:
        # 2) fallback: still overlap-ish, but tolerant to 1-2 bad samples
        bestS = -1.0
        bestXY = (0.0, 0.0)
        for iy in range(ny):
            y = miny + iy * step
            for ix in range(nx):
                x = minx + ix * step
                S = score_at(x, y, mode="qmin", q=q_fallback)
                if S > bestS:
                    bestS = S
                    bestXY = (x, y)
        mode = f"qmin{q_fallback:.2f}"

    bx, by = bestXY
    best_lat, best_lon = xy_m_to_latlon(bx, by, lat0, lon0)

    # radius: contour around best point (half-max region, conservative)
    thr = bestS * 0.5
    rad = 0.0
    if thr > 0:
        for iy in range(ny):
            y = miny + iy * step
            for ix in range(nx):
                x = minx + ix * step
                S = score_at(x, y, mode="min" if mode=="min" else "qmin", q=q_fallback)
                if S >= thr:
                    rad = max(rad, math.hypot(x - bx, y - by))
    if rad <= 0.0:
        rad = float(r_min)

    return best_lat, best_lon, float(rad), float(bestS), mode

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="Path to singularity.db")
    ap.add_argument("--out", default="/mnt/data/bee/gps/heatmaps", help="Output directory")
    ap.add_argument("--trim", type=float, default=0.35, help="Keep strongest fraction per AP (0..1]")
    ap.add_argument("--min-samples", type=int, default=8, help="Minimum samples per AP to emit")
    ap.add_argument("--r-min", type=float, default=8.0, help="Radius for strongest RSSI (meters)")
    ap.add_argument("--r-max", type=float, default=80.0, help="Radius for weakest RSSI (meters)")
    ap.add_argument("--grid-step", type=float, default=3.0, help="Grid step in meters")
    ap.add_argument("--grid-max", type=int, default=220, help="Max grid cells per side (auto-coarsen)")
    ap.add_argument("--q", type=float, default=0.20, help="Fallback quantile for qmin mode (0..1)")
    ap.add_argument("--print", action="store_true", help="Print CSV lines to stdout")
    args = ap.parse_args()

    trim = max(0.01, min(args.trim, 1.0))
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    rows = fetch_rows_iw(args.db)

    groups = defaultdict(list)
    for r in rows:
        mac = (r["mac"] or "").strip().lower()
        ssid = sanitize_ssid(r["ssid"] or "")
        if len(mac) != 17:
            continue
        groups[(mac, ssid)].append((float(r["lat"]), float(r["lon"]), float(r["rssi_dbm"])))

    summary = []
    for (mac, ssid), obs in groups.items():
        if len(obs) < args.min_samples:
            continue

        obs.sort(key=lambda t: t[2], reverse=True)  # strongest first
        keep_n = max(args.min_samples, int(math.ceil(len(obs) * trim)))
        obs_k = obs[:keep_n]

        # local origin: average of kept points (stable)
        lat0 = sum(o[0] for o in obs_k) / len(obs_k)
        lon0 = sum(o[1] for o in obs_k) / len(obs_k)

        obs_xy_rssi = []
        for la, lo, rs in obs_k:
            x, y = latlon_to_xy_m(la, lo, lat0, lon0)
            obs_xy_rssi.append((x, y, rs))

        est_lat, est_lon, radius_m, score, mode = estimate_one(
            obs_xy_rssi, lat0, lon0,
            r_min=float(args.r_min), r_max=float(args.r_max),
            grid_step=float(args.grid_step),
            grid_max=int(args.grid_max),
            q_fallback=float(args.q),
        )

        summary.append({
            "mac": mac,
            "ssid": ssid,
            "lat": est_lat,
            "lon": est_lon,
            "radius_m": radius_m,
            "score": score,
            "mode": mode,
            "n_used": len(obs_k),
        })

    # stable order: smaller radius first, higher score first
    summary.sort(key=lambda d: (d["radius_m"], -d.get("score", 0.0)))

    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    if args.print:
        for s in summary:
            ss = (s.get("ssid") or "").replace('"', "'")
            print(f'{s["mac"]},"{ss}",{s["lat"]:.8f},{s["lon"]:.8f},{s["radius_m"]:.2f},{s.get("score",0):.3f},{s.get("mode","")}')

if __name__ == "__main__":
    main()
