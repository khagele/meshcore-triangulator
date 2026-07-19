# MeshCore Triangulator — handoff bundle

Localises repeaters/nodes/traffic sources on MeshCore from the mesh topology
(who heard what, from where). Two components, pick based on what you're
hosting:

## `web-standalone/` — start here

Self-contained, no database (`index.html` + `server.py` proxy).
Resolves a node live against the public mc-radar / map.meshcore.io
feeds — nothing to install beyond Python 3.7+ stdlib, nothing to keep running
except the one process. **This is almost certainly what you want to host** —
lower maintenance, no persistent state to lose.

### Run with Docker (recommended)

See the [docker-compose.yml](web-standalone/docker-compose.yml) for how to use it.

A quick start is:

```bash
mkdir ~/triangulator
cd ~/triangulator
wget https://raw.githubusercontent.com/khagele/meshcore-triangulator/refs/heads/main/web-standalone/docker-compose.yml
wget https://raw.githubusercontent.com/khagele/meshcore-triangulator/refs/heads/main/web-standalone/.env.example
cp .env.example .env
```

`cloudflared` is built into the stack for ease of use:

1. In the Cloudflare Zero Trust dashboard (Networks → Tunnels), create a
   tunnel and copy its token into `TUNNEL_TOKEN` in `.env`.
2. Point the tunnel's public hostname at the service URL
   `http://triangulator:8000` (Docker DNS resolves the service name on the
   internal network shared with cloudflared).
3. `docker compose up -d`

### Run directly

```bash
cd web-standalone
python3 server.py          # http://127.0.0.1:8000
```

Bind for remote access with `HOST=0.0.0.0` — put a reverse proxy with TLS/access
control in front if you do; the proxy has no auth of its own.

### Data sources (proxied by server.py)

| Source | Use |
|---|---|
| mc-radar Node Inspector | Resolve prefixes; proven links. |
| map.meshcore.io | Global node feed (fallback). |
| PDOK AHN | Terrain/surface elevation (NL). |
| OpenFreeMap | 3D building tiles (no key). |

## `nuc-full/` — the full system, only if you're also taking over data collection

Everything above, plus: an MQTT collector that builds a local observations
database over time, hourly export, GPS-track "movers" view, node/target
dashboards, and a live-corrected localiser overlay fed by that database.
Meant to run as a set of systemd services (see `nuc-full/DEPLOY.md` and
`nuc-full/install.sh`).

**`config.ini` was deliberately left out** — it holds a live MQTT broker
password. Copy `config.example.ini` → `config.ini` and fill in your own
broker credentials (ask Kasper for the dutchmeshcore.nl broker details if
you're taking over the existing collector rather than pointing at your own).

Only reach for this one if you're standing up the whole pipeline, not just
the map.

## Why two copies

The original deployment (`nuc-full`) ran on an unreliable local NUC. The
`web-standalone` tool was built as a lighter, database-free fallback with the
same core triangulation logic, meant to run somewhere steadier. They're kept
in sync on the shared triangulator code (clue parsing, clustering, weighted
estimate, terrain refine, 2D/3D rendering) — `nuc-full` additionally has the
database-backed features (verified positions, augment/correct overlay,
Nodes/Targets/Movers/Monitoring) that `web-standalone` doesn't need or have.
