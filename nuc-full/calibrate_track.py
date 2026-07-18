#!/usr/bin/env python3
"""Calibrate the GPS-less pathwalking track against a node's REAL GPS history.

Pick a node that *does* share GPS (so we have ground truth in node_positions),
run the same windowed triangulation track_locate.py uses for GPS-less movers,
then compare each estimated point to the node's true position interpolated to
that timestamp. Reports error distribution for the SNR-aware estimator and, for
reference, the legacy snap-to-hearer baseline — so you can see whether the SNR
distance prior actually helps on your network.

Usage:
  ./calibrate_track.py --target <pubkey-prefix> [--lookback-hours 24]
                       [--window-min 12] [--step-min 4] [--min-obs 2]
                       [--max-gap-min 15] [--geojson out.geojson]

Notes:
  - Error is haversine (km) between each estimated point and the true GPS track
    linearly interpolated to the estimate's timestamp. Estimates more than
    --max-gap-min from any true fix are skipped (can't fairly compare).
  - "baseline" = the old Profile-A weighted geometric median of the hearer
    locations (no SNR). "snr" = the new ring/disk estimator. Same windows, same
    smoother, so the only difference is the per-window estimate.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import locate as L
import track_locate as T


def load_truth(db, target_pk, t_lo_iso, t_hi_iso):
    """Time-sorted list of (epoch_seconds, lat, lng) from node_positions."""
    rows = db.execute(
        "SELECT lat, lng, timestamp FROM node_positions "
        "WHERE public_key = ? AND timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp ASC",
        (target_pk, t_lo_iso, t_hi_iso),
    ).fetchall()
    out = []
    for r in rows:
        if r["lat"] is None or r["lng"] is None:
            continue
        t = _parse_ts(r["timestamp"])
        if t is not None:
            out.append((t, float(r["lat"]), float(r["lng"])))
    return out


def _parse_ts(s):
    from datetime import datetime, timezone
    if s is None:
        return None
    s = str(s).strip().replace("T", " ").rstrip("Z")
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def interp_truth(truth, t, max_gap_s):
    """Linear-interpolate the true position to epoch t, or None if no fix is
    within max_gap_s on both sides (or one side for exact endpoints)."""
    if not truth:
        return None
    # exact / before-first / after-last
    if t <= truth[0][0]:
        return (truth[0][1], truth[0][2]) if truth[0][0] - t <= max_gap_s else None
    if t >= truth[-1][0]:
        return (truth[-1][1], truth[-1][2]) if t - truth[-1][0] <= max_gap_s else None
    for i in range(1, len(truth)):
        t0, la0, lo0 = truth[i - 1]
        t1, la1, lo1 = truth[i]
        if t0 <= t <= t1:
            if (t - t0) > max_gap_s and (t1 - t) > max_gap_s:
                return None
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return (la0 + f * (la1 - la0), lo0 + f * (lo1 - lo0))
    return None


def err_stats(errs):
    if not errs:
        return None
    a = np.array(errs)
    return {"n": int(len(a)), "mean": round(float(a.mean()), 3),
            "median": round(float(np.median(a)), 3),
            "p90": round(float(np.percentile(a, 90)), 3), "max": round(float(a.max()), 3)}


def errors_for_track(result, truth, max_gap_s):
    """(raw_errors_km, smoothed_errors_km) comparing each point to true GPS."""
    raw, sm = [], []
    for p in result.get("points", []):
        t = _parse_ts(p["t"])
        if t is None:
            continue
        tp = interp_truth(truth, t, max_gap_s)
        if tp is None:
            continue
        sm.append(L.haversine_km(p["lat"], p["lng"], tp[0], tp[1]))
        if "raw_lat" in p and p["raw_lat"] is not None:
            raw.append(L.haversine_km(p["raw_lat"], p["raw_lng"], tp[0], tp[1]))
    return raw, sm


def summarize(name, errs):
    if not errs:
        print(f"  {name:18s}: no comparable points")
        return
    a = np.array(errs)
    print(f"  {name:18s}: n={len(a):3d}  mean={a.mean():5.2f}  median={np.median(a):5.2f}  "
          f"p90={np.percentile(a,90):5.2f}  max={a.max():5.2f}  km")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--target", required=True, help="Pubkey prefix of a GPS-sharing node")
    ap.add_argument("--window-min", type=float, default=12.0)
    ap.add_argument("--step-min", type=float, default=4.0)
    ap.add_argument("--lookback-hours", type=float, default=24.0)
    ap.add_argument("--min-obs", type=int, default=2)
    ap.add_argument("--max-gap-min", type=float, default=15.0,
                    help="Max time gap (min) to a true GPS fix for a fair comparison")
    ap.add_argument("--geojson", default=None, help="Write estimated + true tracks for inspection")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="Emit a machine-readable JSON result (used by the web app)")
    args = ap.parse_args()

    cfg = L.load_config(args.config)
    db = L.open_db(cfg["db_path"])

    target = T.resolve_target(db, args.target)
    if not target:
        print(f"[!] no node matching prefix '{args.target}'", file=sys.stderr)
        sys.exit(1)

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lo = (now - timedelta(hours=args.lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
    hi = now.strftime("%Y-%m-%d %H:%M:%S")
    truth = load_truth(db, target, lo, hi)
    max_gap_s = args.max_gap_min * 60.0

    print(f"target: {target[:16]}…  true GPS fixes in window: {len(truth)}", file=sys.stderr)
    if len(truth) < 2:
        msg = ("not enough GPS history to calibrate against. Pick a node that shares GPS "
               "and has moved within the lookback window.")
        if args.as_json:
            print(json.dumps({"error": msg}))
            return
        print("[!] " + msg, file=sys.stderr)
        sys.exit(1)

    # Same pipeline + smoother for both; only the per-window estimator differs.
    snr_res = T.build_track(db, cfg, args.target, args.window_min, args.step_min,
                            args.lookback_hours, args.min_obs, use_snr=True)
    base_res = T.build_track(db, cfg, args.target, args.window_min, args.step_min,
                             args.lookback_hours, args.min_obs, use_snr=False)

    raw_b, sm_b = errors_for_track(base_res, truth, max_gap_s)
    raw_s, sm_s = errors_for_track(snr_res, truth, max_gap_s)
    impr = (round(float(np.median(sm_b) - np.median(sm_s)), 3)
            if (sm_b and sm_s) else None)

    result = {
        "target": target,
        "n_truth": len(truth),
        "snr": {"n_points": snr_res.get("n_points"), "raw": err_stats(raw_s), "smoothed": err_stats(sm_s)},
        "baseline": {"n_points": base_res.get("n_points"), "raw": err_stats(raw_b), "smoothed": err_stats(sm_b)},
        "median_improvement_km": impr,
        "tracks": {
            "snr": [[p["lat"], p["lng"]] for p in snr_res.get("points", [])],
            "baseline": [[p["lat"], p["lng"]] for p in base_res.get("points", [])],
            "true": [[la, lo] for _, la, lo in truth],
        },
    }

    if args.as_json:
        print(json.dumps(result))
        return

    print(f"track points: snr={snr_res.get('n_points')}  baseline={base_res.get('n_points')}")
    print("error vs true GPS (km):")
    summarize("baseline raw", raw_b)
    summarize("baseline smoothed", sm_b)
    summarize("snr raw", raw_s)
    summarize("snr smoothed", sm_s)
    if impr is not None:
        print(f"\n  median improvement (baseline→snr, smoothed): {impr:+.2f} km")

    if args.geojson:
        feats = []
        for name, res_, color in (("snr", snr_res, "#ff8c42"), ("baseline", base_res, "#7dd3fc")):
            coords = [[p["lng"], p["lat"]] for p in res_.get("points", [])]
            if len(coords) >= 2:
                feats.append({"type": "Feature", "properties": {"track": name, "stroke": color},
                              "geometry": {"type": "LineString", "coordinates": coords}})
        if len(truth) >= 2:
            feats.append({"type": "Feature", "properties": {"track": "true_gps", "stroke": "#9be564"},
                          "geometry": {"type": "LineString", "coordinates": [[lo, la] for _, la, lo in truth]}})
        Path(args.geojson).write_text(json.dumps({"type": "FeatureCollection", "features": feats}, indent=2))
        print(f"\n  wrote {args.geojson} (open in geojson.io / QGIS to eyeball the tracks)")


if __name__ == "__main__":
    main()
