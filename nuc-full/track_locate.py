#!/usr/bin/env python3
"""Windowed triangulation track for a (GPS-less) moving node, with smoothing.

Static-node triangulation aggregates over days; a mover needs per-window
estimates stitched into a path. For each sliding time window we run the same
chain-walk + weighted geometric median as locate.py (bounded to that window),
then smooth the noisy sequence with a constant-velocity Kalman filter + RTS
smoother. Measurement noise is scaled by how many observers a window had, so
weak windows bend the path less.

Usage:
  ./track_locate.py --target <pubkey-prefix> --window-min 12 --step-min 4 --lookback-hours 24
  ./track_locate.py --target db11 --out track.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import locate as L  # reuse load_config, open_db, load_relay_index, parse_path, haversine_km


def resolve_target(db, prefix: str) -> str | None:
    p = prefix.lower() + "%"
    row = db.execute(
        "SELECT public_key FROM contacts WHERE public_key LIKE ? LIMIT 1", (p,)
    ).fetchone()
    if row:
        return row["public_key"]
    row = db.execute(
        "SELECT source_pk FROM observations WHERE source_pk LIKE ? LIMIT 1", (p,)
    ).fetchone()
    return row["source_pk"] if row else None


def windowed_constraints(db, target_pk, cfg, one_byte_index, full_pk_gps, t0_iso, t1_iso):
    """locate.collect_constraints, bounded to [t0, t1] and using precomputed
    indexes. Emits the same 'first_hop' / 'direct' constraint dicts (carrying
    SNR for directs) so the window estimator can use signal strength as a range
    cue. Relay hops resolve via L.relay_candidates (full-key when available,
    1-byte fallback)."""
    obs = db.execute(
        "SELECT receiver_pk, path_json, snr, rssi FROM observations "
        "WHERE source_pk = ? AND timestamp >= ? AND timestamp < ?",
        (target_pk, t0_iso, t1_iso),
    ).fetchall()
    if not obs:
        return [], 0

    h0_counts: dict = {}
    direct_counts: dict = defaultdict(list)
    observers_used = set()

    for r in obs:
        receiver = r["receiver_pk"].lower()
        recv_gps = full_pk_gps.get(receiver)
        if recv_gps is None:
            continue
        recv_lat, recv_lng = recv_gps
        path = [h for h in L.parse_path(r["path_json"]) if not target_pk.startswith(h)]
        if not path:
            direct_counts[receiver].append({"snr": r["snr"], "rssi": r["rssi"]})
            observers_used.add(receiver)
            continue
        prev_lat, prev_lng = recv_lat, recv_lng
        chain_ok = True
        h0_pk = None; h0_lat = h0_lng = 0.0
        for i in range(len(path) - 1, -1, -1):
            cands = L.relay_candidates(one_byte_index, path[i])
            if not cands:
                chain_ok = False; break
            best = min(cands, key=lambda c: L.haversine_km(c[1], c[2], prev_lat, prev_lng))
            if L.haversine_km(best[1], best[2], prev_lat, prev_lng) > cfg["max_rf_km"]:
                chain_ok = False; break
            if i == 0:
                h0_pk, h0_lat, h0_lng = best
            prev_lat, prev_lng = best[1], best[2]
        if not chain_ok or h0_pk is None:
            continue
        if h0_pk not in h0_counts:
            h0_counts[h0_pk] = {"lat": h0_lat, "lng": h0_lng, "n_paths": 0}
        h0_counts[h0_pk]["n_paths"] += 1
        observers_used.add(receiver)

    constraints = []
    for v in h0_counts.values():
        constraints.append({"kind": "first_hop", "lat": v["lat"], "lng": v["lng"],
                            "n_paths": v["n_paths"], "weight": math.log(v["n_paths"] + 1),
                            "snr": None, "rssi": None})
    for obs_pk, recs in direct_counts.items():
        ola, olg = full_pk_gps[obs_pk]
        best = max(recs, key=lambda x: (x["snr"] if x["snr"] is not None else -99))
        constraints.append({"kind": "direct", "lat": ola, "lng": olg,
                            "n_paths": len(recs), "weight": math.log(len(recs) + 1) * 2,
                            "snr": best["snr"], "rssi": best["rssi"]})
    return constraints, len(observers_used)


# Per-window measurement-noise model (km).
BASE_SIGMA_KM = 1.5    # Profile-A (topology-only) baseline noise at 1 observer
MIN_SIGMA_KM = 0.3     # floor; we never claim sub-300 m from RF alone


def window_estimate(constraints, use_snr=True):
    """Per-window estimate. Returns (lat, lng, sigma_km) or None.

    use_snr is the website toggle. When False (or when there is no usable direct
    SNR) -> Profile A: weighted geometric median of the first-hop relay/receiver
    locations (the legacy behaviour); sigma from observer count.

    When True and direct observations with SNR are present -> terrain-free
    ring/disk MAP: each direct observer contributes an SNR-derived distance
    *ring* (locate.snr_to_distance_km, non-LoS table) so the node is placed at
    the likely range from the hearer rather than snapped onto it; first-hop
    relays add soft disk priors. Minimised from a geometric-median + per-direct
    multistart. sigma_km is the inverse-variance combination of the ring sigmas.
    """
    if not constraints:
        return None
    p0 = L.baseline_geomedian(constraints)
    if p0 is None:
        return None
    directs = ([c for c in constraints if c.get("kind") == "direct" and c.get("snr") is not None]
               if use_snr else [])
    if not directs:
        n = sum(c["n_paths"] for c in constraints)
        return (p0[0], p0[1], max(MIN_SIGMA_KM, BASE_SIGMA_KM / math.sqrt(max(1, n))))

    from scipy.optimize import minimize

    rings = [(c["lat"], c["lng"], *L.snr_to_distance_km(False, c["snr"]), c["weight"]) for c in directs]
    disks = [(c["lat"], c["lng"], L.SIGMA_FIRSTHOP_KM, c["weight"])
             for c in constraints if c.get("kind") == "first_hop"]

    rl = np.array([a[0] for a in rings]); rg = np.array([a[1] for a in rings])
    re = np.array([a[2] for a in rings]); rs = np.array([a[3] for a in rings])
    rw = np.array([a[4] for a in rings])
    has_disk = bool(disks)
    if has_disk:
        ol = np.array([a[0] for a in disks]); og = np.array([a[1] for a in disks])
        osig = np.array([a[2] for a in disks]); ow = np.array([a[3] for a in disks])

    R_KM = 6371.0

    def hav_v(la, lo, lats, lngs):
        p1 = math.radians(la)
        dp = np.radians(lats - la); dl = np.radians(lngs - lo)
        a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(np.radians(lats)) * np.sin(dl / 2) ** 2
        return 2 * R_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

    def cost(v):
        la, lo = float(v[0]), float(v[1])
        d = hav_v(la, lo, rl, rg)
        c = float((rw * ((d - re) ** 2) / (2 * rs * rs)).sum())
        if has_disk:
            d2 = hav_v(la, lo, ol, og)
            c += float((ow * (d2 ** 2) / (2 * osig * osig)).sum())
        return c

    starts = [p0] + [(c["lat"], c["lng"]) for c in directs]
    best, best_c = None, float("inf")
    for s in starts:
        res = minimize(cost, np.array(s, float), method="Nelder-Mead",
                       options={"xatol": 1e-4, "fatol": 1e-6, "maxiter": 400})
        if res.fun < best_c:
            best_c, best = res.fun, res.x

    inv_var = float((rw / (rs * rs)).sum())
    sigma_km = max(MIN_SIGMA_KM, 1.0 / math.sqrt(inv_var)) if inv_var > 0 else BASE_SIGMA_KM
    return (float(best[0]), float(best[1]), sigma_km)


def rts_smooth(meas, times_s, n_obs, sigmas_m=None, max_speed_ms=35.0, base_sigma_m=1500.0):
    """Constant-velocity Kalman forward filter + RTS backward smoother, in metres.

    meas: Nx2 array of (x, y) metres. times_s: N epoch seconds. n_obs: per-point.
    sigmas_m: optional per-point measurement 1-sigma (metres) from the window
    estimator — when given, the filter trusts each window by its own estimated
    accuracy instead of the count-only heuristic. Returns Nx2 smoothed (x, y).
    """
    n = len(meas)
    if n == 0:
        return meas
    H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], float)
    q = (max_speed_ms / 3.0) ** 2  # accel variance driving process noise
    x = np.array([meas[0][0], meas[0][1], 0.0, 0.0])
    P = np.eye(4) * 1e6
    xs_f, Ps_f, xs_p, Ps_p, Fs = [], [], [], [], []
    for k in range(n):
        dt = (times_s[k] - times_s[k - 1]) if k > 0 else 1.0
        dt = max(1.0, min(dt, 3600.0))
        F = np.array([[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], float)
        G = np.array([0.5 * dt * dt, 0.5 * dt * dt, dt, dt])
        Q = np.outer(G, G) * q
        xp = F @ x
        Pp = F @ P @ F.T + Q
        if sigmas_m is not None:
            sigma = max(50.0, float(sigmas_m[k]))
        else:
            sigma = base_sigma_m / math.sqrt(max(1, n_obs[k]))
        R = np.eye(2) * sigma * sigma
        z = np.array(meas[k], float)
        S = H @ Pp @ H.T + R
        K = Pp @ H.T @ np.linalg.inv(S)
        x = xp + K @ (z - H @ xp)
        P = (np.eye(4) - K @ H) @ Pp
        xs_f.append(x.copy()); Ps_f.append(P.copy())
        xs_p.append(xp.copy()); Ps_p.append(Pp.copy()); Fs.append(F)
    # RTS backward pass
    xs = [a.copy() for a in xs_f]
    Ps = [a.copy() for a in Ps_f]
    for k in range(n - 2, -1, -1):
        C = Ps_f[k] @ Fs[k + 1].T @ np.linalg.inv(Ps_p[k + 1])
        xs[k] = xs_f[k] + C @ (xs[k + 1] - xs_p[k + 1])
        Ps[k] = Ps_f[k] + C @ (Ps[k + 1] - Ps_p[k + 1]) @ C.T
    return np.array([[s[0], s[1]] for s in xs])


def build_track(db, cfg, prefix, window_min, step_min, lookback_hours, min_obs, use_snr=True):
    target = resolve_target(db, prefix)
    if not target:
        return {"error": f"no node matching prefix '{prefix}'", "points": []}
    one_byte_index, full_pk_gps = L.load_relay_index(db)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(hours=lookback_hours)
    win = timedelta(minutes=window_min)
    step = timedelta(minutes=step_min)

    raw = []
    t_end = start + win
    while t_end <= now:
        t0 = t_end - win
        cons, nobs = windowed_constraints(
            db, target, cfg, one_byte_index, full_pk_gps,
            t0.strftime("%Y-%m-%d %H:%M:%S"), t_end.strftime("%Y-%m-%d %H:%M:%S"))
        if nobs >= min_obs and cons:
            est = window_estimate(cons, use_snr=use_snr)
            if est:
                lat, lng, sigma_km = est
                mid = t0 + win / 2
                raw.append({"t": mid, "lat": lat, "lng": lng, "n_obs": nobs, "sigma_km": sigma_km})
        t_end += step

    if len(raw) < 2:
        return {"target": target, "window_min": window_min, "points": [],
                "note": "not enough windows with data to build a track"}

    lat0 = sum(p["lat"] for p in raw) / len(raw)
    lng0 = sum(p["lng"] for p in raw) / len(raw)
    mx = 111320.0 * math.cos(math.radians(lat0))
    my = 110540.0
    meas = [[(p["lng"] - lng0) * mx, (p["lat"] - lat0) * my] for p in raw]
    times_s = [p["t"].timestamp() for p in raw]
    nobs = [p["n_obs"] for p in raw]
    sigmas_m = [p.get("sigma_km", BASE_SIGMA_KM) * 1000.0 for p in raw]
    sm = rts_smooth(meas, times_s, nobs, sigmas_m=sigmas_m)

    points = []
    for p, (sxx, syy) in zip(raw, sm):
        points.append({
            "t": p["t"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lat": round(lat0 + syy / my, 6), "lng": round(lng0 + sxx / mx, 6),
            "raw_lat": round(p["lat"], 6), "raw_lng": round(p["lng"], 6),
            "n_obs": p["n_obs"], "sigma_km": round(p.get("sigma_km", BASE_SIGMA_KM), 2),
        })
    return {"target": target, "window_min": window_min, "step_min": step_min,
            "use_snr": bool(use_snr), "n_points": len(points), "points": points}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--target", required=True, help="Pubkey prefix of the moving node")
    ap.add_argument("--window-min", type=float, default=12.0)
    ap.add_argument("--step-min", type=float, default=4.0)
    ap.add_argument("--lookback-hours", type=float, default=24.0)
    ap.add_argument("--min-obs", type=int, default=2)
    ap.add_argument("--no-snr", dest="snr", action="store_false",
                    help="Disable the SNR distance prior (legacy snap-to-hearer estimate)")
    ap.set_defaults(snr=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = L.load_config(args.config)
    db = L.open_db(cfg["db_path"])
    result = build_track(db, cfg, args.target, args.window_min, args.step_min,
                         args.lookback_hours, args.min_obs, use_snr=args.snr)
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"[*] wrote {result.get('n_points', 0)} track points to {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
