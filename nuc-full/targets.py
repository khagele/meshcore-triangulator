#!/usr/bin/env python3
"""
List every source pubkey in the database that has enough data to be
triangulated reliably.

Filters out sources where:
  - Fewer than `min_observers` distinct receivers heard them
  - The chain-walk algorithm produces < 2 distinct first-hop relays AND
    < 3 direct receptions (degenerate cases — algorithm reports a point
    but it's just the location of a single relay, not real triangulation)

Output is sorted by triangulation quality, grouped into 4 tiers:
  Tier 1: ≥4 distinct first-hop relays  (best)
  Tier 2: 3 distinct first-hop relays
  Tier 3: 2 distinct first-hop relays   (minimum genuine triangulation)
  Tier 4: ≥3 direct (0-hop) receptions  (no h0, multilateration on receivers)

Each row shows the pubkey prefix you'd type for `./locate.py --target <prefix>`.
"""
from __future__ import annotations

import argparse
import statistics
import sys

from locate import (
    haversine_km,
    load_config,
    locate,
    open_db,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--top", type=int, default=0,
                    help="Limit to top N candidates (default: unlimited)")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config(args.config)
    db = open_db(cfg["db_path"])

    n_with_gps = db.execute(
        "SELECT COUNT(*) FROM contacts WHERE lat IS NOT NULL AND lat != 0"
    ).fetchone()[0]
    print(f"[*] {n_with_gps} contacts with known GPS available as anchors/relays\n")

    candidates = db.execute(
        f"""
        SELECT o.source_pk AS pk,
               COUNT(DISTINCT o.receiver_pk) AS n_obs,
               c.name, c.role, c.lat, c.lng
        FROM observations o
        LEFT JOIN contacts c ON c.public_key = o.source_pk
        WHERE o.source_pk IS NOT NULL
        GROUP BY o.source_pk
        HAVING n_obs >= {cfg['min_observers']}
        ORDER BY n_obs DESC
        """
    ).fetchall()
    print(f"[*] {len(candidates)} sources have ≥{cfg['min_observers']} observers; "
          f"running chain-walk algorithm on each...\n")

    results = []
    for c in candidates:
        r = locate(db, c["pk"], cfg)
        if r is None or r.get("no_chain") or r.get("no_data"):
            continue
        n_h0 = r["n_h0_candidates"]
        n_direct = r["n_direct"]
        if n_h0 < 2 and n_direct < 3:
            continue

        err = None
        if c["lat"] and c["lat"] != 0:
            est_lat, est_lng = r["estimate"]
            err = haversine_km(est_lat, est_lng, c["lat"], c["lng"])

        if n_h0 >= 4:
            tier = 1
        elif n_h0 == 3:
            tier = 2
        elif n_h0 == 2:
            tier = 3
        else:
            tier = 4

        results.append({
            "tier": tier,
            "prefix": c["pk"][:8],
            "name": (c["name"] or "?")[:32],
            "role": (c["role"] or "?")[:10],
            "n_obs": c["n_obs"],
            "n_used": r["n_observers_used"],
            "n_h0": n_h0,
            "n_direct": n_direct,
            "has_gps": "yes" if c["lat"] else "no",
            "err_km": err,
        })

    results.sort(key=lambda x: (x["tier"], -x["n_h0"], -x["n_used"]))
    if args.top > 0:
        results = results[: args.top]

    tier_counts: dict[int, int] = {}
    for r in results:
        tier_counts[r["tier"]] = tier_counts.get(r["tier"], 0) + 1
    print(f"[*] {len(results)} reliable candidates after chain-walk filter:")
    print(f"    tier 1 (≥4 h0):     {tier_counts.get(1, 0)}")
    print(f"    tier 2 (3 h0):      {tier_counts.get(2, 0)}")
    print(f"    tier 3 (2 h0):      {tier_counts.get(3, 0)}")
    print(f"    tier 4 (≥3 direct): {tier_counts.get(4, 0)}\n")

    print(f"  {'tier':>4} {'prefix':<8} {'name':<32} {'role':<10} "
          f"{'n_obs':>5} {'used':>4} {'h0':>3} {'dir':>3} "
          f"{'gps?':<4} {'est.err':>7}")
    print(f"  {'-'*4} {'-'*8} {'-'*32} {'-'*10} "
          f"{'-'*5} {'-'*4} {'-'*3} {'-'*3} "
          f"{'-'*4} {'-'*7}")

    last_tier = None
    for r in results:
        if r["tier"] != last_tier:
            print()
            last_tier = r["tier"]
        err_str = f"{r['err_km']:>6.1f}km" if r["err_km"] is not None else "  (n/a)"
        print(f"  {r['tier']:>4} {r['prefix']:<8} {r['name']:<32} {r['role']:<10} "
              f"{r['n_obs']:>5} {r['n_used']:>4} {r['n_h0']:>3} {r['n_direct']:>3} "
              f"{r['has_gps']:<4} {err_str:>7}")

    validated = [r for r in results if r["err_km"] is not None]
    if validated:
        errs = sorted(r["err_km"] for r in validated)
        print()
        print(f"=== Accuracy on {len(validated)} validatable targets "
              "(target self-advertises GPS) ===")
        print(f"  median err : {statistics.median(errs):.2f} km")
        print(f"  mean err   : {statistics.mean(errs):.2f} km")
        print(f"  ≤  1 km    : {sum(1 for e in errs if e <= 1)}")
        print(f"  ≤  5 km    : {sum(1 for e in errs if e <= 5)}")
        print(f"  ≤ 10 km    : {sum(1 for e in errs if e <= 10)}")


if __name__ == "__main__":
    main()
