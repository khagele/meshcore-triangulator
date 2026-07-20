# AGENTS.md — meshcore-triangulator contributor guide

> For human contributors and AI agents alike. Read this before opening a PR or starting a task.

---

## 1. What is this

Localises MeshCore repeaters/nodes/traffic sources from mesh topology (who heard what, from
where), using RSSI/SNR-weighted clustering rather than GPS tracking of the target.

Two components:

1. **`web-standalone/`** — self-contained, no database. Resolves a node live against the public
   mc-radar / map.meshcore.io feeds. This is the actively developed, deployed component
   (live at [triangulator.dutchmeshcore.nl](https://triangulator.dutchmeshcore.nl/)).
2. **`nuc-full/`** — the full system: MQTT collector building a local observations database,
   hourly export, GPS-track "movers" view, node/target dashboards, systemd services. Only
   relevant if taking over the data-collection pipeline, not just the map.

---

## 2. Repo layout

```
README.md              Start here — which component to host, quick start for both
.github/workflows/      CI: docker-build.yml builds/publishes web-standalone image to GHCR

web-standalone/         Self-contained map tool (no DB)
  index.html            Map UI + triangulation logic (Leaflet, vanilla JS, :root CSS tokens)
  server.py             stdlib-only HTTP server + proxy for upstream feeds (mc-radar, meshcore.io, PDOK)
  Dockerfile            Alpine, non-root, stdlib-only
  docker-compose.yml    App + cloudflared sidecar, hardened (read-only rootfs, cap_drop ALL)
  .env.example           TUNNEL_TOKEN, optional image override

nuc-full/                Full pipeline (MQTT collector + DB + systemd services)
  collector.py           MQTT ingest → SQLite observations DB
  meshcore_decoder.py    Raw MeshCore ADVERT packet decoder (empirically-verified byte layout)
  locate.py               Core triangulation: clue parsing, clustering, weighted estimate, terrain refine
  track_locate.py / calibrate_track.py   GPS-track "movers" support
  export_triangulator.py  Hourly export job
  targets.py / prune_db.py  Dashboards / DB maintenance
  *.service / *.timer     systemd units (see DEPLOY.md, install.sh)
  config.example.ini      Template — copy to config.ini, fill in MQTT broker creds (gitignored)
  web/                    Same map UI as web-standalone, plus DB-backed features
    verified_positions.json  Operator-confirmed ground-truth anchors
```

---

## 3. Tech stack

| Layer | Technology |
|---|---|
| Web UI | Vanilla JS, Leaflet 1.9.4, plain CSS with `:root` custom-property tokens — no build step |
| Web server / proxy | Python 3.7+ stdlib only (`http.server`) — nothing to install for `web-standalone/` |
| Collector / triangulation (`nuc-full/`) | Python, `paho-mqtt`, `numpy`, `scipy`; optional `rasterio` for terrain mode |
| Storage (`nuc-full/`) | SQLite, no ORM |
| Container | Docker (Alpine, non-root), multi-arch GHA build → GHCR |
| Deploy | Cloudflare Tunnel (`web-standalone`) or systemd services (`nuc-full`) |
| MQTT broker | Whatever broker `config.ini` points at (dutchmeshcore.nl broker for the existing collector) |

---

## 4. Build, run, and test

### `web-standalone/`

```bash
cd web-standalone
python3 server.py          # http://127.0.0.1:8000 — nothing to install
```

Or via Docker — see the root README's Docker quick-start section.

### `nuc-full/`

```bash
cd nuc-full
pip install -r requirements.txt
cp config.example.ini config.ini   # fill in your MQTT broker details; gitignored, never commit
python3 collector.py               # or deploy via install.sh / the systemd units
```

### Tests

There is no automated test suite yet. Going forward:

- **Do** add unit tests (pytest) for new *pure logic* — clustering, weighted-estimate math,
  clue parsing, coordinate/terrain calculations (the kind of code in `locate.py`,
  `meshcore_decoder.py`). Keep such logic in small, importable functions so it stays testable.
- **Don't** retrofit tests onto existing untested code as a prerequisite for unrelated changes,
  and don't write tests for I/O/glue code (`server.py`, `collector.py`, `index.html`'s DOM/map
  wiring) — verify those by running them and checking behaviour manually.
- Before claiming a change done: run it (`python3 server.py` + load the page, or the relevant
  script) and confirm the actual behaviour, not just that it doesn't error at import time.

---

## 5. How we work

### 5.0 Start every task with an issue — problem/feature → issue → PR

Before starting a new feature or fix, open a GitHub issue for it:

> **problem or feature → issue → PR**

Write down the problem or feature and why it matters, expected behaviour, which component it
touches (`web-standalone` / `nuc-full`), and any repro steps/context. If a request is
underspecified, ask for the missing details before starting.

Link the PR back with a closing keyword (`Closes #<n>`) so merging auto-closes the issue.

### 5.1 Task execution

Keep changes small and focused — one logical change per issue/PR. For anything nontrivial,
sketch the approach before writing the real implementation.

### 5.2 Verify before claiming done

Run the affected script/page and observe the actual behaviour before saying a task is complete.
Intent is not verification.

---

## 6. Git conventions

### Branch policy

Kasper (maintainer) may push small fixes directly to `main`. Everyone else — contributors and AI
agents — works through issue → branch → PR; don't push directly to `main`.

### Staging

**Always stage named files.** Never `git add -A` / `git add .` — this repo has live credentials
in gitignored files (`config.ini`, `.env`); a broad add is how those leak.

```bash
# correct
git add web-standalone/index.html README.md

# never do this
git add -A
```

### Commits

One commit per logical change. Conventional commit style is fine but not strictly enforced:

```
feat(web-standalone): add GitHub link to map toolbar
fix(nuc-full): handle empty MQTT payload in collector
docs: update AGENTS.md
```

---

## 7. Hard rules

### MeshCore packet layout is empirically verified — never guess

`nuc-full/meshcore_decoder.py`'s header documents the ADVERT packet byte layout as *verified
against real packets*. If you're touching packet decoding and a field's layout isn't confirmed,
don't guess — leave it undecoded and say so in a comment, matching the existing style.

### No secrets in the repo

`nuc-full/config.ini` and `web-standalone/.env` are gitignored. Never commit broker URLs,
passwords, or tokens. Scrub infrastructure detail (hostnames, IPs) before publishing docs or
commit messages.

### Colours via the existing CSS custom properties

`web-standalone/index.html` defines its palette in a `:root` block (`--text`, `--muted`,
`--accent`, `--bg`, …). New component styles should use those variables rather than introducing
new hardcoded hex values.

### Don't hammer upstream feeds per-node

The proxied upstream APIs (mc-radar, map.meshcore.io, PDOK) are third-party and rate-limitable.
Batch/cache lookups (as the existing code does) rather than firing one request per node/prefix.

---

## 8. Working with Kasper interactively (AI agents)

For feature/change work initiated in conversation (not a pre-filed issue from someone else),
work in explicit stages rather than jumping to code:

1. Shape/refine the idea in discussion.
2. File the GitHub issue capturing the shaped result (§5.0) — before any mockup or code.
3. Produce a mockup/sketch of the intended result.
4. Explicitly repeat the pitch back in plain language.
5. Get a go-ahead before writing real code.
6. Get a go-ahead before pushing the PR.
7. Get a go-ahead before merging to `main`.

Skip stages only if Kasper explicitly says to move fast for that task.
