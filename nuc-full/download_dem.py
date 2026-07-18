#!/usr/bin/env python3
"""Download Copernicus GLO-30 DEM tiles for a bounding box.

Tiles come from AWS Open Data (anonymous public S3 — no account, no auth).
1°×1° GeoTIFFs, ~25 MB each, named for their SW corner. Ocean-only squares
don't exist in the bucket — those are skipped silently.

Usage:
  ./download_dem.py --bbox <S> <W> <N> <E>
  ./download_dem.py --auto                   # infer bbox from collected GPS
  ./download_dem.py --auto --buffer-km 50    # add a buffer

Defaults:
  --out-dir ./dem
  --parallel 8

Examples:
  # Australian SE mainland + Tasmania
  ./download_dem.py --bbox -43 138 -27 154

  # Auto-detect from collector data with default 50km buffer
  ./download_dem.py --auto

  # Western Europe
  ./download_dem.py --bbox 36 -10 60 20
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import configparser
import math
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def tile_name(lat_floor: int, lng_floor: int) -> str:
    """Copernicus naming convention. lat_floor < 0 → S; lng_floor < 0 → W."""
    ns = "S" if lat_floor < 0 else "N"
    ew = "W" if lng_floor < 0 else "E"
    return (
        f"Copernicus_DSM_COG_10_{ns}{abs(lat_floor):02d}_00_"
        f"{ew}{abs(lng_floor):03d}_00_DEM"
    )


def download_tile(lat_floor: int, lng_floor: int, out_dir: Path, timeout: int = 60):
    name = tile_name(lat_floor, lng_floor)
    url = f"{BASE}/{name}/{name}.tif"
    out = out_dir / f"{name}.tif"
    if out.exists() and out.stat().st_size > 0:
        return lat_floor, lng_floor, "cached", out.stat().st_size
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mc-tri-dem/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        out.write_bytes(data)
        return lat_floor, lng_floor, "ok", len(data)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return lat_floor, lng_floor, "no-tile", 0  # ocean / not in bucket
        return lat_floor, lng_floor, f"http-{e.code}", 0
    except Exception as e:
        return lat_floor, lng_floor, f"err-{type(e).__name__}", 0


def bbox_from_db(db_path: str, buffer_km: float) -> tuple[int, int, int, int]:
    """Return (south_floor, west_floor, north_floor, east_floor) from collector contacts.

    Coordinates outside the valid geographic range (lat in [-90, 90], lng in
    [-180, 180]) are excluded. Some MeshCore firmware versions occasionally
    publish malformed GPS in adverts; without this filter a single rogue
    contact (e.g. lat=1989) would blow the bbox up to cover the whole globe
    and the tile enumeration would OOM the host.
    """
    if not os.path.exists(db_path):
        sys.exit(f"[!] DB not found: {db_path}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = db.execute(
        "SELECT MIN(lat), MAX(lat), MIN(lng), MAX(lng) FROM contacts "
        "WHERE lat IS NOT NULL AND lng IS NOT NULL "
        "AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180 "
        "AND lat != 0 AND lng != 0"
    ).fetchone()
    if not row or row[0] is None:
        sys.exit("[!] No GPS-known contacts in DB — collect data first or pass --bbox")
    s, n, w, e = row
    pad_deg = buffer_km / 111.0
    s -= pad_deg; n += pad_deg
    # Longitude pad scales with cos(latitude). Pick the narrower latitude.
    cos_lat = math.cos(math.radians(max(abs(s), abs(n))))
    w -= pad_deg / max(0.1, cos_lat)
    e += pad_deg / max(0.1, cos_lat)
    # Clamp to valid geographic range after buffer expansion, in case the
    # bbox itself is already near the edge of the world.
    s = max(s, -90.0)
    n = min(n,  90.0)
    w = max(w, -180.0)
    e = min(e,  180.0)
    return math.floor(s), math.floor(w), math.floor(n), math.floor(e)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("S", "W", "N", "E"),
                    help="Bounding box: south west north east (degrees)")
    ap.add_argument("--auto", action="store_true",
                    help="Infer bbox from collected contacts in the DB")
    ap.add_argument("--buffer-km", type=float, default=50.0,
                    help="Buffer (km) when using --auto. Default 50.")
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--out-dir", default="./dem")
    ap.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()

    if args.auto and args.bbox:
        ap.error("--auto and --bbox are mutually exclusive")
    if not (args.auto or args.bbox):
        ap.error("specify --bbox S W N E or --auto")

    if args.auto:
        cfg = configparser.ConfigParser()
        if not Path(args.config).exists():
            sys.exit(f"[!] Config file '{args.config}' not found")
        cfg.read(args.config)
        db_path = cfg.get("storage", "db_path", fallback="./meshcore_data.db")
        s_floor, w_floor, n_floor, e_floor = bbox_from_db(db_path, args.buffer_km)
        print(f"[*] Auto-detected bbox: lat [{s_floor}, {n_floor}]  "
              f"lng [{w_floor}, {e_floor}]  (with {args.buffer_km}km buffer)")
    else:
        s, w, n, e = args.bbox
        s_floor = math.floor(s); n_floor = math.floor(n)
        w_floor = math.floor(w); e_floor = math.floor(e)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = [
        (lat, lng)
        for lat in range(s_floor, n_floor + 1)
        for lng in range(w_floor, e_floor + 1)
    ]
    print(f"[*] {len(jobs)} candidate 1°×1° tiles, parallelism={args.parallel}",
          flush=True)

    counts = {"ok": 0, "cached": 0, "no-tile": 0, "err": 0}
    total_bytes = 0
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = [pool.submit(download_tile, la, lo, out_dir) for la, lo in jobs]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            la, lo, status, n = fut.result()
            total_bytes += n
            if status in counts:
                counts[status] += 1
            else:
                counts["err"] += 1
                print(f"  [!] tile {tile_name(la, lo)}: {status}", flush=True)
            if i % 16 == 0 or i == len(jobs):
                print(
                    f"  [{i:>3}/{len(jobs)}] ok={counts['ok']} "
                    f"cached={counts['cached']} no-tile={counts['no-tile']} "
                    f"err={counts['err']}  {total_bytes/1e6:.0f}MB",
                    flush=True,
                )

    print()
    print(f"[*] Done. {counts['ok']} downloaded, {counts['cached']} already on disk, "
          f"{counts['no-tile']} not in bucket (ocean), {counts['err']} errors.")
    print(f"[*] Total {total_bytes/1e6:.1f} MB in {out_dir.resolve()}")
    return 0 if counts["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
