#!/usr/bin/env python3
"""Regenerate fixtures/accuracy-cases.json from mc-radar (#68).

    python3 web-standalone/tools/build-fixture.py

Only needed when the fixture should be refreshed. The committed fixture lets
accuracy.mjs run offline, which matters because the upstream rate-limits.

Ground truth: for a target node whose position is known, the nodes that have a
verified link FROM the target (direction "to"/"both" in the target's own
connections) demonstrably received from it. Those are the observers. The
estimator is then asked to find the target from the observers alone.

Two upstream quirks worth knowing, both learned the hard way:
  - A default urllib User-Agent gets HTTP 403. Set one explicitly.
  - Sustained querying earns a 429. Responses are cached under .cache/ and
    re-running resumes rather than refetching.
"""
import json, math, os, random, sys, time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
OUT = os.path.join(HERE, "fixtures", "accuracy-cases.json")
SEARCH = "https://mc-radar.woodwar.com/api/node-inspector/search"
CONNECTED = "https://mc-radar.woodwar.com/api/node-inspector/connected/"
UA = "meshcore-triangulator-fixture/1.0 (+https://github.com/khagele/meshcore-triangulator)"

MIN_CONF = 85          # same threshold as PROVEN_LINK_MIN_CONFIDENCE in index.html
MIN_OBSERVERS = 3      # fewer than this is not a localisation problem
SAMPLE_SIZE = 200
NL_BBOX = (50.70, 53.65, 3.30, 7.30)
SEED = 20260817        # fixed so a regeneration is comparable to the last one

os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
random.seed(SEED)


def _backoff(attempt):
    time.sleep(min(300, 30 * (2 ** attempt)))


def post(payload):
    request = urllib.request.Request(
        SEARCH, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 503) and attempt < 5:
                print(f"  {error.code}, backing off", flush=True)
                _backoff(attempt)
                continue
            raise
    raise RuntimeError("search failed after retries")


def get_connections(public_key):
    path = os.path.join(CACHE, public_key + ".json")
    if os.path.exists(path):
        with open(path) as handle:
            return json.load(handle).get("connections") or []
    request = urllib.request.Request(CONNECTED + public_key, headers={"User-Agent": UA})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            with open(path, "w") as handle:
                json.dump(data, handle)
            time.sleep(0.35)
            return data.get("connections") or []
        except urllib.error.HTTPError as error:
            if error.code in (429, 503) and attempt < 5:
                print(f"  {error.code}, backing off", flush=True)
                _backoff(attempt)
                continue
            return []
        except Exception:
            if attempt == 5:
                return []
            time.sleep(2 * (attempt + 1))
    return []


def haversine_km(a, b):
    radius = 6371.0088
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat = lat2 - lat1
    dlon = math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


print("fetching node index...")
nodes, offset = [], 0
while offset < 8000:
    batch = post({"hasLocation": True, "isActive": True, "limit": 500, "offset": offset,
                  "sortBy": "last_seen", "sortOrder": "desc"}).get("nodes", [])
    if not batch:
        break
    nodes.extend(batch)
    offset += len(batch)
    if len(batch) < 500:
        break

index = {}
for node in nodes:
    location = node.get("location") or {}
    if location.get("latitude") is None:
        continue
    index[node["public_key"]] = {
        "lat": location["latitude"], "lon": location["longitude"],
        "link_count": int(node.get("link_count") or 0),
        "device_type": node.get("device_type")
    }
print(f"  {len(index)} located nodes")

candidates = [
    key for key, value in index.items()
    if value["device_type"] == 2
    and NL_BBOX[0] <= value["lat"] <= NL_BBOX[1]
    and NL_BBOX[2] <= value["lon"] <= NL_BBOX[3]
]
sample = random.sample(candidates, min(SAMPLE_SIZE, len(candidates)))
print(f"  {len(candidates)} NL repeaters, sampling {len(sample)}")

print("fetching connections (cached under .cache/)...")
connections_by_key = {}
for position, key in enumerate(sample, 1):
    connections_by_key[key] = get_connections(key)
    if position % 25 == 0:
        print(f"  {position}/{len(sample)}", flush=True)

# A node's OWN links, oriented as index.html expects: direction is relative to
# the queried node, so "from"/"both" means that node received, which is the
# inbound evidence provenRadiusFromLinks() wants.
own_links = {}
for key, connections in connections_by_key.items():
    entries = [
        {"km": c["distance_km"],
         "measured": c.get("signal_from_snr_mean") is not None,
         "inbound": c.get("direction") in ("from", "both")}
        for c in connections
        if (c.get("confidence") or 0) >= MIN_CONF
        and isinstance(c.get("distance_km"), (int, float))
    ]
    if entries:
        own_links[key] = entries

cases, skipped = [], 0
for key, connections in connections_by_key.items():
    target = index.get(key)
    if not target:
        continue
    observers = []
    for connection in connections:
        if (connection.get("confidence") or 0) < MIN_CONF:
            continue
        # "to" = target -> peer, i.e. THE PEER HEARD THE TARGET. That is an
        # observer. "from" is the target hearing the peer, which says nothing
        # about where the target is.
        if connection.get("direction") not in ("to", "both"):
            continue
        peer_key = connection.get("connected_node_public_key")
        peer = index.get(peer_key)
        if not peer:
            continue
        observers.append({
            "id": peer_key[:8],
            "lat": peer["lat"], "lon": peer["lon"],
            "link_count": peer["link_count"],
            "distanceKm": round(haversine_km((peer["lat"], peer["lon"]),
                                             (target["lat"], target["lon"])), 3),
            "measured": connection.get("signal_to_snr_mean") is not None,
            # Present only where the observer's own links were fetched. Absent
            # means anchorRangeKm() falls through to the link_count tier, which
            # is what the app does for observers outside its fetch budget.
            "links": own_links.get(peer_key)
        })
    if len(observers) < MIN_OBSERVERS:
        skipped += 1
        continue
    cases.append({
        "targetId": key[:8],
        "target": {"lat": target["lat"], "lon": target["lon"]},
        "observers": observers,
        "maxObserverKm": round(max(o["distanceKm"] for o in observers), 3)
    })

with open(OUT, "w") as handle:
    json.dump({
        "generated": time.strftime("%Y-%m-%d"),
        "source": "mc-radar node-inspector, NL repeaters",
        "note": "observers are peers that received from the target "
                "(direction to/both, confidence >= %d)" % MIN_CONF,
        "seed": SEED,
        "cases": cases
    }, handle, indent=1)

with_links = sum(1 for c in cases for o in c["observers"] if o.get("links"))
total_observers = sum(len(c["observers"]) for c in cases)
print(f"\nwrote {OUT}")
print(f"  cases: {len(cases)}, skipped {skipped} with fewer than {MIN_OBSERVERS} observers")
print(f"  observers with own link data: {with_links} of {total_observers} "
      f"({100 * with_links / max(total_observers, 1):.0f}%)")
