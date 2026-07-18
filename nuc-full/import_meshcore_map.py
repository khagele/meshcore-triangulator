#!/usr/bin/env python3
"""Import GPS positions from map.meshcore.io into the triangulator's contacts table.

Pulls the global MeshCore node feed (msgpack), and for every node with valid
GPS does an upsert into the triangulator's SQLite contacts table:
  - Pubkey not yet in contacts → INSERT with name, role, lat, lng.
  - Pubkey in contacts but lat/lng NULL → UPDATE lat, lng (fill the gap).
  - Pubkey in contacts with existing GPS → LEAVE ALONE.
    Rationale: existing GPS came from the broker your collector subscribes to,
    which is usually more recent than map.meshcore.io's snapshot. If you'd
    rather overwrite, run with --overwrite.

Requires: msgpack. Install with `./venv/bin/pip install msgpack`.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from urllib.request import Request, urlopen

URL = "https://map.meshcore.io/api/v1/nodes?binary=1&short=1"

# MeshCore device_type integer → role string (matches what the triangulator's
# collector writes for contacts of each type).
ROLE_MAP = {
    1: "companion",
    2: "repeater",
    3: "roomserver",
    4: "sensor",
}


def fetch_nodes() -> list[dict]:
    try:
        import msgpack  # type: ignore
    except ImportError:
        sys.exit("[!] missing msgpack — install with: ./venv/bin/pip install msgpack")
    print(f"[*] fetching {URL}", file=sys.stderr)
    req = Request(URL, headers={"Accept": "application/octet-stream"})
    with urlopen(req, timeout=30) as r:
        data = r.read()
    return msgpack.unpackb(data, raw=False)


def normalize(raw: list[dict]) -> list[tuple[str, str, str, float, float]]:
    """Return (pubkey_hex_lower, name, role, lat, lng) for each valid node."""
    out: list[tuple[str, str, str, float, float]] = []
    for node in raw:
        pk = node.get("pk")
        if isinstance(pk, (bytes, bytearray)):
            pk = pk.hex()
        if not isinstance(pk, str) or len(pk) < 8:
            continue
        lat = node.get("lat")
        lon = node.get("lon")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        if lat == 0 and lon == 0:
            continue
        name = (node.get("n") or "").strip()
        type_id = node.get("t")
        role = ROLE_MAP.get(type_id, "")
        out.append((pk.lower(), name, role, float(lat), float(lon)))
    return out


def upsert(db: sqlite3.Connection, rows: list[tuple[str, str, str, float, float]],
           overwrite: bool) -> tuple[int, int, int]:
    inserted = updated = skipped = 0
    for pk, name, role, lat, lng in rows:
        existing = db.execute(
            "SELECT lat, lng FROM contacts WHERE public_key = ?", (pk,)
        ).fetchone()
        if existing is None:
            db.execute(
                "INSERT INTO contacts (public_key, name, role, lat, lng) "
                "VALUES (?, ?, ?, ?, ?)",
                (pk, name or None, role or None, lat, lng),
            )
            inserted += 1
            continue
        ex_lat, ex_lng = existing
        has_gps = ex_lat is not None and ex_lng is not None \
                  and (ex_lat != 0 or ex_lng != 0)
        if has_gps and not overwrite:
            skipped += 1
            continue
        # Either no GPS yet, or --overwrite was requested.
        # Don't clobber name/role if already set — only fill blanks.
        db.execute(
            "UPDATE contacts SET lat = ?, lng = ?, "
            "name = COALESCE(NULLIF(name, ''), ?), "
            "role = COALESCE(NULLIF(role, ''), ?) "
            "WHERE public_key = ?",
            (lat, lng, name or None, role or None, pk),
        )
        updated += 1
    return inserted, updated, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="meshcore_data.db",
                    help="Path to triangulator SQLite DB (default: ./meshcore_data.db)")
    ap.add_argument("--overwrite", action="store_true",
                    help="Overwrite existing GPS coordinates with map.meshcore.io's values")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would happen without writing to the DB")
    args = ap.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"[!] DB not found: {args.db}")

    raw = fetch_nodes()
    print(f"[*] fetched {len(raw)} nodes total", file=sys.stderr)
    rows = normalize(raw)
    print(f"[*] {len(rows)} nodes have valid GPS", file=sys.stderr)

    db = sqlite3.connect(args.db)
    if args.dry_run:
        # Count without writing.
        existing_with_gps = 0
        existing_without_gps = 0
        new = 0
        for pk, *_ in rows:
            r = db.execute(
                "SELECT lat, lng FROM contacts WHERE public_key = ?", (pk,)
            ).fetchone()
            if r is None:
                new += 1
            elif r[0] is None or r[1] is None:
                existing_without_gps += 1
            else:
                existing_with_gps += 1
        print(f"[dry-run] would insert {new}, update {existing_without_gps}, "
              f"skip {existing_with_gps} (already have GPS)",
              file=sys.stderr)
        return

    inserted, updated, skipped = upsert(db, rows, args.overwrite)
    db.commit()
    db.close()
    print(f"[*] inserted {inserted} new, updated {updated} (filled missing GPS), "
          f"skipped {skipped} (existing GPS preserved — re-run with --overwrite to force)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
