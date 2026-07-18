# MC Repeater Map

Browser tool to estimate the location of an unknown MeshCore node from the
repeaters that heard it. Static `index.html` (Leaflet 2D + MapLibre 3D) plus a
small Python proxy (`server.py`) to the upstream APIs. Produces estimates, not
exact fixes.

## Run

```bash
python3 server.py          # serves http://127.0.0.1:8000 (port arg optional)
HOST=0.0.0.0 python3 server.py 8000   # bind for remote access — put a reverse proxy in front (see Notes)
```

Requires Python 3.7+ (stdlib only) and internet access.

To run persistently (survives reboots/crashes), use a systemd unit:

```ini
# /etc/systemd/system/mc-map.service
[Unit]
Description=MC Repeater Map
After=network.target

[Service]
WorkingDirectory=/path/to/mc-map
ExecStart=/usr/bin/python3 server.py 8000
Environment=HOST=127.0.0.1
Restart=on-failure
User=nobody

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now mc-map` and put nginx/Caddy in front for TLS + access control.

## Workflow

1. **Clues** — enter observer prefixes with optional weights (`93:0.8, 48, A8`), set cluster radius, find regions.
2. **Cluster** — pick a region. Duplicate-prefix nodes are auto-deduplicated (kept: nearest the cluster centre; removed ones stay listed, unchecked, with a reason). Edit observers (per-node select/weight); Apply enables only on change.
3. **Estimate** — weighted observer estimate; optional Terrain + LOS refinement (AHN, Netherlands only). Opens the 3D view tilted to the result.

## Map

- 2D (Leaflet/OSM): observers, proven-link range polygons (convex hull of proven peers), proven links (click → remove), estimate heat + candidate area.
- 3D (`3D View` button, or after Estimate): MapLibre + OpenFreeMap 3D buildings, docked in the map panel. Loads flat; tilts to perspective on Estimate. `2D ✕` returns.

## Data sources (proxied by server.py)

| Source | Use |
|---|---|
| mc-radar Node Inspector | Resolve prefixes; proven links. |
| map.meshcore.io | Global node feed (fallback). |
| PDOK AHN | Terrain/surface elevation (NL). |
| OpenFreeMap | 3D building tiles (no key). |

## Tunables (top of the `<script>` in `index.html`)

`REPEATER_ANTENNA_HEIGHT_M` (12), `TARGET_ANTENNA_HEIGHT_M` (1.5),
`DEFAULT_ANCHOR_RANGE_KM` (12), `SUPPORT_NODE_SCORE_WEIGHT` (0 — proven-link
nodes set range only, not position).

## Notes

Accuracy depends on observer count, GPS correctness and terrain. Proxy keeps a
browser-like User-Agent (upstreams sit behind Cloudflare). Don't expose the proxy
openly on the internet without a reverse proxy + access control.
