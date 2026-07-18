# MC Repeater Map (NUC build)

Browser frontend for the meshcore-triangulator bundle. One unified app with a
top-nav (Triangulator / Targets / Nodes / Movers) plus compact live status in
the nav bar. Static `index.html` (Leaflet 2D + MapLibre 3D) plus a Python proxy
(`server.py`). Served on the NUC by `meshcore-map.service` (port 8000). Produces
estimates, not exact fixes.

## Run

```bash
MCMAP_HOST=0.0.0.0 python3 server.py 8000   # http://<nuc-ip>:8000
```

Python 3.7+ (stdlib only), internet access.

## Workflow

1. **Clues** — observer prefixes in one field, comma-separated, with optional weights (`db:30, 23, db11:12` — the `:N` count is times heard, used as weight; 2/4/6 hex). First/second-hop and cluster radii sit on one compact row. Prefixes resolve against mc-radar (meshcore.io feed fallback) **plus the combined local Nodes database** (collector contacts + our localiser estimates), so locally-known repeaters join the clustering even when the external feeds miss them.
2. **Cluster** — pick a region. Duplicate-prefix nodes auto-deduplicated (kept: nearest the cluster centre; removed ones stay listed, unchecked, with a reason). Per-node select/weight; Apply enables only on change.
3. **Estimate** — weighted estimate; optional Terrain + LOS (AHN, NL only). 3D view tilts to the result.

## Map toolbar + Localiser overlay

A horizontal toolbar sits above the map with **Clear Map**, **3D View**, and the
**Localiser overlay** controls. The overlay is always on: local
`triangulator-targets.json` estimates (hourly export) **augment** missing GPS and
**correct** adverted GPS that diverges from the estimate by more than the
`correct if >` km threshold, matched by pubkey prefix.

- Both positions are drawn so you always see the shift: a green dot at the adverted position, a dashed connector to the corrected position, and the observer marker at the corrected position. Clustering and estimation use the corrected position.
- "Show estimates" draws every localiser estimate as a layer.

## Map

- 2D: observers, proven-link range polygons, proven links (click → remove), estimate heat + candidate area.
- 3D (`3D View`, or after Estimate): MapLibre + OpenFreeMap 3D buildings, docked in the map panel; flat by default, tilts on Estimate; `2D ✕` returns.

## Top-nav views (NUC build)

Compact live status in the nav bar polls `/api/status` (~20 s): collector health
(time since last reception), 24 h observation count and DB size, with full detail
on hover. The other views filter the data files:

- **Targets** — `triangulator-targets.json`: name/prefix search, tier, "heard within" window, sortable.
- **Nodes** — `contacts` table via `/api/contacts?search=&role=`, augmented with our estimates + per-node correction (km). Includes a **meshcore.io** column that opens the public map centred on each node.
- **Movers** — `/api/movers` + `/api/track`: GPS-sharing nodes that moved, drawn as a time-coloured path (blue→red). Movement is recorded forward-only from the collector update. For **GPS-less movers**, the "Compute track" button calls `/api/triangulated-track` (runs `track_locate.py`): windowed chain-walk per sliding window + a constant-velocity Kalman/RTS smoother → a smooth estimated path (dashed magenta) over the raw per-window dots. Per-window positions use an SNR distance prior (calibrated `snr_to_distance_km` rings for direct hearers, soft disks for first-hop relays) when direct observations are present, and each window reports a `sigma_km` that drives the smoother's measurement noise. Relay hops resolve by full pubkey prefix when the packet carries >1 byte/hop, else by the legacy 1-byte bucket.

`server.py` reads the DB read-only and `triangulator-targets.json` / `validate-report.txt`.

## Data sources (proxied by server.py)

mc-radar Node Inspector (prefix resolve, proven links), map.meshcore.io (node
feed fallback), PDOK AHN (terrain, NL), OpenFreeMap (3D building tiles, no key).
Local `triangulator-targets.json` is served statically.

## Tunables (top of the `<script>`)

`REPEATER_ANTENNA_HEIGHT_M` (12), `TARGET_ANTENNA_HEIGHT_M` (1.5),
`DEFAULT_ANCHOR_RANGE_KM` (12), `SUPPORT_NODE_SCORE_WEIGHT` (0 — proven-link
nodes set range only, not position).

Internet-serving notes: see `../DEPLOY.md`.
