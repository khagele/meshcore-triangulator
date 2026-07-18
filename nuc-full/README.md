# meshcore-triangulator (NUC bundle)

Passive MeshCore node localisation. Runs on a Linux NUC (Ubuntu/Debian, x86-64):
a collector ingests MeshCore observations from an MQTT broker, and a locator
estimates node positions from them. Includes the mc-map web frontend.

## Components

| File | Role |
|---|---|
| `collector.py` | Subscribes to the MQTT broker, writes observations + contacts to SQLite. |
| `locate.py` | Triangulates one node (`--target`) or validates all GPS-known nodes (`--validate`). Chain-walk + weighted geometric median; optional terrain/LOS. |
| `targets.py` | Lists nodes with enough data to triangulate, ranked by tier. |
| `export_triangulator.py` | Locates every candidate → `web/triangulator-targets.json` (`--name`, `--tier-max` filters). |
| `download_dem.py` | Downloads Copernicus GLO-30 DEM tiles for terrain mode. |
| `meshcore_decoder.py`, `import_meshcore_map.py` | Packet decoding; map.meshcore.io GPS import. |
| `web/` | mc-map web frontend (see `web/README.md`), reads `triangulator-targets.json`. |

## Install

```bash
sudo ./install.sh
sudo nano /opt/meshcore-triangulator/config.ini   # broker host/auth, freq_mhz
sudo systemctl enable --now meshcore-triangulator
```

Config template: `config.example.ini`. Dependencies: `requirements.txt`
(`paho-mqtt`, `numpy`, `scipy`; `rasterio` optional for terrain).

## systemd services (enabled by install.sh)

| Unit | Function |
|---|---|
| `meshcore-triangulator.service` | Collector daemon. |
| `meshcore-export.timer` | Hourly export of `web/triangulator-targets.json`. |
| `meshcore-validate.timer` | Daily accuracy report (`validate-report.txt`). |
| `meshcore-map.service` | Web frontend on port 8000 (`http://<nuc-ip>:8000`). |
| `meshcore-prune.timer` | Weekly retention: delete observations older than 45 days (keeps contacts). |

## Accuracy

Estimates only. Typical error ~1–10 km in dense mesh, 20–50 km sparse. Depends on
relay density and GPS correctness. Verify with `locate.py --validate` on real data.

Full setup, DB migration, terrain mode and internet-serving notes: see `DEPLOY.md`.
Upstream triangulator: `brad28b/meshcore_mqtt_triangulator` (download_dem.py bbox fix applied).
