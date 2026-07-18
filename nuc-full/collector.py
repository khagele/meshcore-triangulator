#!/usr/bin/env python3
"""
Subscribe to a MeshCore MQTT broker and persist every packet observation to
SQLite for later triangulation.

Reads MQTT broker config from config.ini in the current directory.

Two topics are subscribed to (configurable):
  - status_topic_pattern  — each publishing radio's name + GPS + role
  - packets_topic_pattern — every received packet, with rssi/snr/path/raw

Tolerant of variations in broker payload format — accepts common JSON key
aliases for lat/lon/name/pubkey.

Run under systemd / supervisord / docker for production use; --restart on failure.
"""
from __future__ import annotations

import argparse
import configparser
import json
import logging
import math
import signal
import sqlite3
import ssl
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

import paho.mqtt.client as mqtt

from meshcore_decoder import decode_advert, DecodeError

LOG = logging.getLogger("collector")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    public_key   TEXT PRIMARY KEY,
    name         TEXT,
    role         TEXT,
    lat          REAL,
    lng          REAL,
    advert_count INTEGER NOT NULL DEFAULT 0,
    first_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_contacts_role ON contacts(role);
CREATE INDEX IF NOT EXISTS idx_contacts_loc  ON contacts(lat, lng);

CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_hash   TEXT NOT NULL,
    receiver_pk   TEXT NOT NULL,
    receiver_name TEXT,
    source_pk     TEXT,
    payload_type  INTEGER,
    rssi          REAL,
    snr           REAL,
    path_json     TEXT,
    raw_hex       TEXT,
    timestamp     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(packet_hash, receiver_pk)
);
CREATE INDEX IF NOT EXISTS idx_obs_source   ON observations(source_pk);
CREATE INDEX IF NOT EXISTS idx_obs_receiver ON observations(receiver_pk);
CREATE INDEX IF NOT EXISTS idx_obs_time     ON observations(timestamp);

CREATE TABLE IF NOT EXISTS node_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    public_key  TEXT NOT NULL,
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pos_pk   ON node_positions(public_key, timestamp);
CREATE INDEX IF NOT EXISTS idx_pos_time ON node_positions(timestamp);
"""


def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: paho-mqtt callbacks run on a worker thread but
    # write to the same connection. SQLite's default threading mode is
    # serialized so this is safe — each call is internally mutex-protected.
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode = WAL")
    db.commit()
    return db


MOVE_THRESHOLD_M = 30.0  # ignore GPS jitter below this; record real movement


def _valid_gps(lat: float, lng: float) -> bool:
    return (-90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0))


def _moved_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def upsert_contact(
    db: sqlite3.Connection,
    pubkey: str,
    name: str | None = None,
    role: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    bump_advert: bool = False,
) -> None:
    pubkey = pubkey.lower()
    # Record a position sample for moving-node tracks, when GPS is present and
    # the node moved more than MOVE_THRESHOLD_M from its last known spot. Static
    # nodes re-advert the same coordinates → no new row, table stays small.
    if lat is not None and lng is not None and _valid_gps(lat, lng):
        prev = db.execute(
            "SELECT lat, lng FROM contacts WHERE public_key = ?", (pubkey,)
        ).fetchone()
        if (prev is None or prev["lat"] is None or prev["lng"] is None
                or _moved_m(prev["lat"], prev["lng"], lat, lng) > MOVE_THRESHOLD_M):
            db.execute(
                "INSERT INTO node_positions (public_key, lat, lng) VALUES (?, ?, ?)",
                (pubkey, lat, lng),
            )
    db.execute(
        """
        INSERT INTO contacts (public_key, name, role, lat, lng,
                              advert_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(public_key) DO UPDATE SET
            name         = COALESCE(excluded.name, contacts.name),
            role         = COALESCE(excluded.role, contacts.role),
            lat          = COALESCE(excluded.lat, contacts.lat),
            lng          = COALESCE(excluded.lng, contacts.lng),
            advert_count = contacts.advert_count + ?,
            last_seen    = CURRENT_TIMESTAMP
        """,
        (pubkey, name, role, lat, lng, 1 if bump_advert else 0,
         1 if bump_advert else 0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Topic-string utilities
# ─────────────────────────────────────────────────────────────────────────────


def extract_pubkey_from_topic(topic: str) -> str | None:
    """Walk the topic segments for the first one that looks like a 64-hex pubkey.

    The standard pattern is meshcore/{IATA}/{PUBLIC_KEY}/{status,packets} but
    different brokers may put the pubkey in different positions. We just look
    for the first 64-hex segment.
    """
    for seg in topic.split("/"):
        s = seg.strip().lower()
        if len(s) == 64 and all(c in "0123456789abcdef" for c in s):
            return s
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Payload normalisation — different brokers use different JSON key names
# ─────────────────────────────────────────────────────────────────────────────


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def parse_status_payload(payload: dict, fallback_pk: str | None) -> dict | None:
    """Return {pubkey, name, role, lat, lng} or None if not enough info."""
    pk = _first(payload, "origin_id", "public_key", "pubkey", "pubKey", "id")
    if pk is None:
        pk = fallback_pk
    if pk is None:
        return None
    pk = str(pk).lower()
    if len(pk) != 64:
        return None

    name = _first(payload, "name", "adv_name")
    role = _first(payload, "role", "type")
    if isinstance(role, int):
        role = {1: "companion", 2: "repeater", 3: "roomserver", 4: "sensor"}.get(role)

    lat = _first(payload, "lat", "latitude", "adv_lat")
    lng = _first(payload, "lon", "lng", "longitude", "adv_lon")
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lat = lng = None

    return {"pubkey": pk, "name": name, "role": role, "lat": lat, "lng": lng}


def parse_packet_payload(payload: dict, fallback_pk: str | None) -> dict | None:
    """Extract the standard packet fields. Returns None if fundamentals are missing."""
    receiver = _first(payload, "origin_id", "receiver", "receiver_pk", "publisher")
    if receiver is None:
        receiver = fallback_pk
    if receiver is None:
        return None
    receiver = str(receiver).lower()
    if len(receiver) != 64:
        return None

    pkt_hash = _first(payload, "hash", "packet_hash")
    if pkt_hash is None:
        return None

    # Different brokers name this field differently — meshcore-bot's
    # PacketCaptureService publishes 'packet_type', other variants use
    # 'payload_type'. Note: `type` may exist but as a label like "PACKET",
    # so try the numeric ones first.
    payload_type_raw = _first(payload, "packet_type", "payload_type", "type")
    try:
        payload_type = int(payload_type_raw)
    except (TypeError, ValueError):
        payload_type = None

    direction = _first(payload, "direction", "dir")
    # We only care about RECEIVED packets (rx). Some brokers also publish their
    # OWN transmissions (tx) which we should ignore.
    if direction is not None and str(direction).lower() not in ("rx", "in", "recv", "receive"):
        return None

    rssi = _first(payload, "rssi", "RSSI")
    snr = _first(payload, "snr", "SNR")
    try:
        rssi = float(rssi) if rssi is not None else None
        snr = float(snr) if snr is not None else None
    except (TypeError, ValueError):
        rssi = snr = None

    raw_hex = _first(payload, "raw", "raw_hex", "data")
    if raw_hex:
        raw_hex = str(raw_hex).lower().replace(" ", "").replace(":", "")

    # Path: standard format is comma-separated 1-byte hex strings ("ac,27,cf"),
    # or a JSON array (["ac","27","cf"]). Normalise to JSON array string.
    path_raw = _first(payload, "path", "path_json")
    path_json: str | None
    if path_raw is None or path_raw == "":
        path_json = "[]"
    elif isinstance(path_raw, list):
        path_json = json.dumps([str(x).lower() for x in path_raw])
    else:
        s = str(path_raw).strip()
        if s.startswith("["):
            path_json = s
        else:
            arr = [seg.strip().lower() for seg in s.split(",") if seg.strip()]
            path_json = json.dumps(arr)

    return {
        "receiver_pk": receiver,
        "packet_hash": str(pkt_hash).lower(),
        "payload_type": payload_type,
        "rssi": rssi,
        "snr": snr,
        "raw_hex": raw_hex,
        "path_json": path_json,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Message handlers (called from MQTT thread; keep them quick)
# ─────────────────────────────────────────────────────────────────────────────


class Handler:
    def __init__(self, db: sqlite3.Connection, counts: dict):
        self.db = db
        self.counts = counts
        self._commit_every = 25
        self._since_last_commit = 0

    def handle_status(self, topic: str, payload_bytes: bytes) -> None:
        try:
            d = json.loads(payload_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.counts["bad_json"] += 1
            return
        if not isinstance(d, dict):
            return
        info = parse_status_payload(d, fallback_pk=extract_pubkey_from_topic(topic))
        if info is None:
            return
        upsert_contact(
            self.db, info["pubkey"], name=info.get("name"),
            role=info.get("role"), lat=info.get("lat"), lng=info.get("lng"),
        )
        self.counts["status"] += 1
        self._maybe_commit()

    def handle_packet(self, topic: str, payload_bytes: bytes) -> None:
        try:
            d = json.loads(payload_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.counts["bad_json"] += 1
            return
        if not isinstance(d, dict):
            return
        pkt = parse_packet_payload(d, fallback_pk=extract_pubkey_from_topic(topic))
        if pkt is None:
            return

        # Decode ADVERTs to extract source pubkey + name + lat/lng + inline path
        source_pk = None
        if pkt["payload_type"] == 4 and pkt["raw_hex"]:
            try:
                adv = decode_advert(bytes.fromhex(pkt["raw_hex"]))
                source_pk = adv["pubkey"]
                upsert_contact(
                    self.db, adv["pubkey"], name=adv.get("name"),
                    role=adv.get("role"), lat=adv.get("lat"), lng=adv.get("lng"),
                    bump_advert=True,
                )
                # Brokers vary on whether they include the path field in the
                # published JSON. The inline_path from the decoder is always
                # authoritative — extract it from the raw packet bytes.
                if pkt["path_json"] == "[]" and adv.get("inline_path"):
                    pkt["path_json"] = json.dumps(adv["inline_path"])
            except (DecodeError, ValueError) as e:
                LOG.debug(f"decode failed for hash={pkt['packet_hash']}: {e}")
                self.counts["decode_fail"] += 1

        # Always remember the receiver too (so it appears in contacts even
        # before its own status message arrives)
        upsert_contact(self.db, pkt["receiver_pk"])

        try:
            cur = self.db.execute(
                """
                INSERT OR IGNORE INTO observations
                (packet_hash, receiver_pk, source_pk, payload_type,
                 rssi, snr, path_json, raw_hex)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pkt["packet_hash"], pkt["receiver_pk"], source_pk,
                    pkt["payload_type"], pkt["rssi"], pkt["snr"],
                    pkt["path_json"], pkt["raw_hex"],
                ),
            )
            if cur.rowcount > 0:
                self.counts["new_obs"] += 1
            else:
                self.counts["dup_obs"] += 1
        except sqlite3.Error as e:
            LOG.warning(f"DB insert failed: {e}")

        self._maybe_commit()

    def _maybe_commit(self) -> None:
        self._since_last_commit += 1
        if self._since_last_commit >= self._commit_every:
            self.db.commit()
            self._since_last_commit = 0


# ─────────────────────────────────────────────────────────────────────────────
# MQTT setup
# ─────────────────────────────────────────────────────────────────────────────


def load_config(path: str) -> dict:
    p = configparser.ConfigParser()
    if not Path(path).exists():
        sys.exit(f"[!] Config file '{path}' not found. Copy config.example.ini.")
    p.read(path)
    return {
        "mqtt": {
            "host": p.get("mqtt", "host"),
            "port": p.getint("mqtt", "port", fallback=1883),
            "transport": p.get("mqtt", "transport", fallback="tcp").lower(),
            "use_tls": p.getboolean("mqtt", "use_tls", fallback=False),
            "tls_verify": p.getboolean("mqtt", "tls_verify", fallback=True),
            "username": p.get("mqtt", "username", fallback="") or None,
            "password": p.get("mqtt", "password", fallback="") or None,
            "websocket_path": p.get("mqtt", "websocket_path", fallback="/mqtt"),
            "client_id": p.get("mqtt", "client_id", fallback="") or f"mc-tri-{uuid.uuid4().hex[:8]}",
            "status_topic": p.get("mqtt", "status_topic_pattern",
                                   fallback="meshcore/+/+/status"),
            "packets_topic": p.get("mqtt", "packets_topic_pattern",
                                    fallback="meshcore/+/+/packets"),
        },
        "db_path": p.get("storage", "db_path", fallback="./meshcore_data.db"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config(args.config)
    db = open_db(cfg["db_path"])
    counts = defaultdict(int)
    handler = Handler(db, counts)

    mq = cfg["mqtt"]
    LOG.info(f"Connecting to {mq['host']}:{mq['port']} via {mq['transport']} "
             f"(TLS={mq['use_tls']}, auth={'yes' if mq['username'] else 'no'})")

    # Using paho v1 callback API explicitly — works with both paho 1.x and 2.x
    # and matches the de-facto signature most MeshCore-related codebases use.
    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=mq["client_id"],
            transport=mq["transport"],
        )
    except (AttributeError, TypeError):
        # paho 1.x has no callback_api_version kwarg
        client = mqtt.Client(client_id=mq["client_id"], transport=mq["transport"])
    if mq["transport"] == "websockets":
        client.ws_set_options(path=mq["websocket_path"])
    if mq["use_tls"]:
        if mq["tls_verify"]:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        else:
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
    if mq["username"]:
        client.username_pw_set(mq["username"], mq["password"] or "")

    def on_connect(c, userdata, flags, rc):
        # paho v1 callback signature — rc is a plain int (0 = success).
        if rc == 0:
            LOG.info("Connected; subscribing to %s and %s",
                     mq["status_topic"], mq["packets_topic"])
            c.subscribe([(mq["status_topic"], 0), (mq["packets_topic"], 0)])
        else:
            LOG.error(f"Connect failed: rc={rc}")

    def on_disconnect(c, userdata, rc):
        if rc != 0:
            LOG.warning(f"Disconnected: rc={rc}; paho will reconnect")

    def on_message(c, userdata, msg):
        try:
            if mqtt.topic_matches_sub(mq["status_topic"], msg.topic):
                handler.handle_status(msg.topic, msg.payload)
            elif mqtt.topic_matches_sub(mq["packets_topic"], msg.topic):
                handler.handle_packet(msg.topic, msg.payload)
        except Exception as e:
            LOG.exception(f"unhandled error on {msg.topic}: {e}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    # Built-in auto-reconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    stop = False
    def _stop(sig, frm):
        nonlocal stop
        LOG.info(f"Stopping on signal {sig}")
        stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    client.connect_async(mq["host"], mq["port"], keepalive=60)
    client.loop_start()

    last_status = time.time()
    try:
        while not stop:
            time.sleep(1.0)
            now = time.time()
            if now - last_status >= 60:
                db.commit()
                n_obs = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                n_contacts = db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
                n_with_gps = db.execute(
                    "SELECT COUNT(*) FROM contacts WHERE lat IS NOT NULL AND lat != 0"
                ).fetchone()[0]
                LOG.info(
                    f"status: new_obs={counts['new_obs']} dup={counts['dup_obs']} "
                    f"status_msgs={counts['status']} bad_json={counts['bad_json']} "
                    f"decode_fail={counts['decode_fail']} | "
                    f"db: {n_obs} obs, {n_contacts} contacts ({n_with_gps} w/ GPS)"
                )
                last_status = now
    finally:
        client.loop_stop()
        client.disconnect()
        db.commit()
        LOG.info("Bye.")


if __name__ == "__main__":
    main()
