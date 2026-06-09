# Tropical Cyclone Spaghetti Models → PostGIS

Downloads tropical-cyclone "spaghetti model" track guidance from NOAA/NHC and
loads it into a PostGIS-enabled PostgreSQL database.

## What are "spaghetti models"?

They're the individual track forecasts from many numerical models plotted
together — they look like strands of spaghetti. The canonical machine-readable
source is NHC's **ATCF a-deck ("aid") files**. Each line is one model's
forecast position at one lead time:

```
EP, 02, 2026060512, 03, AVNO,  24, 145N, 1071W,  28, 1006, TS, ...
└basin └num └run(UTC)    └model └lead └lat  └lon  └wind └pres
```

`139N` = 13.9°N, `1045W` = 104.5°W (West → negative longitude). Grouping rows by
model and ordering by lead time gives each spaghetti strand.

Models you'll see include `AVNO` (GFS), `CMC` (Canadian), `UKX` (UKMET),
`HWRF`, `HMON`, `HFSA`/`HFSB` (HAFS), `NVGM` (NAVGEM), the `AP01`–`AP30` GEFS
ensemble members, consensus aids like `TVCN`, plus `OFCL` (official NHC
forecast) and `CARQ` (current analysis).

## Setup

```bash
pip install -r requirements.txt
```

Provide your Postgres connection as `DATABASE_URL` (or pass `--dsn`):

```powershell
# Windows PowerShell
$env:DATABASE_URL = "postgresql://user:pass@host:5432/dbname"
```
```bash
# Linux / macOS
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
```

Or create a `.env` file next to the script:

```
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

The database needs the **PostGIS** extension. The script runs
`CREATE EXTENSION IF NOT EXISTS postgis;` for you; if your DB user lacks
permission, ask an admin to run it once.

## Usage

```bash
python fetch_spaghetti_models.py                  # active storms (default)
python fetch_spaghetti_models.py --all            # every storm this season
python fetch_spaghetti_models.py --storm ep022026 al052026
python fetch_spaghetti_models.py --year 2025 --all
python fetch_spaghetti_models.py --dry-run        # parse only, write nothing
```

The default (no args) pulls only currently **active** storms from NHC's
`CurrentStorms.json`. Run it on a schedule and, because each run upserts on
`(storm_id, init_time, model, tau)`, you accumulate the full season archive as
storms come and go — safe to re-run as often as you like.

## Viewer

A small Flask app serves an interactive map + intensity graphs from the data
you've loaded:

```bash
pip install -r requirements.txt
python viewer.py        # -> http://127.0.0.1:5000
```

- **Map** (Leaflet): every model's forecast track as a strand of spaghetti.
  `OFCL` (official) is drawn bold; GEFS ensemble members are thin/grey; a red
  marker shows the current analysis position. The storm's **observed best track**
  (b-deck) is overlaid as a bold white line with dots — forecast vs. reality.
  **Hover any vertex** (forecast position) for its forecast hour, valid time,
  wind, pressure, and lat/lon. The map uses canvas rendering so it stays smooth
  even with the full ensemble shown.
- **Intensity graphs** (Chart.js): max wind and min pressure vs. forecast hour,
  one line per model, with the **observed** track overlaid in white for
  verification. The wind chart has **Saffir-Simpson** category bands as y-axis
  guides; the pressure chart is reversed (lower = stronger) with an
  intensity gradient.
- **Run slider / animation**: scrub the slider (or hit **Play**) to step through
  model runs and watch how the guidance shifted cycle-to-cycle.
- **Controls**: pick the storm and the model run; click model chips (or the
  Key / All / None presets) to toggle strands on the map and charts together.

> **Why is the pressure chart sometimes sparse on the latest run?** Most models
> only report `mslp` once their full-resolution run lands, which is a few hours
> after each synoptic cycle. On the freshest cycle only the interpolated early
> guidance (no pressure) has arrived — step back one run with the slider and the
> pressure lines fill in.

It reads the same `DATABASE_URL` / `.env`. API endpoints (`/api/storms`,
`/api/runs`, `/api/tracks`, `/api/intensity`, `/api/besttrack`) return
JSON/GeoJSON if you want to build your own front end.

## Scheduling

NHC issues new guidance after each synoptic cycle (00/06/12/18 UTC), with
files trickling in over the following hours. Running hourly is a fine default.

**Windows Task Scheduler** (runs hourly):
```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
    -Argument "C:\Users\jason\Documents\Dev\spaghettimodels\fetch_spaghetti_models.py" `
    -WorkingDirectory "C:\Users\jason\Documents\Dev\spaghettimodels"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "SpaghettiModels" -Action $action -Trigger $trigger
```
(Set `DATABASE_URL` as a machine/user environment variable, or use a `.env` file
in the working directory, so the scheduled task can see it.)

**cron** (Linux/macOS), every hour at :20:
```
20 * * * * cd /path/to/spaghettimodels && /usr/bin/python3 fetch_spaghetti_models.py >> fetch.log 2>&1
```

## Data model

`cyclone_model_tracks` — one row per `(storm, model run, model, lead-hour)`:

| column | meaning |
| --- | --- |
| `storm_id` | e.g. `ep022026` (basin + number + year) |
| `model` | ATCF technique, e.g. `AVNO`, `HWRF`, `CMC` |
| `init_time` | model run / warning time (UTC) |
| `tau` | forecast lead time in hours |
| `valid_time` | `init_time + tau` |
| `lat` / `lon` / `geom` | forecast position (`geom` = `Point,4326`) |
| `vmax_kt` / `mslp_mb` | intensity (wind kt, pressure mb; `NULL` if missing) |
| `storm_type` | `TD`, `TS`, `HU`, ... |

`cyclone_spaghetti_strands` (view) — one `LINESTRING` per `(storm, run, model)`,
ready to map.

## Example queries

Latest spaghetti plot for a storm, as GeoJSON (drop straight into Leaflet/Mapbox):
```sql
SELECT model, ST_AsGeoJSON(track) AS geojson
FROM cyclone_spaghetti_strands
WHERE storm_id = 'ep022026'
  AND init_time = (SELECT max(init_time) FROM cyclone_spaghetti_strands
                   WHERE storm_id = 'ep022026');
```

Just the latest GFS track points:
```sql
SELECT valid_time, lat, lon, vmax_kt, mslp_mb
FROM cyclone_model_tracks
WHERE storm_id = 'ep022026' AND model = 'AVNO'
  AND init_time = (SELECT max(init_time) FROM cyclone_model_tracks
                   WHERE storm_id = 'ep022026' AND model = 'AVNO')
  AND tau >= 0
ORDER BY tau;
```

How far apart are the models at 120 h (forecast spread / cone of uncertainty)?
```sql
SELECT model, lat, lon
FROM cyclone_model_tracks
WHERE storm_id = 'ep022026' AND tau = 120
  AND init_time = (SELECT max(init_time) FROM cyclone_model_tracks WHERE storm_id = 'ep022026')
ORDER BY model;
```

## Notes & caveats

- **Source:** `https://ftp.nhc.noaa.gov/atcf/aid_public/` (the public aids deck).
- **ECMWF (`EMX`):** the deterministic ECMWF track is often withheld from the
  public deck for licensing reasons; you may not see it for every storm.
- **Off-season:** with no active storms the default run is a no-op (exit 0).
- This loads forecast *guidance*. For verified best-track history, see the
  ATCF b-deck (`btk`) files.
