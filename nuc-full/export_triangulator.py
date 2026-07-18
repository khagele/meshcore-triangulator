#!/usr/bin/env python3
"""Export triangulator estimates as a single JSON file for mc-map to render.

Runs targets.py to get the candidate list, then locate.py per candidate.
Parses the human-readable output (key:value pairs) into structured records.
Result: triangulator-targets.json, ready to drop into mc-map's directory.

Slow: ~0.3-0.7s per candidate via subprocess. For 2000 candidates expect
~15-25 minutes. Sensible as an hourly cron job; not for interactive use.

Use --limit N for quick testing.
"""
from __future__ import annotations

import argparse
import configparser
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO_DEFAULT = Path.home() / "meshcore_mqtt_triangulator"
OUT_DEFAULT = Path("triangulator-targets.json")


def db_path_from_config(repo: Path) -> Path:
    """Read storage.db_path from the repo's config.ini (fallback to default)."""
    cfg_file = repo / "config.ini"
    db_rel = "./meshcore_data.db"
    if cfg_file.exists():
        p = configparser.ConfigParser()
        p.read(cfg_file)
        db_rel = p.get("storage", "db_path", fallback=db_rel)
    db = Path(db_rel)
    return db if db.is_absolute() else (repo / db).resolve()


def enrich_from_db(rows: list[dict], db_file: Path) -> None:
    """Add `name` and human-readable `last_seen` (last reception, UTC) per node.

    `last_seen` is the most recent observations.timestamp for the source, i.e.
    the last time anyone on the broker heard this node. `name`/`last_advert`
    come from the contacts table (set from the node's own adverts).
    """
    if not db_file.exists():
        print(f"[!] DB not found for enrichment: {db_file}", file=sys.stderr)
        return
    db = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    for r in rows:
        pref = r["pubkey_prefix"] + "%"
        seen = db.execute(
            "SELECT MAX(timestamp) AS t FROM observations WHERE source_pk LIKE ?",
            (pref,),
        ).fetchone()
        contact = db.execute(
            "SELECT name, role, last_seen FROM contacts WHERE public_key LIKE ? "
            "ORDER BY last_seen DESC LIMIT 1",
            (pref,),
        ).fetchone()
        last_seen = seen["t"] if seen else None
        r["last_seen"] = f"{last_seen}Z" if last_seen else None  # stored UTC
        if contact:
            if contact["name"]:
                r["name"] = contact["name"]
            r["last_advert"] = f"{contact['last_seen']}Z" if contact["last_seen"] else None
        # Distinct relay paths seen for this source, most-frequent first.
        path_rows = db.execute(
            "SELECT path_json, COUNT(*) AS c FROM observations "
            "WHERE source_pk LIKE ? AND path_json IS NOT NULL AND path_json NOT IN ('', '[]') "
            "GROUP BY path_json ORDER BY c DESC LIMIT 20",
            (pref,),
        ).fetchall()
        paths = []
        for pr in path_rows:
            try:
                hops = json.loads(pr["path_json"])
            except Exception:
                continue
            if isinstance(hops, list) and hops:
                paths.append({"path": [str(h).upper() for h in hops], "count": pr["c"]})
        r["paths"] = paths
        r["n_distinct_paths"] = len(paths)
    db.close()


def find_python(repo: Path) -> str:
    """Prefer the venv's python; fall back to system python3."""
    venv_py = repo / "venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return "python3"


# Matches the data rows in targets.py output. Example:
#  "     3 12bc9584 maaskern                         companion     31    3   2   1 no     (n/a)"
TARGETS_ROW = re.compile(
    r"^\s+(\d+)\s+([0-9a-fA-F]{8})\s+(.+?)\s{2,}"
    r"(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(yes|no)\s+(.+)$"
)

LOCATE_PATTERNS = [
    ("name",        re.compile(r"Target\s*:\s*(.+)")),
    ("estimate",    re.compile(r"Estimate\s*:\s*\(([-0-9.]+)\s*,\s*([-0-9.]+)\)")),
    ("profile",     re.compile(r"Profile\s*:\s*(\S+)")),
    ("n_observers", re.compile(r"Observers used\s*:\s*(\d+)")),
    ("n_paths",     re.compile(r"Paths used\s*:\s*(\d+)")),
    ("n_h0",        re.compile(r"H0 candidates\s*:\s*(\d+)")),
    ("n_direct",    re.compile(r"Direct heard\s*:\s*(\d+)")),
    ("actual",      re.compile(r"Actual\s*:\s*\(([-0-9.]+)\s*,\s*([-0-9.]+)\)")),
    ("error",       re.compile(r"Error\s*:\s*([0-9.]+)")),
]


def list_candidates(python: str, repo: Path) -> list[dict]:
    print("[*] running targets.py ...", file=sys.stderr)
    r = subprocess.run(
        [python, "targets.py"], cwd=repo, capture_output=True, text=True
    )
    if r.returncode != 0:
        sys.exit(f"[!] targets.py failed: {r.stderr}")
    candidates = []
    for line in r.stdout.splitlines():
        m = TARGETS_ROW.match(line)
        if not m:
            continue
        tier, prefix, name, role, n_obs, used, h0, direct, gps_known, est_err = m.groups()
        err_km: float | None = None
        if "km" in est_err:
            try:
                err_km = float(est_err.replace("km", "").strip())
            except ValueError:
                pass
        candidates.append({
            "tier":       int(tier),
            "prefix":     prefix.lower(),
            "name":       name.strip(),
            "role":       role,
            "n_obs":      int(n_obs),
            "used":       int(used),
            "h0":         int(h0),
            "direct":     int(direct),
            "gps_known":  gps_known == "yes",
            "est_err_km": err_km,
        })
    print(f"[*] {len(candidates)} candidates", file=sys.stderr)
    return candidates


def locate_one(python: str, repo: Path, prefix: str, timeout: int = 60) -> dict | None:
    try:
        r = subprocess.run(
            [python, "locate.py", "--target", prefix],
            cwd=repo, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    out = r.stdout
    # Sentinel error lines from locate.py
    for needle in ("every chain rejected", "No observations", "No contact found"):
        if needle in out:
            return None

    parsed: dict = {}
    for key, regex in LOCATE_PATTERNS:
        m = regex.search(out)
        if not m:
            continue
        groups = m.groups()
        parsed[key] = groups if len(groups) > 1 else groups[0]

    if "estimate" not in parsed:
        return None
    lat, lng = parsed["estimate"]
    actual = parsed.get("actual")

    return {
        "name":        (parsed.get("name") or "").strip(),
        "lat":         float(lat),
        "lng":         float(lng),
        "profile":     parsed.get("profile"),
        "n_observers": int(parsed.get("n_observers", 0)),
        "n_paths":     int(parsed.get("n_paths", 0)),
        "n_h0":        int(parsed.get("n_h0", 0)),
        "n_direct":    int(parsed.get("n_direct", 0)),
        "actual_lat":  float(actual[0]) if actual else None,
        "actual_lng":  float(actual[1]) if actual else None,
        "error_km":    float(parsed["error"]) if "error" in parsed else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(REPO_DEFAULT),
                    help=f"Triangulator repo path (default: {REPO_DEFAULT})")
    ap.add_argument("--out", default=str(OUT_DEFAULT),
                    help="Output JSON path (default: ./triangulator-targets.json)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only locate first N candidates (testing)")
    ap.add_argument("--tier-max", type=int, default=4,
                    help="Skip candidates above this tier number (default: 4 = all)")
    ap.add_argument("--name", default=None,
                    help="Only emit nodes whose name contains this substring "
                         "(case-insensitive). Filtering happens after locating, "
                         "so names come from the contacts table.")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        sys.exit(f"[!] repo not found: {repo}")
    python = find_python(repo)

    candidates = list_candidates(python, repo)
    candidates = [c for c in candidates if c["tier"] <= args.tier_max]
    if args.limit:
        candidates = candidates[: args.limit]

    rows = []
    t0 = time.monotonic()
    for i, c in enumerate(candidates, 1):
        result = locate_one(python, repo, c["prefix"])
        if result is None:
            continue
        rows.append({
            "pubkey_prefix": c["prefix"],
            "tier":          c["tier"],
            "role":          c["role"],
            "candidate":     {  # what targets.py thought before re-running locate
                "n_obs":   c["n_obs"],
                "used":    c["used"],
                "h0":      c["h0"],
                "direct":  c["direct"],
            },
            **result,
        })
        if i % 25 == 0:
            rate = i / max(time.monotonic() - t0, 0.01)
            eta_s = (len(candidates) - i) / max(rate, 0.01)
            print(f"[*] {i}/{len(candidates)}  "
                  f"({rate:.1f}/s, ETA {eta_s/60:.1f} min, "
                  f"emitted {len(rows)})", file=sys.stderr)

    # Enrich with name + last-seen timestamps from the DB, then optionally
    # filter by name.
    enrich_from_db(rows, db_path_from_config(repo))
    if args.name:
        needle = args.name.lower()
        rows = [r for r in rows if needle in (r.get("name") or "").lower()]

    # Most-recently-heard first.
    rows.sort(key=lambda r: r.get("last_seen") or "", reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_candidates_input": len(candidates),
        "n_targets_emitted":  len(rows),
        "name_filter":        args.name,
        "targets":            rows,
    }
    out = Path(args.out).expanduser()
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[*] wrote {len(rows)} targets to {out} "
          f"({out.stat().st_size / 1024:.0f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
