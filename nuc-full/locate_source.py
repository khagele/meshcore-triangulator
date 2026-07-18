#!/usr/bin/env python3
"""Localise a (possibly GPS-less, non-adverting) MeshCore source from topology,
using positive AND exclusion constraints — the method from the ON8AR/CoreScope
report.

Positive: relays that heard the source FIRST-HOP (path[0]) or directly (empty path)
→ the source is within each such relay's reach (intersection of "heard" disks).

Exclusion ("silent wall"): GPS-known relays that RELAYED the source's packets (appear
deeper in paths) but were NEVER first-hop ⇒ the source is OUTSIDE each such relay's
reach. Relaying proves the relay was active in the window, so its silence is
meaningful (the "active state" gate — offline relays never appear).

Per-relay reach is that relay's own observed first-hop range (p80 of distances to the
GPS-known sources it has heard directly), NOT a fixed radius — a distant flood-relayer
then excludes nothing nearby, which is correct. Falls back to max_rf_km.

Output mirrors the report: a bounded fix + "who heard him" / "who stayed silent"
tables. Reads the collector DB read-only; safe to run alongside the live system.

  ./locate_source.py --target <pubkey-prefix> [--hours 48] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import locate as L  # haversine_km, relay_candidates, parse_path, load_relay_index, etc.

REACH_PCTL = 0.80
DEFAULT_REACH_KM = 12.0    # used when a relay has no usable direct-hearing distances
POS_TOL = 0.04             # tie-break region: cells within this fraction of peak score
REGION_KM = 1.5            # …AND within this many km of the positive peak. Exclusion is
                           # capped to a micro-refinement so a false silence can't drag
                           # a well-supported fix.


def _sane(lat, lng):
    return (lat is not None and lng is not None
            and abs(lat) <= 90 and abs(lng) <= 180 and not (lat == 0 and lng == 0))


def percentile(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    i = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[i]


def load_verified():
    """Operator-confirmed correct positions (verified_positions.json beside this
    script). Returns lowercase pubkey prefixes."""
    try:
        data = json.loads((Path(__file__).resolve().parent / "web" / "verified_positions.json").read_text())
        return [str(v["pubkey_prefix"]).lower() for v in data.get("verified", []) if v.get("pubkey_prefix")]
    except Exception:
        return []


def _is_verified(pk, prefixes):
    pk = (pk or "").lower()
    return any(pk.startswith(p) for p in prefixes)


def resolve_target(db, prefix):
    p = prefix.lower() + "%"
    row = db.execute("SELECT public_key FROM contacts WHERE public_key LIKE ? LIMIT 1", (p,)).fetchone()
    if row:
        return row["public_key"]
    row = db.execute("SELECT source_pk FROM observations WHERE source_pk LIKE ? LIMIT 1", (p,)).fetchone()
    return row["source_pk"] if row else None


def relay_reach_km(db, relay_pk, full_pk_gps, cache, p=REACH_PCTL, max_rf_km=35.0):
    """p-th percentile of distances from `relay_pk` to the GPS-known sources it has
    heard DIRECTLY (empty path) — the relay's observed first-hop range. Cached."""
    if relay_pk in cache:
        return cache[relay_pk]
    rg = full_pk_gps.get(relay_pk)
    dists = []
    if rg and _sane(rg[0], rg[1]):
        rows = db.execute(
            "SELECT DISTINCT source_pk FROM observations "
            "WHERE receiver_pk = ? AND (path_json IS NULL OR path_json = '[]' OR path_json = '')",
            (relay_pk,),
        ).fetchall()
        for r in rows:
            sg = full_pk_gps.get((r["source_pk"] or "").lower())
            if sg and _sane(sg[0], sg[1]):
                d = L.haversine_km(rg[0], rg[1], sg[0], sg[1])
                # A direct (first-hop) reception is RF-limited; anything beyond
                # max_rf_km is a non-RF/misattributed reception (gateways log these)
                # and must not inflate the reach.
                if d <= max_rf_km:
                    dists.append(d)
    if dists:
        reach = min(max_rf_km, max(2.0, percentile(dists, p)))
    else:
        reach = DEFAULT_REACH_KM
    cache[relay_pk] = reach
    return reach


def analyze_source(db, prefix, hours, cfg):
    target = resolve_target(db, prefix)
    if not target:
        return {"error": f"no node matching prefix '{prefix}'"}
    one_byte_index, full_pk_gps = L.load_relay_index(db)
    obs = db.execute(
        "SELECT receiver_pk, path_json FROM observations "
        "WHERE source_pk = ? AND timestamp >= datetime('now', ?)",
        (target, f"-{int(hours)} hours"),
    ).fetchall()

    heard = defaultdict(int)      # relay_pk -> first-hop/direct count
    relayed = defaultdict(int)    # relay_pk -> relayed (non-first-hop) count
    for r in obs:
        receiver = (r["receiver_pk"] or "").lower()
        recv_gps = full_pk_gps.get(receiver)
        path = [h for h in L.parse_path(r["path_json"]) if not target.startswith(h)]
        if not path:
            if recv_gps:
                heard[receiver] += 1
            continue
        if recv_gps is None:
            continue
        # Chain-walk from the receiver back toward the source, resolving each hop to
        # the candidate nearest the running chain (same logic as collect_constraints).
        prev_lat, prev_lng = recv_gps[0], recv_gps[1]
        resolved = []
        ok = True
        for i in range(len(path) - 1, -1, -1):
            cands = L.relay_candidates(one_byte_index, path[i])
            if not cands:
                ok = False
                break
            best = min(cands, key=lambda c: L.haversine_km(c[1], c[2], prev_lat, prev_lng))
            if L.haversine_km(best[1], best[2], prev_lat, prev_lng) > cfg["max_rf_km"]:
                ok = False
                break
            resolved.append(best[0])           # best[0] = pubkey
            prev_lat, prev_lng = best[1], best[2]
        if not ok or not resolved:
            continue
        resolved.reverse()                     # now index 0 == first hop (nearest source)
        heard[resolved[0]] += 1
        for pk in resolved[1:]:
            relayed[pk] += 1

    # Silent = relayed but never first-hop, GPS-known, sane coords.
    verified = load_verified()
    reach_cache = {}
    heard_rows, silent_rows = [], []
    for pk, n in heard.items():
        g = full_pk_gps.get(pk)
        if not g or not _sane(g[0], g[1]):
            continue
        heard_rows.append({"pk": pk, "lat": g[0], "lng": g[1], "n": n,
                           "reach_km": relay_reach_km(db, pk, full_pk_gps, reach_cache, max_rf_km=cfg["max_rf_km"]),
                           "verified": _is_verified(pk, verified)})
    for pk, n in relayed.items():
        if pk in heard:
            continue
        g = full_pk_gps.get(pk)
        if not g or not _sane(g[0], g[1]):
            continue
        silent_rows.append({"pk": pk, "lat": g[0], "lng": g[1], "relayed": n,
                            "reach_km": relay_reach_km(db, pk, full_pk_gps, reach_cache, max_rf_km=cfg["max_rf_km"]),
                            "verified": _is_verified(pk, verified)})

    heard_rows = trim_heard(heard_rows, cfg["max_rf_km"])
    est = localise(heard_rows, silent_rows)
    return {"target": target, "heard": heard_rows, "silent": silent_rows, **est}


def trim_heard(rows, max_rf_km, iters=4):
    """Drop implausible first-hop relays. A real first-hop relay must be within RF
    range of the source, so it must cluster with the other (weighted) heard relays;
    relays sitting >max_rf_km from the robust centre are chain-resolution/gateway
    artifacts (we saw 'first-hop' relays 33–88 km away) and only drag the fix."""
    if len(rows) <= 2:
        return rows
    keep = rows
    for _ in range(iters):
        gm = _weighted_geomedian(keep)
        if not gm:
            break
        kept = [r for r in keep if L.haversine_km(gm[0], gm[1], r["lat"], r["lng"]) <= max_rf_km]
        if len(kept) < 2 or len(kept) == len(keep):
            if len(kept) >= 2:
                keep = kept
            break
        keep = kept
    return keep if len(keep) >= 2 else rows


def _weighted_geomedian(rows):
    if not rows:
        return None
    x = sum(r["lat"] for r in rows) / len(rows)
    y = sum(r["lng"] for r in rows) / len(rows)
    for _ in range(200):
        nx = ny = den = 0.0
        for r in rows:
            d = max(L.haversine_km(x, y, r["lat"], r["lng"]), 0.05)
            w = math.log((r.get("n") or r.get("relayed") or 1) + 1) / d
            nx += r["lat"] * w; ny += r["lng"] * w; den += w
        if den == 0:
            break
        nx, ny = nx / den, ny / den
        if abs(nx - x) < 1e-7 and abs(ny - y) < 1e-7:
            break
        x, y = nx, ny
    return (x, y)


def localise(heard, silent):
    """Positive evidence (heard first-hop relays) defines WHERE the source can be;
    the silent wall only chooses WITHIN that region — never overrides it. This way
    a strong, well-anchored fix is barely moved, while a broad one-sided fix (the
    NIMBUS case) is pulled to the side the silent relays permit.

    Returns the fix, the positive-only geomedian (baseline, for comparison), and an
    uncertainty (km) from the spread of the high-positive region.
    """
    base = _weighted_geomedian(heard)
    if base is None:
        return {"estimate": None, "baseline_geomedian": None, "note": "no GPS-known heard relays"}

    def positive(la, lo):
        s = 0.0
        for r in heard:
            d = L.haversine_km(la, lo, r["lat"], r["lng"])
            sigma = max(r["reach_km"] * 0.6, 2.0)
            s += math.log((r.get("n") or 1) + 1) * math.exp(-(d * d) / (2 * sigma * sigma))
        return s

    def exclusion(la, lo):
        s = 0.0
        for r in silent:
            d = L.haversine_km(la, lo, r["lat"], r["lng"])
            reach = r["reach_km"]
            if d < reach:
                s += math.log((r.get("relayed") or 1) + 1) * (1.0 - d / reach)
        return s

    # Grid the area; the PRIMARY fix is the peak of the reach-aware positive score
    # (this is the real win — it down-weights far relays the geomedian over-trusts).
    lats = [r["lat"] for r in heard + silent]; lngs = [r["lng"] for r in heard + silent]
    pad = 0.25
    min_la, max_la = min(lats) - pad, max(lats) + pad
    min_lo, max_lo = min(lngs) - pad, max(lngs) + pad
    steps = 70
    dla = (max_la - min_la) / steps
    dlo = (max_lo - min_lo) / steps
    cells = []
    la = min_la
    while la <= max_la:
        lo = min_lo
        while lo <= max_lo:
            cells.append((positive(la, lo), la, lo))
            lo += dlo
        la += dla
    max_pos = max(p for p, _, _ in cells)
    if max_pos <= 0:
        return {"estimate": None, "baseline_geomedian": {"lat": round(base[0], 5), "lng": round(base[1], 5)},
                "note": "no positive support"}
    peak = max(cells, key=lambda c: c[0])
    method = "positive-peak"

    # Exclusion as a TIGHT, discounted tie-break. A silent relay sitting where the
    # positive score is already high is almost certainly a *false* silence
    # (terrain/timing), so discount it toward zero; only silent relays in
    # low-positive regions (a genuine open side) keep their weight.
    region = [(la, lo) for p, la, lo in cells
              if p >= max_pos * (1 - POS_TOL)
              and L.haversine_km(peak[1], peak[2], la, lo) <= REGION_KM]
    best = (peak[1], peak[2])
    # Only trust silent relays whose reach does NOT contain the positive peak — one
    # that does is contradicting strong positive evidence (a false silence), so drop it.
    silent_used = [r for r in silent
                   if L.haversine_km(peak[1], peak[2], r["lat"], r["lng"]) > r["reach_km"]]
    if silent_used and len(region) > 1:
        def excl_pen(la, lo):
            s = 0.0
            for r in silent_used:
                d = L.haversine_km(la, lo, r["lat"], r["lng"])
                if d < r["reach_km"]:
                    s += math.log((r.get("relayed") or 1) + 1) * (1.0 - d / r["reach_km"])
            return s
        cand = min(region, key=lambda c: excl_pen(c[0], c[1]))
        if excl_pen(cand[0], cand[1]) < excl_pen(best[0], best[1]) - 1e-9:
            best = cand
            method = "positive-peak + exclusion tie-break"

    cell_km = dla * 111.0
    spread = percentile([L.haversine_km(best[0], best[1], la, lo) for la, lo in region], 0.9) or 0.0
    unc = max(spread, cell_km)
    return {"estimate": {"lat": round(best[0], 5), "lng": round(best[1], 5),
                         "uncertainty_km": round(unc, 1), "method": method,
                         "region_cells": len(region)},
            "baseline_geomedian": {"lat": round(base[0], 5), "lng": round(base[1], 5)}}


def _heard_uncertainty(heard, base):
    cell = percentile([L.haversine_km(base[0], base[1], r["lat"], r["lng"]) for r in heard], 0.5) or 2.0
    return max(2.0, min(cell, 35.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--target", required=True)
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args()
    cfg = L.load_config(args.config)
    db = L.open_db(cfg["db_path"])
    res = analyze_source(db, args.target, args.hours, cfg)
    if args.as_json:
        print(json.dumps(res)); return
    if "error" in res:
        print("[!]", res["error"]); sys.exit(1)
    e = res.get("estimate"); b = res.get("baseline_geomedian")
    print(f"target {res['target'][:12]}…")
    if e:
        print(f"fix: {e['lat']}, {e['lng']}  ± ~{e['uncertainty_km']} km   (positive-only geomedian: {b['lat']}, {b['lng']})")
    print(f"\nHEARD first-hop ({len(res['heard'])}):")
    for r in sorted(res["heard"], key=lambda x: -x["n"])[:15]:
        print(f"  {r['pk'][:8]} n={r['n']:3} reach~{r['reach_km']:.0f}km @ {r['lat']:.4f},{r['lng']:.4f}")
    print(f"\nSILENT / exclusion ({len(res['silent'])}):")
    for r in sorted(res["silent"], key=lambda x: -x["relayed"])[:15]:
        print(f"  {r['pk'][:8]} relayed={r['relayed']:3} reach~{r['reach_km']:.0f}km @ {r['lat']:.4f},{r['lng']:.4f}")


if __name__ == "__main__":
    main()
