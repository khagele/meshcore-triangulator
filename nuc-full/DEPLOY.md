# Triangulator op een nieuwe NUC

Deze map is een schone deploybundel van `meshcore_mqtt_triangulator`, klaar voor een
Linux-NUC (Ubuntu/Debian, x86-64). De venv en de oude database zitten er bewust **niet**
in — die zijn host-specifiek (de meegekopieerde `meshcore_data.db` was bovendien corrupt,
gekopieerd terwijl de collector schreef). De `download_dem.py` bbox-fix is al toegepast.

## Snelle weg (aanbevolen)

Kopieer deze hele map naar de NUC en draai het installscript:

```bash
# vanaf je Mac:
scp -r triangulator-nuc/  user@nuc:~/

# op de NUC:
cd ~/triangulator-nuc
sudo ./install.sh
```

Het script installeert Python, maakt een service-user `mctri`, zet de app in
`/opt/meshcore-triangulator`, bouwt een verse venv, en installeert een systemd-service.
Daarna:

```bash
sudo nano /opt/meshcore-triangulator/config.ini   # broker host/auth, freq_mhz=869 voor EU
sudo systemctl enable --now meshcore-triangulator
journalctl -u meshcore-triangulator -f
```

## Je historische data meenemen (optioneel maar aangeraden)

De triangulatie wordt beter naarmate de database meer observaties bevat. Wil je de
historie van de Pi behouden, kopieer dan een **schone** kopie van de DB — niet het
corrupte bestand uit deze map. Maak op de bronmachine een consistente kopie met de
collector gestopt:

```bash
# op de Pi / bronmachine:
sudo systemctl stop <collector-service>          # of stop het collector-proces
sqlite3 meshcore_data.db ".backup meshcore_data.backup.db"

# naar de NUC:
scp meshcore_data.backup.db  user@nuc:/tmp/
# op de NUC:
sudo systemctl stop meshcore-triangulator
sudo mv /tmp/meshcore_data.backup.db /opt/meshcore-triangulator/meshcore_data.db
sudo chown mctri:mctri /opt/meshcore-triangulator/meshcore_data.db
sudo systemctl start meshcore-triangulator
```

Sla je dit over, dan begint de collector met een lege DB en bouwt hij vanzelf nieuwe
historie op (reken op minstens 24 uur, idealiter een week, voor goede resultaten).

## Automatische locatiedatabase (elk uur)

`install.sh` zet naast de collector ook een **uurlijkse export** aan
(`meshcore-export.timer`). Die draait `export_triangulator.py`: lokaliseert élke
node waarvoor genoeg data is en schrijft het resultaat naar één JSON:

```
/opt/meshcore-triangulator/triangulator-targets.json
```

Per node staat erin: `name`, geschatte `lat`/`lng`, `tier`/`role`, `error_km`
(als de node z'n eigen GPS uitzendt), aantal waarnemers/paden, en twee leesbare
UTC-timestamps:

- `last_seen` — wanneer de node voor het laatst door iemand op de broker gehoord is (laatste ontvangst)
- `last_advert` — wanneer de node voor het laatst zelf een advert (incl. naam/GPS) uitzond

De lijst is gesorteerd op `last_seen`, meest recent eerst.

Bedienen:

```bash
systemctl list-timers meshcore-export.timer   # wanneer draait de volgende run?
sudo systemctl start meshcore-export          # nu meteen draaien
journalctl -u meshcore-export -f              # voortgang volgen
```

Een volledige ronde duurt ~15–25 min voor ~2000 nodes; daarom uurlijks (nodes
adverteren toch maar ~elke 12u, dus vaker voegt weinig toe).

### Filteren

```bash
PY=/opt/meshcore-triangulator/.venv/bin/python
APP=/opt/meshcore-triangulator

# Alleen nodes met "maaskern" in de naam:
sudo -u mctri $PY $APP/export_triangulator.py --repo $APP --out $APP/maaskern.json --name maaskern

# Alleen de betrouwbaarste tiers (1-2):
sudo -u mctri $PY $APP/export_triangulator.py --repo $APP --out $APP/best.json --tier-max 2
```

Wil je vaker draaien dan elk uur, pas dan `OnUnitActiveSec=` aan in
`/etc/systemd/system/meshcore-export.timer` en doe `sudo systemctl daemon-reload`.

## Automatische nauwkeurigheidscheck (dagelijks)

Los van de export draait er ook een dagelijkse zelf-check (`meshcore-validate.timer`,
elke nacht om 04:00). Die draait `locate.py --validate`: trianguleert elke node die
z'n eigen GPS uitzendt en vergelijkt de schatting met de echte locatie, zodat je ziet
of de nauwkeurigheid goed blijft.

- Laatste rapport:  `/opt/meshcore-triangulator/validate-report.txt`
- Trend over tijd:  `/opt/meshcore-triangulator/validate-history.log` (alleen het samenvattingsblok)
- Nu meteen draaien: `sudo systemctl start meshcore-validate`
- Volgende run:      `systemctl list-timers meshcore-validate.timer`

Belangrijk: validate zegt pas iets zinnigs als de collector een paar dagen data heeft.
De eerste rapporten kunnen leeg of grillig zijn — dat is normaal.

## Webkaart (mc-map 2) op de NUC

De bundel bevat onder `web/` de mc-map 2-frontend, geïntegreerd met de localiser.
`install.sh` zet 'm aan als `meshcore-map.service` op poort 8000. Open in een browser
op je netwerk:

```
http://<nuc-ip>:8000
```

In het paneel **Triangulator overlay** kies je hoe jouw localiser-schattingen
samengevoegd worden met de nodes uit mc-radar / meshcore.io:

- **Uit** — alleen de bronnen, ongewijzigd.
- **Alleen aanvullen** — nodes zonder GPS in de bronnen krijgen jouw geschatte positie
  (oranje "📍 positie uit localiser"). Voorheen vielen die nodes helemaal weg.
- **Aanvullen + corrigeren** — bovendien: waar de bron-GPS méér dan de drempel (standaard
  3 km) afwijkt van jouw schatting, wordt de positie vervangen (rood "📍 gecorrigeerd").

Het vinkje **Toon alle localiser-schattingen als laag** tekent élke schatting als paarse
pin met foutcirkel, los van de zoek-workflow.

De data komt uit `web/triangulator-targets.json`, dat de uurlijkse export ververst —
zelfde machine, dus geen kopiëren nodig. Matchen gaat op de eerste 8 hex van de pubkey.

### Later op internet serveren

`server.py` bindt nu op `0.0.0.0` (bereikbaar op je LAN). Zet het **niet** zomaar open op
internet: er zit geen authenticatie of HTTPS op. Doe dit als je het publiek wilt:

1. Zet `MCMAP_HOST=127.0.0.1` in `/etc/systemd/system/meshcore-map.service` (alleen lokaal).
2. Plaats een reverse proxy ervoor (Caddy of nginx) die TLS regelt en doorzet naar
   `127.0.0.1:8000`. Caddy regelt automatisch een Let's Encrypt-certificaat.
3. Voeg in de proxy desgewenst Basic Auth of een andere toegangscontrole toe.
4. `sudo systemctl daemon-reload && sudo systemctl restart meshcore-map`.

Let op: de proxies in `server.py` (mc-radar, meshcore.io, PDOK) draaien dan vanaf de NUC.
Dat is prima voor privégebruik, maar zet er geen open relay van neer.

## Updates & aanpassingen

De bundel op je Mac is de bron; `/opt/meshcore-triangulator` is de live install.
Workflow voor een update:

```bash
# 1. vanaf je Mac: nieuwe bundel naar de NUC
rsync -av --exclude='__pycache__' \
  ~/Documents/Claude/Projects/obs/map/mesh/triangulator-nuc/ \
  <gebruiker>@<nuc-ip>:~/triangulator-nuc/

# 2. op de NUC: code naar /opt duwen + services herstarten
cd ~/triangulator-nuc && sudo ./update.sh
```

`update.sh` synct de code naar `/opt` (laat `config.ini`, de database en de venv met
rust), werkt eventueel gewijzigde dependencies bij, ververst de systemd-units,
en herstart de collector + webkaart. De timers (export/validate/prune) pakken de
nieuwe code vanzelf op bij hun volgende run. Browser hard-refreshen (Ctrl/Cmd+Shift+R)
voor frontend-wijzigingen.

Alleen de webkaart gewijzigd? Dan volstaat de `web/`-map syncen en
`sudo systemctl restart meshcore-map` (statische HTML: alleen browser verversen).

Tip voor doorlopend ontwikkelen: zet de bundel in een git-repo, dan is updaten op
de NUC `git pull` + `sudo ./update.sh`.

## Database-grootte / retentie

De `observations`-tabel groeit ~10 MB/dag per actief mesh. De locator gebruikt
alleen een tijdvenster (`days_lookback`, standaard 14 dagen) met indexen, dus een
grote DB vertraagt de schatting nauwelijks — maar het bestand blijft wel groeien.

`install.sh` zet daarom een **wekelijkse opschoonjob** aan (`meshcore-prune.timer`,
maandag 03:30) die observaties ouder dan **45 dagen** verwijdert. De `contacts`-tabel
(nodes + GPS) blijft volledig. Met WAL worden vrijgekomen pagina's hergebruikt, dus
de DB-grootte vlakt af rond het werkende venster.

```bash
systemctl list-timers meshcore-prune.timer     # volgende run
sudo systemctl start meshcore-prune            # nu draaien
journalctl -u meshcore-prune -f
```

Bewaarvenster aanpassen: wijzig `--days 45` in `/etc/systemd/system/meshcore-prune.service`
en `sudo systemctl daemon-reload`.

Het bestand écht laten krimpen (VACUUM) vraagt een exclusieve lock; doe dat met de
collector gestopt:

```bash
sudo systemctl stop meshcore-triangulator
sudo -u mctri /opt/meshcore-triangulator/.venv/bin/python \
     /opt/meshcore-triangulator/prune_db.py --repo /opt/meshcore-triangulator --days 45 --vacuum
sudo systemctl start meshcore-triangulator
```

## Terrain-mode (optioneel)

Voor terrein-bewuste triangulatie (line-of-sight via DEM-tegels):

```bash
sudo -u mctri /opt/meshcore-triangulator/.venv/bin/pip install rasterio
sudo -u mctri /opt/meshcore-triangulator/.venv/bin/python /opt/meshcore-triangulator/download_dem.py --auto
sudo nano /opt/meshcore-triangulator/config.ini   # [terrain] dem_dir = ./dem
```

DEM-tegels zijn 1°×1°, ~25 MB elk; een regio is al gauw 3–8 GB. De bbox-fix zorgt dat één
rotte GPS-rij niet meer de halve wereld aan tegels probeert te downloaden.

## Handmatig lokaliseren

```bash
sudo -u mctri /opt/meshcore-triangulator/.venv/bin/python /opt/meshcore-triangulator/targets.py
sudo -u mctri /opt/meshcore-triangulator/.venv/bin/python /opt/meshcore-triangulator/locate.py --target <pubkey-prefix>
```

## Handmatige install (zonder script)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.ini config.ini   # en invullen
.venv/bin/python collector.py       # draai onder systemd voor productie
```
