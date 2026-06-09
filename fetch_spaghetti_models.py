#!/usr/bin/env python3
"""
fetch_spaghetti_models.py

Download tropical-cyclone "spaghetti model" track guidance (NOAA/NHC ATCF
a-deck / "aid" files) and load it into a PostGIS-enabled PostgreSQL database.

Each ATCF a-deck line is one model's (TECH) forecast position at one lead
time (TAU). Grouped by (storm, model run, model) and ordered by TAU, those
points form one "strand" of the spaghetti plot. We store every point and also
expose a view that stitches each model run into a LINESTRING.

The script is IDEMPOTENT and meant to be run on a schedule. Re-running upserts
on (storm_id, init_time, model, tau), so running it (say) every hour across a
season quietly builds a complete archive of all the guidance that was issued.

Connection comes from the DATABASE_URL environment variable (or --dsn), e.g.
    postgresql://user:pass@host:5432/dbname

Examples
--------
    # Windows (PowerShell):  $env:DATABASE_URL = "postgresql://..."
    # Linux/macOS:           export DATABASE_URL="postgresql://..."

    python fetch_spaghetti_models.py                  # active storms (default)
    python fetch_spaghetti_models.py --all            # every storm this season
    python fetch_spaghetti_models.py --storm ep022026 al052026
    python fetch_spaghetti_models.py --year 2025 --all
    python fetch_spaghetti_models.py --dry-run        # parse only, no DB writes
"""

import argparse
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
AID_BASE = "https://ftp.nhc.noaa.gov/atcf/aid_public"
BTK_BASE = "https://ftp.nhc.noaa.gov/atcf/btk"   # best-track ("b-deck") files
USER_AGENT = "spaghetti-models-loader/1.0 (+https://www.nhc.noaa.gov)"
HTTP_TIMEOUT = 60  # seconds


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http_get(url):
    """GET a URL and return raw bytes (raises urllib.error.HTTPError on 4xx/5xx)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


# --------------------------------------------------------------------------- #
# Storm discovery
# --------------------------------------------------------------------------- #
def discover_active_storms():
    """Return [(storm_id, name)] for currently active systems from NHC."""
    raw = http_get(CURRENT_STORMS_URL)
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict):
        storms = data.get("activeStorms") or data.get("storms") or []
    elif isinstance(data, list):
        storms = data
    else:
        storms = []
    out = []
    for s in storms:
        sid = (s.get("id") or "").strip().lower()
        if re.fullmatch(r"[a-z]{2}\d{2}\d{4}", sid):
            out.append((sid, (s.get("name") or "").strip() or None))
    return out


def discover_season_storms(year):
    """Return [(storm_id, None)] for every a-deck file present for `year`."""
    html = http_get(f"{AID_BASE}/").decode("utf-8", "replace")
    ids = set()
    for basin, cy, yr in re.findall(r"a([a-z]{2})(\d{2})(\d{4})\.dat\.gz", html):
        if yr == str(year):
            ids.add(f"{basin}{cy}{yr}")
    return [(sid, None) for sid in sorted(ids)]


# --------------------------------------------------------------------------- #
# ATCF a-deck parsing
# --------------------------------------------------------------------------- #
def _parse_latlon(tok):
    """'139N' -> 13.9 ; '1045W' -> -104.5 ; bad/empty -> None."""
    tok = tok.strip()
    if not tok:
        return None
    hemi = tok[-1].upper()
    if hemi not in ("N", "S", "E", "W"):
        return None
    try:
        val = int(tok[:-1]) / 10.0
    except ValueError:
        return None
    return -val if hemi in ("S", "W") else val


def _parse_int(tok, zero_is_missing=False):
    tok = tok.strip()
    try:
        v = int(tok)
    except ValueError:
        return None
    if zero_is_missing and v == 0:
        return None
    return v


def parse_adeck(text, storm_id, storm_name=None, source_file=None):
    """
    Parse a raw ATCF a-deck into de-duplicated track-point dicts.

    Many lines repeat a position across the 34/50/64-kt wind-radii rows; the
    forecast position is identical, so we keep one row per
    (init_time, model, tau).
    """
    rows = {}
    for line in text.splitlines():
        f = [c.strip() for c in line.split(",")]
        if len(f) < 8 or not f[2]:
            continue
        try:
            init = datetime.strptime(f[2], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        basin = f[0].upper()
        cyclone_num = _parse_int(f[1]) or 0
        model = f[4]
        tau = _parse_int(f[5])
        if model == "" or tau is None:
            continue

        lat = _parse_latlon(f[6])
        lon = _parse_latlon(f[7])
        vmax = _parse_int(f[8], zero_is_missing=True) if len(f) > 8 else None
        mslp = _parse_int(f[9], zero_is_missing=True) if len(f) > 9 else None
        storm_type = f[10] if len(f) > 10 and f[10] else None

        key = (init.isoformat(), model, tau)
        if key in rows:
            continue  # first wins; positions across RAD rows are identical
        rows[key] = {
            "storm_id": storm_id,
            "basin": basin,
            "cyclone_num": cyclone_num,
            "storm_name": storm_name,
            "model": model,
            "init_time": init,
            "tau": tau,
            "valid_time": init + timedelta(hours=tau),
            "lat": lat,
            "lon": lon,
            "vmax_kt": vmax,
            "mslp_mb": mslp,
            "storm_type": storm_type,
            "source_file": source_file,
        }
    return list(rows.values())


def fetch_storm_points(storm_id, storm_name=None):
    """Download and parse one storm's a-deck. Returns [] if the file is absent."""
    fname = f"a{storm_id}.dat.gz"
    url = f"{AID_BASE}/{fname}"
    try:
        blob = http_get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"  ! no a-deck found for {storm_id} ({url}) - skipping")
            return []
        raise
    text = gzip.decompress(blob).decode("utf-8", "replace")
    return parse_adeck(text, storm_id, storm_name, source_file=fname)


def fetch_best_track(storm_id, storm_name=None):
    """
    Download and parse a storm's b-deck (the observed "best track").

    Each BEST line is one observed fix; the ATCF date-time group is the *valid*
    (observation) time, so we map it straight onto valid_time. Returns [] if the
    file is absent.
    """
    fname = f"b{storm_id}.dat"
    url = f"{BTK_BASE}/{fname}"
    try:
        text = http_get(url).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    out, seen = [], set()
    for r in parse_adeck(text, storm_id, storm_name, source_file=fname):
        if r["model"] != "BEST" or r["init_time"] in seen:
            continue
        seen.add(r["init_time"])
        out.append({
            "storm_id": r["storm_id"],
            "basin": r["basin"],
            "cyclone_num": r["cyclone_num"],
            "storm_name": storm_name,
            "valid_time": r["init_time"],   # DTG is the observation time
            "lat": r["lat"],
            "lon": r["lon"],
            "vmax_kt": r["vmax_kt"],
            "mslp_mb": r["mslp_mb"],
            "storm_type": r["storm_type"],
            "source_file": r["source_file"],
        })
    return out


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA_SQL = [
    "CREATE EXTENSION IF NOT EXISTS postgis;",
    """
    CREATE TABLE IF NOT EXISTS cyclone_model_tracks (
        id           BIGSERIAL PRIMARY KEY,
        storm_id     TEXT        NOT NULL,             -- e.g. ep022026
        basin        TEXT        NOT NULL,             -- AL, EP, CP, WP, ...
        cyclone_num  INT         NOT NULL,             -- annual cyclone number
        storm_name   TEXT,                             -- e.g. Boris (when known)
        model        TEXT        NOT NULL,             -- ATCF TECH: AVNO, HWRF...
        init_time    TIMESTAMPTZ NOT NULL,             -- model run / warning DTG (UTC)
        tau          INT         NOT NULL,             -- forecast lead time, hours
        valid_time   TIMESTAMPTZ NOT NULL,             -- init_time + tau
        lat          DOUBLE PRECISION,
        lon          DOUBLE PRECISION,
        vmax_kt      INT,                              -- max sustained wind, knots
        mslp_mb      INT,                              -- min sea-level pressure, mb
        storm_type   TEXT,                             -- TY: DB, LO, TD, TS, HU...
        geom         geometry(Point, 4326),
        source_file  TEXT,
        fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_cmt UNIQUE (storm_id, init_time, model, tau)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_cmt_storm ON cyclone_model_tracks (storm_id, init_time, model);",
    "CREATE INDEX IF NOT EXISTS idx_cmt_valid ON cyclone_model_tracks (valid_time);",
    "CREATE INDEX IF NOT EXISTS idx_cmt_geom  ON cyclone_model_tracks USING GIST (geom);",
    """
    CREATE TABLE IF NOT EXISTS cyclone_best_track (
        id           BIGSERIAL PRIMARY KEY,
        storm_id     TEXT        NOT NULL,
        basin        TEXT        NOT NULL,
        cyclone_num  INT         NOT NULL,
        storm_name   TEXT,
        valid_time   TIMESTAMPTZ NOT NULL,             -- observation time (UTC)
        lat          DOUBLE PRECISION,
        lon          DOUBLE PRECISION,
        vmax_kt      INT,
        mslp_mb      INT,
        storm_type   TEXT,
        geom         geometry(Point, 4326),
        source_file  TEXT,
        fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_cbt UNIQUE (storm_id, valid_time)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_cbt_storm ON cyclone_best_track (storm_id, valid_time);",
    "CREATE INDEX IF NOT EXISTS idx_cbt_geom  ON cyclone_best_track USING GIST (geom);",
    """
    CREATE OR REPLACE VIEW cyclone_spaghetti_strands AS
    SELECT
        storm_id,
        basin,
        cyclone_num,
        max(storm_name)                  AS storm_name,
        model,
        init_time,
        count(*)                         AS n_points,
        min(valid_time)                  AS first_valid,
        max(valid_time)                  AS last_valid,
        ST_MakeLine(geom ORDER BY tau)   AS track   -- one spaghetti strand
    FROM cyclone_model_tracks
    WHERE tau >= 0
      AND geom IS NOT NULL
      AND model <> 'CARQ'                -- analysis aid, not a forecast
    GROUP BY storm_id, basin, cyclone_num, model, init_time
    HAVING count(*) >= 2;                -- need >= 2 points to draw a line
    """,
]

INSERT_SQL = """
INSERT INTO cyclone_model_tracks
    (storm_id, basin, cyclone_num, storm_name, model, init_time, tau,
     valid_time, lat, lon, vmax_kt, mslp_mb, storm_type, geom, source_file)
VALUES %s
ON CONFLICT (storm_id, init_time, model, tau) DO UPDATE SET
    lat         = EXCLUDED.lat,
    lon         = EXCLUDED.lon,
    vmax_kt     = EXCLUDED.vmax_kt,
    mslp_mb     = EXCLUDED.mslp_mb,
    storm_type  = EXCLUDED.storm_type,
    valid_time  = EXCLUDED.valid_time,
    geom        = EXCLUDED.geom,
    storm_name  = COALESCE(EXCLUDED.storm_name, cyclone_model_tracks.storm_name),
    source_file = EXCLUDED.source_file,
    fetched_at  = now();
"""

# geom is built from lon,lat (note: ST_MakePoint takes X=lon, Y=lat).
# ST_MakePoint is STRICT, so NULL inputs yield a NULL geometry (no error).
INSERT_TEMPLATE = (
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
    "ST_SetSRID(ST_MakePoint(%s,%s),4326),%s)"
)


def ensure_schema(conn):
    conn.autocommit = True  # let each DDL stand alone
    with conn.cursor() as cur:
        for i, stmt in enumerate(SCHEMA_SQL):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                if i == 0:  # CREATE EXTENSION postgis may need elevated rights
                    cur.execute("SELECT 1 FROM pg_extension WHERE extname='postgis'")
                    if cur.fetchone():
                        continue  # already installed; carry on
                    raise SystemExit(
                        "PostGIS is required but not installed and could not be "
                        f"created automatically: {e}\n"
                        "Ask your DB admin to run: CREATE EXTENSION postgis;"
                    )
                raise
    conn.autocommit = False


def upsert_points(conn, rows):
    from psycopg2.extras import execute_values

    # Final safeguard: a single INSERT ... ON CONFLICT cannot touch the same
    # conflict key twice (e.g. if a storm id was supplied more than once).
    deduped = {}
    for r in rows:
        deduped[(r["storm_id"], r["init_time"], r["model"], r["tau"])] = r
    rows = list(deduped.values())

    values = [
        (
            r["storm_id"], r["basin"], r["cyclone_num"], r["storm_name"],
            r["model"], r["init_time"], r["tau"], r["valid_time"],
            r["lat"], r["lon"], r["vmax_kt"], r["mslp_mb"], r["storm_type"],
            r["lon"], r["lat"],            # -> ST_MakePoint(lon, lat)
            r["source_file"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        execute_values(cur, INSERT_SQL, values,
                       template=INSERT_TEMPLATE, page_size=1000)
    conn.commit()


BEST_INSERT_SQL = """
INSERT INTO cyclone_best_track
    (storm_id, basin, cyclone_num, storm_name, valid_time,
     lat, lon, vmax_kt, mslp_mb, storm_type, geom, source_file)
VALUES %s
ON CONFLICT (storm_id, valid_time) DO UPDATE SET
    lat=EXCLUDED.lat, lon=EXCLUDED.lon, vmax_kt=EXCLUDED.vmax_kt,
    mslp_mb=EXCLUDED.mslp_mb, storm_type=EXCLUDED.storm_type,
    storm_name=COALESCE(EXCLUDED.storm_name, cyclone_best_track.storm_name),
    geom=EXCLUDED.geom, source_file=EXCLUDED.source_file, fetched_at=now();
"""
BEST_INSERT_TEMPLATE = (
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
    "ST_SetSRID(ST_MakePoint(%s,%s),4326),%s)"
)


def upsert_best_track(conn, rows):
    from psycopg2.extras import execute_values

    deduped = {}
    for r in rows:
        deduped[(r["storm_id"], r["valid_time"])] = r
    rows = list(deduped.values())

    values = [
        (
            r["storm_id"], r["basin"], r["cyclone_num"], r["storm_name"],
            r["valid_time"], r["lat"], r["lon"], r["vmax_kt"], r["mslp_mb"],
            r["storm_type"], r["lon"], r["lat"], r["source_file"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        execute_values(cur, BEST_INSERT_SQL, values,
                       template=BEST_INSERT_TEMPLATE, page_size=1000)
    conn.commit()


# --------------------------------------------------------------------------- #
# Config / CLI
# --------------------------------------------------------------------------- #
def load_dotenv_if_present():
    """Minimal .env loader (only sets vars that aren't already in the env)."""
    path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Load NHC ATCF spaghetti-model guidance into PostGIS."
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true",
                   help="Pull every storm with an a-deck file this season.")
    g.add_argument("--storm", nargs="+", metavar="ID",
                   help="Specific storm id(s), e.g. ep022026 al052026.")
    p.add_argument("--year", type=int, default=datetime.now(timezone.utc).year,
                   help="Season year for --all (default: current year).")
    p.add_argument("--dsn", default=os.environ.get("DATABASE_URL"),
                   help="Postgres DSN (default: $DATABASE_URL).")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and parse only; print a summary, write nothing.")
    return p.parse_args(argv)


def main(argv=None):
    load_dotenv_if_present()
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # 1. Decide which storms to fetch.
    if args.storm:
        targets = [(s.strip().lower(), None) for s in args.storm]
        log(f"Targets (explicit): {', '.join(t[0] for t in targets)}")
    elif args.all:
        targets = discover_season_storms(args.year)
        log(f"Targets (season {args.year}): {len(targets)} a-deck files")
    else:
        targets = discover_active_storms()
        if not targets:
            log("No active storms right now. Nothing to do.")
            return 0
        log("Active storms: "
            + ", ".join(f"{sid}{f' ({n})' if n else ''}" for sid, n in targets))

    # 2. Fetch + parse model guidance (a-deck) and best track (b-deck).
    all_rows, all_best = [], []
    for sid, name in targets:
        pts = fetch_storm_points(sid, name)
        if pts:
            n_models = len({r["model"] for r in pts})
            n_runs = len({r["init_time"] for r in pts})
            log(f"  {sid}: {len(pts):>6} points  "
                f"({n_models} models, {n_runs} model runs)")
            all_rows.extend(pts)
        bt = fetch_best_track(sid, name)
        if bt:
            log(f"  {sid}: {len(bt):>6} best-track fixes")
            all_best.extend(bt)

    if not all_rows and not all_best:
        log("Nothing parsed.")
        return 0
    log(f"Parsed {len(all_rows)} track points + {len(all_best)} best-track fixes.")

    # 3. Write (unless dry run).
    if args.dry_run:
        models = sorted({r["model"] for r in all_rows})
        log(f"DRY RUN - not writing. Distinct models: {', '.join(models)}")
        return 0

    if not args.dsn:
        sys.exit("No database DSN. Set DATABASE_URL or pass --dsn.")

    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required. Install with: pip install psycopg2-binary")

    log("Connecting to Postgres...")
    conn = psycopg2.connect(args.dsn)
    try:
        ensure_schema(conn)
        if all_rows:
            upsert_points(conn, all_rows)
        if all_best:
            upsert_best_track(conn, all_best)
    finally:
        conn.close()
    log(f"Done. Upserted {len(all_rows)} model points + "
        f"{len(all_best)} best-track fixes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
