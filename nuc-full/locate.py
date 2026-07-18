#!/usr/bin/env python3
"""
Triangulate a MeshCore source from observations stored by collector.py.

Usage:
  ./locate.py --target <pubkey-prefix>     # locate one
  ./locate.py --validate                   # run against every GPS-known target,
                                           # report per-target error and aggregate

Algorithm: multi-anchor chain-walk + weighted geometric median (Weiszfeld).

Optional terrain mode (when [terrain] dem_dir is configured): adds line-of-
sight + Fresnel-zone analysis using a Copernicus GLO-30 DEM. Direct (0-hop)
observations get LoS-conditional Gaussian σ from an empirical SNR→distance
table, tightening the answer when high-SNR LoS observations exist. First-hop
chain-walk relays use a constant σ (matching baseline behaviour) when no
direct observations exist, so terrain mode never hurts targets where it
has no signal. See README.md "Terrain-aware mode" for details.
"""
from __future__ import annotations

import argparse
import configparser
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

WEISZFELD_ITERS = 200

# ─── Terrain-mode constants (only used when DEM is configured) ───────────────
SIGMA_FIRSTHOP_KM = 15.0       # constant σ → behaves like baseline
STRONG_SNR_THRESHOLD = 5.0
RING_TRILATERATION_MIN = 3     # need ≥3 strong-LoS direct for ring multilateration


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_config(path: str = "config.ini") -> dict:
    p = configparser.ConfigParser()
    if not Path(path).exists():
        print(f"[!] Config file '{path}' not found. Copy config.example.ini.", file=sys.stderr)
        sys.exit(2)
    p.read(path)
    return {
        "db_path": p.get("storage", "db_path", fallback="./meshcore_data.db"),
        "max_rf_km": p.getfloat("locator", "max_rf_km", fallback=35.0),
        "days_lookback": p.getint("locator", "days_lookback", fallback=14),
        "min_observers": p.getint("locator", "min_observers", fallback=2),
        "dem_dir": p.get("terrain", "dem_dir", fallback="").strip() or None,
        "default_antenna_m": p.getfloat("terrain", "default_antenna_m", fallback=5.0),
        "freq_mhz": p.getfloat("terrain", "freq_mhz", fallback=915.0),
    }


def open_db(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def load_relay_index(db: sqlite3.Connection) -> tuple[dict, dict]:
    """Returns (one_byte_index, full_pubkey_gps) for chain-walk lookups."""
    one_byte: dict[str, list] = defaultdict(list)
    full: dict[str, tuple[float, float]] = {}
    rows = db.execute(
        "SELECT public_key, lat, lng FROM contacts "
        "WHERE lat IS NOT NULL AND lat != 0 AND lng IS NOT NULL AND lng != 0"
    ).fetchall()
    for r in rows:
        pk = r["public_key"].lower()
        full[pk] = (r["lat"], r["lng"])
        one_byte[pk[:2]].append((pk, r["lat"], r["lng"]))
    return one_byte, full


def relay_candidates(one_byte_index: dict, hop: str) -> list:
    """Relay candidates for a single path hop, backwards-compatible across hop
    widths.

    MeshCore packets encode 1, 2 or 3 bytes per hop (path_meta bits 6-7), so a
    path element can be 2, 4 or 6 hex chars. The relay index buckets relays by
    their first byte only. A 1-byte hop (2 hex) therefore returns that whole
    first-byte bucket — exactly the legacy behaviour. A wider hop is narrowed to
    relays whose pubkey actually starts with the full hop prefix: far less
    ambiguous, and it rescues multi-byte-per-hop packets that the old
    1-byte-only `dict.get(hop)` silently dropped (it missed → chain broken).
    """
    bucket = one_byte_index.get(hop[:2], [])
    if len(hop) <= 2:
        return bucket
    return [c for c in bucket if c[0].startswith(hop)]


def parse_path(path_json: str | None) -> list[str]:
    if not path_json:
        return []
    s = path_json.strip()
    if not s or s == "[]":
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
        except json.JSONDecodeError:
            return []
        return [str(x).lower() for x in arr if isinstance(x, str)]
    return [seg.strip().lower() for seg in s.split(",") if seg.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Terrain-mode helpers (no-ops when terrain isn't configured)
# ─────────────────────────────────────────────────────────────────────────────

def sigma_for_direct(chord_clear: bool, snr: float | None) -> float:
    """Empirically calibrated σ (km) for a direct (0-hop) observation.

    Calibration source: 22 385 direct observations of 798 GPS-known multi-
    observer targets in a real-world MQTT-broker dataset, binned by SNR and
    chord-clear status. See README "Terrain-aware mode" for the table.
    """
    s = -99.0 if snr is None else snr
    if chord_clear:
        if s >= 10:  return 1.0
        if s >=  5:  return 3.0
        if s >=  0:  return 8.0
        if s >= -5:  return 13.0
        return 18.0
    else:
        if s >= 10:  return 6.0
        if s >=  5:  return 6.0
        if s >=  0:  return 11.0
        if s >= -5:  return 14.0
        return 18.0


def snr_to_distance_km(chord_clear: bool, snr: float) -> tuple[float, float]:
    """Empirical SNR → (expected_distance_km, sigma_km). Used by ring trilateration."""
    if chord_clear:
        if snr >= 10:  return 0.47, 1.0
        if snr >=  5:  return 1.56, 3.0
        if snr >=  0:  return 6.37, 5.0
        if snr >= -5:  return 11.59, 8.0
        return 22.61, 15.0
    else:
        if snr >= 10:  return 4.84, 4.0
        if snr >=  5:  return 4.50, 4.0
        if snr >=  0:  return 10.42, 7.0
        if snr >= -5:  return 12.09, 9.0
        return 22.63, 15.0


# ─────────────────────────────────────────────────────────────────────────────
# Constraint collection
# ─────────────────────────────────────────────────────────────────────────────


def collect_constraints(
    db: sqlite3.Connection,
    target_pk: str,
    cfg: dict,
) -> tuple[list[dict], int, int]:
    """Walk every observation of target_pk and produce a list of constraints.

    Each constraint dict has:
      kind        : 'first_hop' or 'direct'
      lat, lng    : anchor location (relay or receiving radio)
      n_paths     : number of paths/observations corroborating this anchor
      weight      : log(n_paths + 1), with 2x bonus for direct anchors
      snr / rssi  : best-of-window per direct observer (None for first_hop)
    """
    obs = db.execute(
        f"""
        SELECT receiver_pk, path_json, snr, rssi
        FROM observations
        WHERE source_pk = ?
          AND timestamp >= datetime('now','-{cfg['days_lookback']} days')
        """,
        (target_pk,),
    ).fetchall()
    if not obs:
        return [], 0, 0

    one_byte_index, full_pk_gps = load_relay_index(db)

    h0_counts: dict[str, dict] = {}
    direct_counts: dict[str, list] = defaultdict(list)
    observers_used: set[str] = set()
    paths_used = 0

    for r in obs:
        receiver = r["receiver_pk"].lower()
        recv_gps = full_pk_gps.get(receiver)
        if recv_gps is None:
            continue
        recv_lat, recv_lng = recv_gps

        path = parse_path(r["path_json"])
        path = [h for h in path if not target_pk.startswith(h)]

        if not path:
            direct_counts[receiver].append({"snr": r["snr"], "rssi": r["rssi"]})
            observers_used.add(receiver)
            paths_used += 1
            continue

        prev_lat, prev_lng = recv_lat, recv_lng
        chain_ok = True
        h0_pk = None; h0_lat = h0_lng = 0.0
        for i in range(len(path) - 1, -1, -1):
            byte_key = path[i]
            cands = relay_candidates(one_byte_index, byte_key)
            if not cands:
                chain_ok = False; break
            best = min(cands, key=lambda c: haversine_km(c[1], c[2], prev_lat, prev_lng))
            d = haversine_km(best[1], best[2], prev_lat, prev_lng)
            if d > cfg["max_rf_km"]:
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
        paths_used += 1

    constraints: list[dict] = []
    for h0_pk, v in h0_counts.items():
        constraints.append({
            "kind": "first_hop", "lat": v["lat"], "lng": v["lng"],
            "n_paths": v["n_paths"],
            "weight": math.log(v["n_paths"] + 1),
            "snr": None, "rssi": None,
        })
    for obs_pk, recs in direct_counts.items():
        ola, olg = full_pk_gps[obs_pk]
        best = max(recs, key=lambda r: (r["snr"] if r["snr"] is not None else -99))
        constraints.append({
            "kind": "direct", "lat": ola, "lng": olg,
            "n_paths": len(recs),
            "weight": math.log(len(recs) + 1) * 2,
            "snr": best["snr"], "rssi": best["rssi"],
        })

    return constraints, len(observers_used), paths_used


# ─────────────────────────────────────────────────────────────────────────────
# Estimators
# ─────────────────────────────────────────────────────────────────────────────


def baseline_geomedian(constraints: list[dict], max_iters: int = WEISZFELD_ITERS):
    """Weighted geometric median (Weiszfeld). Production baseline."""
    if not constraints:
        return None
    x = sum(c["lat"] for c in constraints) / len(constraints)
    y = sum(c["lng"] for c in constraints) / len(constraints)
    for _ in range(max_iters):
        nx = ny = den = 0.0
        for p in constraints:
            d = max(haversine_km(x, y, p["lat"], p["lng"]), 0.05)
            w = math.log(p["n_paths"] + 1) / d
            nx += p["lat"] * w; ny += p["lng"] * w; den += w
        nx, ny = nx / den, ny / den
        if abs(nx - x) < 1e-7 and abs(ny - y) < 1e-7:
            break
        x, y = nx, ny
    return x, y


def _terrain_map_step(constraints, sigmas, x0, y0, max_iters=200):
    """Inverse-variance weighted geomedian-ish iteration."""
    import numpy as np
    lats = np.array([c["lat"] for c in constraints])
    lngs = np.array([c["lng"] for c in constraints])
    weights = np.array([c["weight"] for c in constraints])
    iv = weights / (sigmas * sigmas)
    R = 6371.0
    x, y = x0, y0
    for _ in range(max_iters):
        p1 = math.radians(x)
        dp = np.radians(lats - x)
        dl = np.radians(lngs - y)
        a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(np.radians(lats)) * np.sin(dl / 2) ** 2
        d = 2 * R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
        d_safe = np.maximum(d, 0.05)
        u = iv / d_safe
        s = u.sum()
        nx = float((u * lats).sum() / s)
        ny = float((u * lngs).sum() / s)
        if abs(nx - x) < 1e-7 and abs(ny - y) < 1e-7:
            break
        x, y = nx, ny
    return x, y


def _terrain_locate(constraints, los_engine, cfg):
    """Hybrid terrain-aware locator (Profile A/B/C dispatch)."""
    if not constraints:
        return None, "empty"

    n_direct = sum(1 for c in constraints if c["kind"] == "direct")
    if n_direct == 0:
        # Profile A — pure baseline. LoS classification of source↔first-hop
        # relays isn't a useful distance signal because the SNR we observe
        # is the LAST hop, not the first. Falling through to baseline avoids
        # adding noise.
        return baseline_geomedian(constraints), "A"

    # Profile B/C — direct observations are present.
    p0 = baseline_geomedian(constraints)
    if p0 is None:
        return None, "fail"

    # Identify "strong-LoS direct" observations using LoS at the baseline
    # estimate. With ≥3 of these, ring trilateration becomes well-posed.
    strong_los_directs = []
    for c in constraints:
        if c["kind"] != "direct": continue
        snr = c.get("snr")
        if snr is None or snr < STRONG_SNR_THRESHOLD: continue
        res = los_engine.los(p0[0], p0[1], c["lat"], c["lng"])
        if res.clearance_min_m >= 0:
            strong_los_directs.append(c)

    if len(strong_los_directs) >= RING_TRILATERATION_MIN:
        return _profile_c(strong_los_directs, constraints, los_engine, p0, cfg), "C"
    else:
        return _profile_b(constraints, los_engine, p0, cfg), "B"


def _profile_b(constraints, los_engine, p0, cfg):
    """LoS-conditional Gaussian MAP. σ for direct anchors comes from the
    SNR-binned table; first-hop relays use a constant σ so they collectively
    behave like baseline (no terrain noise injection)."""
    import numpy as np
    p = p0
    for it in range(2):
        sigmas = np.empty(len(constraints))
        for i, c in enumerate(constraints):
            if c["kind"] == "first_hop":
                sigmas[i] = SIGMA_FIRSTHOP_KM
            else:
                res = los_engine.los(p[0], p[1], c["lat"], c["lng"])
                chord = res.clearance_min_m >= 0
                sigmas[i] = sigma_for_direct(chord, c.get("snr"))
        nxy = _terrain_map_step(constraints, sigmas, p[0], p[1])
        moved = haversine_km(p[0], p[1], nxy[0], nxy[1])
        p = nxy
        if moved < 0.05: break
    return p


def _profile_c(strong_directs, all_constraints, los_engine, p0, cfg):
    """Ring trilateration: each strong-LoS direct contributes a ring of
    radius d_expected from its observer (per SNR table). Other constraints
    fall back to soft-Gaussian disk priors. L-BFGS-B from multistart."""
    import numpy as np
    from scipy.optimize import minimize

    rings = []
    others = []
    for c in all_constraints:
        if c in strong_directs:
            d_exp, d_sig = snr_to_distance_km(True, c["snr"])
            rings.append((c["lat"], c["lng"], d_exp, d_sig, c["weight"]))
        elif c["kind"] == "direct":
            res = los_engine.los(p0[0], p0[1], c["lat"], c["lng"])
            chord = res.clearance_min_m >= 0
            sigma = sigma_for_direct(chord, c.get("snr"))
            others.append((c["lat"], c["lng"], sigma, c["weight"]))
        else:
            others.append((c["lat"], c["lng"], SIGMA_FIRSTHOP_KM, c["weight"]))

    rl = np.array([a[0] for a in rings])
    rg = np.array([a[1] for a in rings])
    re = np.array([a[2] for a in rings])
    rs = np.array([a[3] for a in rings])
    rw = np.array([a[4] for a in rings])
    ol = np.array([a[0] for a in others]) if others else np.empty(0)
    og = np.array([a[1] for a in others]) if others else np.empty(0)
    os_ = np.array([a[2] for a in others]) if others else np.empty(0)
    ow = np.array([a[3] for a in others]) if others else np.empty(0)

    R_KM = 6371.0
    def hav_v(la, lo, lats, lngs):
        p1 = math.radians(la)
        dp = np.radians(lats - la)
        dl = np.radians(lngs - lo)
        a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(np.radians(lats)) * np.sin(dl / 2) ** 2
        return 2 * R_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

    def cost(la, lo):
        d_ring = hav_v(la, lo, rl, rg)
        rc = float((rw * ((d_ring - re) ** 2) / (2 * rs * rs)).sum())
        if others:
            d_other = hav_v(la, lo, ol, og)
            oc = float((ow * (d_other ** 2) / (2 * os_ * os_)).sum())
        else:
            oc = 0.0
        return rc + oc

    starts = [p0] + [(c["lat"], c["lng"]) for c in strong_directs]
    pad = 0.5
    all_lats = list(rl) + list(ol)
    all_lngs = list(rg) + list(og)
    bounds = [
        (min(all_lats) - pad, max(all_lats) + pad),
        (min(all_lngs) - pad, max(all_lngs) + pad),
    ]
    best = None; best_f = float("inf")
    for s in starts:
        try:
            res = minimize(
                lambda v: cost(v[0], v[1]),
                x0=np.array(s), method="L-BFGS-B", bounds=bounds,
                options={"ftol": 1e-8, "gtol": 1e-6, "maxiter": 200},
            )
            if res.fun < best_f:
                best_f = float(res.fun)
                best = (float(res.x[0]), float(res.x[1]))
        except Exception:
            continue
    return best if best is not None else p0


# ─────────────────────────────────────────────────────────────────────────────
# Top-level locate()
# ─────────────────────────────────────────────────────────────────────────────


def locate(
    db: sqlite3.Connection,
    target_prefix: str,
    cfg: dict,
    los_engine=None,
) -> dict | None:
    target_prefix = target_prefix.lower()
    row = db.execute(
        "SELECT public_key, name, role, lat, lng FROM contacts "
        "WHERE public_key LIKE ? LIMIT 1",
        (target_prefix + "%",),
    ).fetchone()
    if row is None:
        return None
    target_pk = row["public_key"].lower()
    target_name = row["name"] or "?"
    actual_lat = row["lat"]; actual_lng = row["lng"]

    constraints, n_observers, paths_used = collect_constraints(db, target_pk, cfg)
    if not constraints and paths_used == 0:
        # Could be no observations at all OR no chains survived
        any_rows = db.execute(
            "SELECT 1 FROM observations WHERE source_pk = ? LIMIT 1",
            (target_pk,),
        ).fetchone()
        return {"name": target_name, "no_data": True} if not any_rows \
                else {"name": target_name, "no_chain": True}
    if not constraints:
        return {"name": target_name, "no_chain": True}

    n_h0 = sum(1 for c in constraints if c["kind"] == "first_hop")
    n_direct = sum(1 for c in constraints if c["kind"] == "direct")

    if los_engine is not None:
        est, profile = _terrain_locate(constraints, los_engine, cfg)
    else:
        est = baseline_geomedian(constraints)
        profile = "baseline"
    if est is None:
        return {"name": target_name, "no_chain": True}
    x, y = est

    result: dict = {
        "name": target_name,
        "estimate": (round(x, 5), round(y, 5)),
        "n_observers_used": n_observers,
        "n_paths_used": paths_used,
        "n_h0_candidates": n_h0,
        "n_direct": n_direct,
        "profile": profile,
    }
    if actual_lat is not None and actual_lng is not None and actual_lat != 0:
        result["actual"] = (actual_lat, actual_lng)
        result["error_km"] = round(haversine_km(x, y, actual_lat, actual_lng), 2)
    return result


def _build_los_engine(cfg: dict):
    if not cfg.get("dem_dir"):
        return None
    try:
        from terrain import TerrainLoS, HAS_TERRAIN_DEPS
    except ImportError as e:
        print(f"[!] terrain module import failed: {e}; running baseline-only", file=sys.stderr)
        return None
    if not HAS_TERRAIN_DEPS:
        print("[!] rasterio/numpy not installed — install with "
              "`pip install rasterio numpy scipy` to enable terrain mode",
              file=sys.stderr)
        return None
    if not Path(cfg["dem_dir"]).is_dir():
        print(f"[!] dem_dir '{cfg['dem_dir']}' not found — run "
              "`./download_dem.py --auto` first; running baseline-only",
              file=sys.stderr)
        return None
    return TerrainLoS(
        cfg["dem_dir"],
        default_antenna_m=cfg.get("default_antenna_m", 5.0),
        freq_mhz=cfg.get("freq_mhz", 915.0),
    )


def cmd_target(db, prefix: str, cfg: dict, los_engine) -> None:
    r = locate(db, prefix, cfg, los_engine)
    if r is None:
        print(f"[!] No contact found matching prefix '{prefix}'")
        return
    if r.get("no_data"):
        print(f"[!] No observations of {r['name']} in last {cfg['days_lookback']} days")
        return
    if r.get("no_chain"):
        print(f"[!] {r['name']}: every chain rejected (no GPS-known relays in path, "
              f"or chains exceed max_rf_km={cfg['max_rf_km']})")
        return
    lat, lng = r["estimate"]
    print(f"  Target          : {r['name']}")
    print(f"  Estimate        : ({lat}, {lng})")
    print(f"  Profile         : {r['profile']}")
    print(f"  Observers used  : {r['n_observers_used']}")
    print(f"  Paths used      : {r['n_paths_used']}")
    print(f"  H0 candidates   : {r['n_h0_candidates']}")
    print(f"  Direct heard    : {r['n_direct']}")
    if "error_km" in r:
        print(f"  Actual          : {r['actual']}")
        print(f"  Error           : {r['error_km']} km")
    print(f"  Google Maps     : https://maps.google.com/?q={lat:.5f},{lng:.5f}")


def cmd_validate(db, cfg: dict, los_engine) -> None:
    rows = db.execute(
        f"""
        SELECT o.source_pk AS pk, COUNT(DISTINCT o.receiver_pk) AS n_obs,
               c.name, c.role, c.lat, c.lng
        FROM observations o
        JOIN contacts c ON c.public_key = o.source_pk
        WHERE o.source_pk IS NOT NULL
          AND c.lat IS NOT NULL AND c.lat != 0
          AND o.timestamp >= datetime('now','-{cfg['days_lookback']} days')
        GROUP BY o.source_pk
        HAVING n_obs >= {cfg['min_observers']}
        ORDER BY n_obs DESC
        """
    ).fetchall()

    mode = "TERRAIN" if los_engine is not None else "baseline"
    print(f"Validating {mode} algorithm against {len(rows)} GPS-known targets "
          f"with ≥{cfg['min_observers']} observers")
    print()
    print(f"  {'name':<30} {'obs':>4} {'used':>4} {'h0':>3} {'dir':>3} "
          f"{'profile':<8} {'err_km':>7}")
    print(f"  {'-'*30} {'-'*4} {'-'*4} {'-'*3} {'-'*3} {'-'*8} {'-'*7}")

    errs: list[float] = []
    profile_breakdown: dict[str, list[float]] = defaultdict(list)
    for c in rows:
        r = locate(db, c["pk"], cfg, los_engine)
        if not r or r.get("no_chain") or r.get("no_data"):
            print(f"  {(c['name'] or '?')[:30]:<30} {c['n_obs']:>4} "
                  f"{0:>4} {0:>3} {0:>3} {'-':<8} {'-':>7}")
            continue
        err = haversine_km(*r["estimate"], c["lat"], c["lng"])
        errs.append(err)
        profile_breakdown[r["profile"]].append(err)
        print(f"  {(c['name'] or '?')[:30]:<30} {c['n_obs']:>4} "
              f"{r['n_observers_used']:>4} {r['n_h0_candidates']:>3} {r['n_direct']:>3} "
              f"{r['profile']:<8} {err:>7.2f}")

    if errs:
        s = sorted(errs)
        print()
        print(f"=== Aggregate over {len(errs)} targets ===")
        print(f"  median err : {statistics.median(s):.2f} km")
        print(f"  mean err   : {statistics.mean(s):.2f} km")
        print(f"  ≤  1 km    : {sum(1 for e in s if e <= 1)}")
        print(f"  ≤  5 km    : {sum(1 for e in s if e <= 5)}")
        print(f"  ≤ 10 km    : {sum(1 for e in s if e <= 10)}")
        if los_engine is not None and len(profile_breakdown) > 1:
            print()
            print("  Per-profile breakdown:")
            for p, e in sorted(profile_breakdown.items()):
                if not e: continue
                print(f"    {p:<10} n={len(e):>3}  median {statistics.median(e):.2f} km  "
                      f"mean {statistics.mean(e):.2f} km")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--target", help="Pubkey prefix of target to locate (≥4 hex chars)")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--no-terrain", action="store_true",
                    help="Disable terrain-aware mode even if dem_dir is configured")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config(args.config)
    db = open_db(cfg["db_path"])
    los_engine = None if args.no_terrain else _build_los_engine(cfg)

    if not args.target and not args.validate:
        ap.error("specify --target or --validate")
    if args.target:
        cmd_target(db, args.target, cfg, los_engine)
    if args.validate:
        cmd_validate(db, cfg, los_engine)


if __name__ == "__main__":
    main()
