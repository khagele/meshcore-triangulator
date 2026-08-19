#!/usr/bin/env python3
"""Regenerate fixtures/accuracy-cases.json from mc-radar (#68).

    python3 web-standalone/tools/build-fixture.py

Only needed when the fixture should be refreshed. The committed fixture lets
accuracy.mjs run offline, which matters because the upstream rate-limits.

Ground truth: for a target node whose position is known, the nodes that have a
verified link FROM the target (direction "to"/"both" in the target's own
connections) demonstrably received from it. Those are the 1st-hop observers.
The peers those observers in turn transmitted to are the 2nd-hop observers:
target -> parent -> observer, with the parent recorded as `via`. Anything
already 1st-hop, and the target itself, is excluded from the 2nd-hop set. The
estimator is then asked to find the target from the observers alone.

Without the 2nd-hop half the harness measured nothing for #46, #65, #66 and #67:
those code paths were simply never entered, and a zero delta read as "no
regression" when it meant "not exercised".

Three upstream quirks worth knowing, all learned the hard way:
  - A default urllib User-Agent gets HTTP 403. Set one explicitly.
  - The budget is 300 requests per 900 s, published on every response as
    ratelimit-limit / ratelimit-remaining / ratelimit-reset. Pacing off those
    headers is the difference between a walk and a wall of 429s: this walk needs
    ~900 requests, so it spans several windows by design.
  - Responses are cached under .cache/, so an interrupted run resumes rather
    than refetching. Nothing is cached for a failed request.
"""
import json, math, os, random, time
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
# A hub 1st-hop observer can have hundreds of peers, and dumping all of them in
# would measure a scenario no operator can produce: the 2nd-hop prefixes come
# from packet paths, so an operator enters a handful. The cap is per case and
# filled round-robin over the parents, so one hub cannot own the whole list.
# The uncapped count is recorded as secondHopEligible.
SECOND_HOP_PER_CASE = 8
NL_BBOX = (50.70, 53.65, 3.30, 7.30)
SEED = 20260817        # fixed so a regeneration is comparable to the last one

os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
random.seed(SEED)


# Published budget is 300 per 900 s, so the sustained rate is one request per
# 3 s. Asking faster does not get the data faster, it gets 429s and then a
# retry storm on top of an upstream that is already saying no. The 0.5 s of
# headroom is deliberate: the window is shared with anyone else using the API.
RATE_MIN_INTERVAL = 3.5
_last_request = [0.0]
_budget = {"remaining": None, "reset": None}


def _note_budget(headers):
    try:
        _budget["remaining"] = int(headers.get("ratelimit-remaining"))
        _budget["reset"] = int(headers.get("ratelimit-reset"))
    except (TypeError, ValueError):
        pass


def _throttle():
    # Spend the budget the server publishes rather than guess at it. When it is
    # gone, wait out the window instead of collecting 429s.
    if _budget["remaining"] is not None and _budget["remaining"] <= 2:
        wait = (_budget["reset"] or 900) + 5
        print(f"  rate budget spent, waiting {wait}s for the window", flush=True)
        time.sleep(wait)
        _budget["remaining"] = None
    gap = time.monotonic() - _last_request[0]
    if gap < RATE_MIN_INTERVAL:
        time.sleep(RATE_MIN_INTERVAL - gap)
    _last_request[0] = time.monotonic()


def _backoff(attempt, error=None):
    # A 429 still names its reset, so honour that before falling back to a
    # doubling wait.
    if error is not None:
        try:
            return time.sleep(min(900, int(error.headers.get("ratelimit-reset")) + 5))
        except (TypeError, ValueError, AttributeError):
            pass
    time.sleep(min(300, 30 * (2 ** attempt)))


def post(payload):
    request = urllib.request.Request(
        SEARCH, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    for attempt in range(6):
        _throttle()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                _note_budget(response.headers)
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 503) and attempt < 5:
                print(f"  {error.code}, backing off", flush=True)
                _backoff(attempt, error)
                continue
            raise
    raise RuntimeError("search failed after retries")


# Cache of every connections response we have pulled this run or a previous one,
# keyed by public key. Targets and 1st-hop observers both land here; own_links
# and the 2nd-hop walk read it rather than refetching.
connections_cache = {}


def get_connections(public_key):
    if public_key in connections_cache:
        return connections_cache[public_key]
    path = os.path.join(CACHE, public_key + ".json")
    if os.path.exists(path):
        with open(path) as handle:
            connections = json.load(handle).get("connections") or []
        connections_cache[public_key] = connections
        return connections
    request = urllib.request.Request(CONNECTED + public_key, headers={"User-Agent": UA})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            with open(path, "w") as handle:
                json.dump(data, handle)
            time.sleep(0.35)
            connections = data.get("connections") or []
            connections_cache[public_key] = connections
            return connections
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


def usable(connection):
    """A link solid enough to reason from, in either direction."""
    return (connection.get("confidence") or 0) >= MIN_CONF


def transmitted_to(connection):
    """Direction is relative to the queried node: "to"/"both" means the queried
    node transmitted and the peer RECEIVED. That peer is an observer of it."""
    return connection.get("direction") in ("to", "both")


# The node index is 16 paged POSTs and never changes mid-run, so it is cached
# too. An interrupted run then resumes without re-walking it.
index_path = os.path.join(CACHE, "index.json")
if os.path.exists(index_path):
    with open(index_path) as handle:
        index = json.load(handle)
    print(f"node index from cache: {len(index)} located nodes")
else:
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
    with open(index_path, "w") as handle:
        json.dump(index, handle)
    print(f"  {len(index)} located nodes")

candidates = [
    key for key, value in index.items()
    if value["device_type"] == 2
    and NL_BBOX[0] <= value["lat"] <= NL_BBOX[1]
    and NL_BBOX[2] <= value["lon"] <= NL_BBOX[3]
]
candidates.sort()      # dict order is insertion order; sort so the sample is reproducible
sample = random.sample(candidates, min(SAMPLE_SIZE, len(candidates)))
print(f"  {len(candidates)} NL repeaters, sampling {len(sample)}")

print("fetching target connections (cached under .cache/)...")
for position, key in enumerate(sample, 1):
    get_connections(key)
    if position % 25 == 0:
        print(f"  {position}/{len(sample)}", flush=True)


def first_hop_peers(target_key):
    """Peer keys that received from this target, nearest first."""
    target = index[target_key]
    peers = []
    for connection in get_connections(target_key):
        if not usable(connection) or not transmitted_to(connection):
            continue
        peer_key = connection.get("connected_node_public_key")
        peer = index.get(peer_key)
        if not peer or peer_key == target_key:
            continue
        peers.append((peer_key, connection,
                      haversine_km((peer["lat"], peer["lon"]), (target["lat"], target["lon"]))))
    peers.sort(key=lambda entry: entry[2])
    seen, unique = set(), []
    for entry in peers:
        if entry[0] in seen:
            continue
        seen.add(entry[0])
        unique.append(entry)
    return unique


# Only cases that will survive MIN_OBSERVERS are worth a 2nd-hop walk, and each
# walk costs one request per 1st-hop observer. Settle the case list first.
viable = {}
for key in sample:
    if key not in index:
        continue
    peers = first_hop_peers(key)
    if len(peers) >= MIN_OBSERVERS:
        viable[key] = peers
skipped = len(sample) - len(viable)

wanted = sorted({peer_key for peers in viable.values() for peer_key, _, _ in peers})
print(f"fetching 1st-hop observer connections: {len(wanted)} distinct nodes "
      f"across {len(viable)} cases...")
for position, key in enumerate(wanted, 1):
    get_connections(key)
    if position % 25 == 0:
        print(f"  {position}/{len(wanted)}", flush=True)

# A node's OWN links, oriented as index.html expects: direction is relative to
# the queried node, so "from"/"both" means that node received, which is the
# inbound evidence provenRadiusFromLinks() wants. Available for every node whose
# connections we fetched, which is now the targets plus every 1st-hop observer.
own_links = {}
for key, connections in connections_cache.items():
    entries = [
        {"km": c["distance_km"],
         "measured": c.get("signal_from_snr_mean") is not None,
         "inbound": c.get("direction") in ("from", "both")}
        for c in connections
        if usable(c) and isinstance(c.get("distance_km"), (int, float))
    ]
    if entries:
        own_links[key] = entries


def observer(peer_key, target, hop, measured, extra=None):
    peer = index[peer_key]
    record = {
        "id": peer_key[:8],
        "hop": hop,
        "lat": peer["lat"], "lon": peer["lon"],
        "link_count": peer["link_count"],
        "distanceKm": round(haversine_km((peer["lat"], peer["lon"]),
                                         (target["lat"], target["lon"])), 3),
        "measured": measured,
        # Present only where the observer's own links were fetched. Absent means
        # anchorRangeKm() falls through to the link_count tier, which is what the
        # app does for observers outside PROVEN_LINK_NODE_BUDGET.
        "links": own_links.get(peer_key)
    }
    if extra:
        record.update(extra)
    return record


def second_hop_observers(target_key, first_hop):
    """Peers of the 1st-hop observers, excluding the target and anything already
    1st-hop. Filled round-robin over the parents so a hub with 300 peers cannot
    crowd out the rest, and shuffled per target so the pick is not systematically
    the nearest or the furthest of a parent's peers."""
    target = index[target_key]
    excluded = {target_key} | {peer_key for peer_key, _, _ in first_hop}
    # Stable across runs and independent of dict ordering: seed off the key.
    rng = random.Random(SEED ^ int(target_key[:8], 16))
    queues, eligible = [], set()
    for parent_key, _, _ in first_hop:                   # nearest parent first
        options = []
        for connection in get_connections(parent_key):
            if not usable(connection) or not transmitted_to(connection):
                continue
            child_key = connection.get("connected_node_public_key")
            if child_key in excluded or child_key not in index:
                continue
            options.append((child_key, connection))
            eligible.add(child_key)
        rng.shuffle(options)
        queues.append((parent_key, options))
    picked, taken = [], set()
    while len(picked) < SECOND_HOP_PER_CASE:
        progressed = False
        for parent_key, options in queues:
            while options:
                child_key, connection = options.pop()
                if child_key in taken:
                    continue
                taken.add(child_key)
                parent = index[parent_key]
                child = index[child_key]
                picked.append(observer(
                    child_key, target, 2,
                    connection.get("signal_to_snr_mean") is not None,
                    {"via": parent_key[:8],
                     "viaKm": round(haversine_km((child["lat"], child["lon"]),
                                                 (parent["lat"], parent["lon"])), 3)}))
                progressed = True
                break
            if len(picked) >= SECOND_HOP_PER_CASE:
                break
        if not progressed:
            break
    return picked, len(eligible)


cases = []
for target_key, first_hop in viable.items():
    target = index[target_key]
    observers = [
        observer(peer_key, target, 1, connection.get("signal_to_snr_mean") is not None)
        for peer_key, connection, _ in first_hop
    ]
    second_hop, eligible = second_hop_observers(target_key, first_hop)
    cases.append({
        "targetId": target_key[:8],
        "target": {"lat": target["lat"], "lon": target["lon"]},
        "observers": observers,
        # 1st-hop only, as before: this is the reach of the direct evidence.
        "maxObserverKm": round(max(o["distanceKm"] for o in observers), 3),
        # Kept in their own list rather than mixed into observers, so a reader
        # (or an older harness) that ignores the field still measures exactly
        # the 1st-hop case it measured before.
        "secondHopObservers": second_hop,
        "secondHopEligible": eligible
    })
cases.sort(key=lambda case: case["targetId"])

with open(OUT, "w") as handle:
    json.dump({
        "generated": time.strftime("%Y-%m-%d"),
        "source": "mc-radar node-inspector, NL repeaters",
        "note": "1st-hop observers are peers that received from the target "
                "(direction to/both, confidence >= %d); 2nd-hop observers are "
                "peers that received from a 1st-hop observer, excluding the "
                "target and anything already 1st-hop, capped at %d per case "
                "and tagged with the parent they heard (via)" % (MIN_CONF, SECOND_HOP_PER_CASE),
        "seed": SEED,
        "cases": cases
    }, handle, indent=1)

total_observers = sum(len(c["observers"]) for c in cases)
total_second = sum(len(c["secondHopObservers"]) for c in cases)
with_links = sum(1 for c in cases for o in c["observers"] if o.get("links"))
second_with_links = sum(1 for c in cases for o in c["secondHopObservers"] if o.get("links"))
with_second = sum(1 for c in cases if c["secondHopObservers"])
print(f"\nwrote {OUT}")
print(f"  cases: {len(cases)}, skipped {skipped} with fewer than {MIN_OBSERVERS} observers")
print(f"  1st-hop observers: {total_observers}, with own link data: {with_links} "
      f"({100 * with_links / max(total_observers, 1):.0f}%)")
print(f"  2nd-hop observers: {total_second} across {with_second} cases, "
      f"with own link data: {second_with_links} "
      f"({100 * second_with_links / max(total_second, 1):.0f}%)")
