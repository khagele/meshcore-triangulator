#!/usr/bin/env python3
"""Retention: delete observations older than N days; keep the contacts table.

Bounds database growth. Deleting frees SQLite pages for reuse (with WAL on), so
the file size plateaus around the working-set size even without VACUUM. VACUUM
actually shrinks the file but needs an exclusive lock, so it's opt-in
(`--vacuum`) and best run with the collector stopped.

Usage:
  ./prune_db.py --repo /opt/meshcore-triangulator --days 45
  ./prune_db.py --repo /opt/meshcore-triangulator --days 45 --vacuum   # also shrink
"""
from __future__ import annotations

import argparse
import configparser
import sqlite3
import sys
from pathlib import Path


def db_path(repo: str) -> Path:
    cfg = Path(repo) / "config.ini"
    rel = "./meshcore_data.db"
    if cfg.exists():
        p = configparser.ConfigParser()
        p.read(cfg)
        rel = p.get("storage", "db_path", fallback=rel)
    d = Path(rel)
    return d if d.is_absolute() else (Path(repo) / d).resolve()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="Triangulator dir holding config.ini")
    ap.add_argument("--days", type=int, default=45, help="Keep observations newer than this (default 45)")
    ap.add_argument("--vacuum", action="store_true", help="Also VACUUM to shrink the file (needs exclusive lock)")
    args = ap.parse_args()

    dbf = db_path(args.repo)
    if not dbf.exists():
        sys.exit(f"[!] DB not found: {dbf}")

    db = sqlite3.connect(str(dbf), timeout=120)
    db.execute("PRAGMA journal_mode = WAL")
    cur = db.execute(
        f"DELETE FROM observations WHERE timestamp < datetime('now','-{args.days} days')"
    )
    deleted = cur.rowcount
    try:
        db.execute(
            f"DELETE FROM node_positions WHERE timestamp < datetime('now','-{args.days} days')"
        )
    except sqlite3.OperationalError:
        pass  # table may not exist on older DBs yet
    db.commit()
    # Keep the WAL file from ballooning after a big delete.
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if args.vacuum:
        db.execute("VACUUM")
    db.close()
    print(f"[*] Pruned {deleted} observations older than {args.days} days from {dbf}"
          + (" (vacuumed)" if args.vacuum else ""))


if __name__ == "__main__":
    main()
