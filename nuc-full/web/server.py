from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import configparser
import json
import os
import socket
import sqlite3
import sys
import time
from urllib.parse import urlparse, parse_qs


PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(BASE_DIR)  # triangulator dir (parent of web/)


def _utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _db_path():
    cfg = os.path.join(APP_DIR, "config.ini")
    rel = "./meshcore_data.db"
    if os.path.exists(cfg):
        p = configparser.ConfigParser()
        p.read(cfg)
        rel = p.get("storage", "db_path", fallback=rel)
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(APP_DIR, rel))


def _filesize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _ro_conn():
    return sqlite3.connect("file:%s?mode=ro" % _db_path(), uri=True, timeout=10)


def _qi(qs, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(qs.get(key, [default])[0])))
    except (ValueError, TypeError):
        return default


def build_contacts(qs):
    """Filterable node list from the contacts table (name/pubkey/role/GPS)."""
    search = (qs.get("search", [""])[0] or "").strip().lower()
    role = (qs.get("role", [""])[0] or "").strip().lower()
    limit = _qi(qs, "limit", 300, 1, 2000)
    where, params = ["1=1"], []
    if search:
        where.append("(LOWER(name) LIKE ? OR LOWER(public_key) LIKE ?)")
        params += ["%" + search + "%", "%" + search + "%"]
    if role:
        where.append("LOWER(role) = ?")
        params.append(role)
    rows = []
    try:
        con = _ro_conn(); con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT public_key, name, role, lat, lng, advert_count, last_seen "
            "FROM contacts WHERE " + " AND ".join(where) +
            " ORDER BY last_seen DESC LIMIT ?", params + [limit]
        ):
            pk = r["public_key"] or ""
            rows.append({
                "public_key": pk, "shortId": pk[:2].upper(), "name": r["name"],
                "role": r["role"], "lat": r["lat"], "lng": r["lng"],
                "advert_count": r["advert_count"],
                "last_advert": (r["last_seen"] + "Z") if r["last_seen"] else None,
            })
        con.close()
    except Exception as error:
        return {"error": str(error), "rows": []}
    return {"rows": rows, "count": len(rows)}


def build_observations(qs):
    """Recent observations: decoded path, packet hash, timestamp, snr/rssi."""
    source = (qs.get("source", [""])[0] or "").strip().lower()
    hours = _qi(qs, "hours", 24, 1, 24 * 90)
    limit = _qi(qs, "limit", 400, 1, 3000)
    where = ["timestamp >= datetime('now','-%d hours')" % hours]
    params = []
    if source:
        where.append("LOWER(source_pk) LIKE ?")
        params.append(source + "%")
    rows = []
    try:
        con = _ro_conn(); con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT timestamp, packet_hash, source_pk, receiver_pk, receiver_name, "
            "snr, rssi, path_json FROM observations WHERE " + " AND ".join(where) +
            " ORDER BY timestamp DESC LIMIT ?", params + [limit]
        ):
            path = []
            if r["path_json"]:
                try:
                    path = json.loads(r["path_json"])
                except Exception:
                    path = [r["path_json"]]
            src = r["source_pk"] or ""
            rcv = r["receiver_pk"] or ""
            rows.append({
                "timestamp": (r["timestamp"] + "Z") if r["timestamp"] else None,
                "packet_hash": r["packet_hash"],
                "source": src[:8], "receiver": rcv[:8],
                "receiver_name": r["receiver_name"],
                "path": path, "hops": len(path),
                "snr": r["snr"], "rssi": r["rssi"],
            })
        con.close()
    except Exception as error:
        return {"error": str(error), "rows": []}
    return {"rows": rows, "count": len(rows)}


def _hav_km(a_lat, a_lng, b_lat, b_lng):
    import math
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(x)))


def build_movers(qs):
    """Nodes that recorded movement in the window (a path, not one fixed spot)."""
    hours = _qi(qs, "hours", 24, 1, 24 * 90)
    try:
        min_m = float(qs.get("min_m", ["200"])[0] or 200)
    except (ValueError, TypeError):
        min_m = 200.0
    rows = []
    try:
        con = _ro_conn(); con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT public_key, lat, lng, timestamp FROM node_positions "
            "WHERE timestamp >= datetime('now','-%d hours') ORDER BY public_key, timestamp" % hours
        )
        by_pk = {}
        for r in cur:
            by_pk.setdefault(r["public_key"], []).append((r["lat"], r["lng"], r["timestamp"]))
        names = {r["public_key"]: r["name"] for r in con.execute("SELECT public_key, name FROM contacts")}
        for pk, pts in by_pk.items():
            if len(pts) < 2:
                continue
            path = sum(_hav_km(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1]) for i in range(1, len(pts)))
            if path * 1000 < min_m:
                continue
            rows.append({
                "public_key": pk, "shortId": pk[:2].upper(), "name": names.get(pk),
                "points": len(pts), "path_km": round(path, 2),
                "first": (pts[0][2] + "Z") if pts[0][2] else None,
                "last": (pts[-1][2] + "Z") if pts[-1][2] else None,
            })
        con.close()
    except Exception as error:
        return {"error": str(error), "rows": []}
    rows.sort(key=lambda x: x["path_km"], reverse=True)
    return {"rows": rows, "count": len(rows)}


def build_track(qs):
    """Ordered position track for one node (by pubkey prefix) within the window."""
    pk = (qs.get("pk", [""])[0] or "").strip().lower()
    hours = _qi(qs, "hours", 24, 1, 24 * 90)
    if not pk:
        return {"rows": []}
    rows = []
    try:
        con = _ro_conn(); con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT lat, lng, timestamp FROM node_positions "
            "WHERE public_key LIKE ? AND timestamp >= datetime('now','-%d hours') "
            "ORDER BY timestamp" % hours, (pk + "%",)
        ):
            rows.append({"lat": r["lat"], "lng": r["lng"], "t": (r["timestamp"] + "Z") if r["timestamp"] else None})
        con.close()
    except Exception as error:
        return {"error": str(error), "rows": []}
    return {"rows": rows, "count": len(rows)}


def build_triangulated_track(qs):
    """Run track_locate.py on demand for a GPS-less mover; return its JSON."""
    import subprocess
    pk = (qs.get("pk", [""])[0] or "").strip()
    if not pk:
        return {"points": []}
    try:
        window = float(qs.get("window", ["12"])[0])
        step = float(qs.get("step", ["4"])[0])
        hours = float(qs.get("hours", ["24"])[0])
        min_obs = int(qs.get("min_obs", ["2"])[0])
    except (ValueError, TypeError):
        window, step, hours, min_obs = 12.0, 4.0, 24.0, 2
    cmd = [
        sys.executable, os.path.join(APP_DIR, "track_locate.py"),
        "--config", os.path.join(APP_DIR, "config.ini"),
        "--target", pk, "--window-min", str(window), "--step-min", str(step),
        "--lookback-hours", str(hours), "--min-obs", str(min_obs),
    ]
    # SNR distance prior is the default; the website can disable it with snr=0.
    if (qs.get("snr", ["1"])[0] or "1").strip() in ("0", "false", "off"):
        cmd.append("--no-snr")
    try:
        out = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=180)
        if out.returncode != 0:
            return {"error": (out.stderr or "track_locate failed")[-300:], "points": []}
        return json.loads(out.stdout)
    except Exception as error:
        return {"error": str(error), "points": []}


def build_calibration(qs):
    """Run calibrate_track.py for a GPS-sharing node and return its JSON: error
    stats (snr vs baseline, raw vs smoothed) plus the snr/baseline/true tracks."""
    import subprocess
    pk = (qs.get("pk", [""])[0] or "").strip()
    if not pk:
        return {"error": "no target prefix"}
    try:
        window = float(qs.get("window", ["12"])[0])
        step = float(qs.get("step", ["4"])[0])
        hours = float(qs.get("hours", ["24"])[0])
        min_obs = int(qs.get("min_obs", ["2"])[0])
    except (ValueError, TypeError):
        window, step, hours, min_obs = 12.0, 4.0, 24.0, 2
    cmd = [
        sys.executable, os.path.join(APP_DIR, "calibrate_track.py"),
        "--config", os.path.join(APP_DIR, "config.ini"), "--json",
        "--target", pk, "--window-min", str(window), "--step-min", str(step),
        "--lookback-hours", str(hours), "--min-obs", str(min_obs),
    ]
    try:
        out = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            return {"error": (out.stderr or "calibrate_track failed")[-300:]}
        return json.loads(out.stdout)
    except Exception as error:
        return {"error": str(error)}


def build_locate_source(qs):
    """Run locate_source.py for a (GPS-less) source and return its JSON: bounded
    fix + heard / silent (exclusion) relay tables."""
    import subprocess
    pk = (qs.get("pk", [""])[0] or "").strip()
    if not pk:
        return {"error": "no target prefix"}
    try:
        hours = float(qs.get("hours", ["48"])[0])
    except (ValueError, TypeError):
        hours = 48.0
    cmd = [
        sys.executable, os.path.join(APP_DIR, "locate_source.py"),
        "--config", os.path.join(APP_DIR, "config.ini"), "--json",
        "--target", pk, "--hours", str(hours),
    ]
    try:
        out = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=180)
        if out.returncode != 0:
            return {"error": (out.stderr or "locate_source failed")[-300:]}
        return json.loads(out.stdout)
    except Exception as error:
        return {"error": str(error)}


VERIFIED_PATH = os.path.join(BASE_DIR, "verified_positions.json")  # web/ — writable by the service


def _load_verified():
    try:
        with open(VERIFIED_PATH) as f:
            return json.load(f)
    except Exception:
        return {"verified": []}


def build_verified(qs=None):
    """Operator-confirmed correct positions (list of {pubkey_prefix, name, note})."""
    return _load_verified()


def mark_verified(body):
    """Add or remove a node from verified_positions.json. body JSON:
    {pubkey_prefix, name?, note?, action: 'add'|'remove'}."""
    try:
        req = json.loads(body or b"{}")
    except Exception:
        return {"error": "bad JSON body"}
    pk = (req.get("pubkey_prefix") or "").strip().lower()
    action = (req.get("action") or "add").lower()
    if not pk:
        return {"error": "pubkey_prefix required"}
    data = _load_verified()
    items = [v for v in data.get("verified", []) if v.get("pubkey_prefix")]
    items = [v for v in items if str(v["pubkey_prefix"]).lower() != pk]   # drop existing
    if action == "add":
        items.append({"pubkey_prefix": pk, "name": req.get("name", ""), "note": req.get("note", "operator-marked")})
    data["verified"] = items
    try:
        tmp = VERIFIED_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, VERIFIED_PATH)
    except Exception as error:
        return {"error": f"could not write: {error}"}
    return {"ok": True, "action": action, "pubkey_prefix": pk, "count": len(items)}


def build_status():
    """Read-only health snapshot for the web monitoring panel."""
    st = {"now": _utc(time.time())}
    db = _db_path()
    size = _filesize(db) + _filesize(db + "-wal") + _filesize(db + "-shm")
    st["db_bytes"] = size
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)

        def one(q):
            try:
                return con.execute(q).fetchone()[0]
            except Exception:
                return None
        st["observations"] = one("SELECT COUNT(*) FROM observations")
        st["obs_1h"] = one("SELECT COUNT(*) FROM observations WHERE timestamp >= datetime('now','-1 hour')")
        st["obs_24h"] = one("SELECT COUNT(*) FROM observations WHERE timestamp >= datetime('now','-1 day')")
        st["last_obs"] = one("SELECT MAX(timestamp) FROM observations")
        st["nodes"] = one("SELECT COUNT(*) FROM contacts")
        st["nodes_gps"] = one("SELECT COUNT(*) FROM contacts WHERE lat IS NOT NULL AND lat != 0")
        con.close()
        st["db_ok"] = True
    except Exception as error:
        st["db_ok"] = False
        st["db_error"] = str(error)

    exp = os.path.join(BASE_DIR, "triangulator-targets.json")
    if os.path.exists(exp):
        st["export_mtime"] = _utc(os.path.getmtime(exp))
        try:
            with open(exp) as fh:
                d = json.load(fh)
            st["export_targets"] = d.get("n_targets_emitted")
            st["export_generated_at"] = d.get("generated_at")
        except Exception:
            pass

    val = os.path.join(APP_DIR, "validate-report.txt")
    if os.path.exists(val):
        st["validate_mtime"] = _utc(os.path.getmtime(val))
        try:
            with open(val, errors="replace") as fh:
                lines = fh.read().splitlines()
            agg = [l.strip() for l in lines if any(k in l for k in ("median err", "mean err", "≤"))]
            st["validate_summary"] = " | ".join(agg[:5]) or None
        except Exception:
            pass
    return st

UPSTREAMS = {
    "/proxy/mc-radar/search": {
        "url": "https://mc-radar.woodwar.com/api/node-inspector/search",
        "method": "POST",
        "content_type": "application/json",
    },
    "/proxy/mc-radar/connected/": {
        "prefix": "https://mc-radar.woodwar.com/api/node-inspector/connected/",
        "method": "GET",
    },
    "/proxy/meshcore/nodes": {
        "url": "https://map.meshcore.io/api/v1/nodes?binary=1&short=1",
        "method": "GET",
    },
    "/proxy/pdok/ahn": {
        "prefix": "https://service.pdok.nl/rws/actueel-hoogtebestand-nederland/wms/v1_0",
        "method": "GET",
    },
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/api/status":
            self._send_json(build_status())
            return
        if route.path == "/api/contacts":
            self._send_json(build_contacts(parse_qs(route.query)))
            return
        if route.path == "/api/observations":
            self._send_json(build_observations(parse_qs(route.query)))
            return
        if route.path == "/api/movers":
            self._send_json(build_movers(parse_qs(route.query)))
            return
        if route.path == "/api/track":
            self._send_json(build_track(parse_qs(route.query)))
            return
        if route.path == "/api/triangulated-track":
            self._send_json(build_triangulated_track(parse_qs(route.query)))
            return
        if route.path == "/api/calibrate-track":
            self._send_json(build_calibration(parse_qs(route.query)))
            return
        if route.path == "/api/locate-source":
            self._send_json(build_locate_source(parse_qs(route.query)))
            return
        if route.path == "/api/verified":
            self._send_json(build_verified())
            return
        if self.path.startswith("/proxy/mc-radar/connected/"):
            self._proxy_dynamic("/proxy/mc-radar/connected/")
            return
        if self.path == "/proxy/meshcore/nodes":
            self._proxy_static("/proxy/meshcore/nodes")
            return
        if self.path.startswith("/proxy/pdok/ahn"):
            self._proxy_query_passthrough("/proxy/pdok/ahn")
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/proxy/mc-radar/search":
            self._proxy_static("/proxy/mc-radar/search")
            return
        if urlparse(self.path).path == "/api/verified":
            self._send_json(mark_verified(self._read_body()))
            return
        self.send_error(404, "Unknown endpoint")

    def _proxy_static(self, key):
        config = UPSTREAMS[key]
        body = self._read_body() if config["method"] == "POST" else None
        self._forward(config["url"], method=config["method"], body=body, content_type=config.get("content_type"))

    def _proxy_dynamic(self, key):
        config = UPSTREAMS[key]
        suffix = self.path[len(key):]
        self._forward(f"{config['prefix']}{suffix}", method=config["method"])

    def _send_json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def _proxy_query_passthrough(self, key):
        config = UPSTREAMS[key]
        query = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
        url = config["prefix"]
        if query:
            url = f"{url}?{query}"
        self._forward(url, method=config["method"])

    def _forward(self, url, method="GET", body=None, content_type=None):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }
        if content_type:
            headers["Content-Type"] = content_type

        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                data = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        except HTTPError as error:
            data = error.read()
            self.send_response(error.code)
            self.send_header("Content-Type", error.headers.get("Content-Type", "application/json"))
            self.end_headers()
            self.wfile.write(data)
        except URLError as error:
            message = str(error.reason).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message)
        except socket.timeout:
            message = b"PDOK request timed out"
            self.send_response(504)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message)
        except Exception as error:
            message = str(error).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message)


def main():
    # Host/port configurable for running on the NUC.
    #   arg 1 = port (default 8000)
    #   MCMAP_HOST env = bind address (default 0.0.0.0 so it's reachable on the
    #   NUC's LAN IP; use 127.0.0.1 if you only want local access / a reverse proxy).
    port = PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    host = os.environ.get("MCMAP_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    shown = host if host != "0.0.0.0" else "0.0.0.0 (all interfaces)"
    print(f"Serving {BASE_DIR} on http://{shown}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
