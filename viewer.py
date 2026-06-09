#!/usr/bin/env python3
"""
viewer.py - tiny Flask app to view spaghetti-model tracks + intensity graphs.

Reads the same DATABASE_URL (env var or .env) used by the loader, queries the
cyclone_model_tracks table / cyclone_spaghetti_strands view, and serves:

    GET  /                      -> the single-page viewer (index.html)
    GET  /api/storms            -> storms present in the DB
    GET  /api/runs?storm_id=    -> available model-run times for a storm
    GET  /api/tracks?storm_id=&init_time=    -> GeoJSON spaghetti strands
    GET  /api/intensity?storm_id=&init_time= -> per-model wind/pressure series
    GET  /api/overlays?storm_id=           -> NHC cone, official track, warnings (GeoJSON)

Run:
    pip install -r requirements.txt
    python viewer.py            # -> http://127.0.0.1:5000
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, Response, jsonify, request, send_from_directory

from fetch_spaghetti_models import USER_AGENT, load_dotenv_if_present

load_dotenv_if_present()
DSN = os.environ.get("DATABASE_URL")
HERE = os.path.dirname(os.path.abspath(__file__))

NHC_MAPSERVER = (
    "https://mapservices.weather.noaa.gov/tropical/rest/services"
    "/tropical/NHC_tropical_weather_summary/MapServer"
)
NHC_LAYERS = {"cone": 7, "official_track": 6, "warnings": 8}
_OVERLAY_CACHE = {}  # storm_id -> (fetched_at, payload)
_OVERLAY_TTL = 300   # seconds

app = Flask(__name__)


def db():
    if not DSN:
        raise RuntimeError("DATABASE_URL is not set (env var or .env file).")
    return psycopg2.connect(DSN, cursor_factory=RealDictCursor)


def model_category(m):
    """Group an ATCF technique into a display category."""
    if m in ("OFCL", "OFCI"):
        return "official"
    if re.fullmatch(r"A[PC]\d{2}", m) or m in (
        "AEMN", "AEMI", "AEM2", "CEMN", "CEMI", "CEM2", "NNIB", "NNIC"):
        return "ensemble"
    if m in ("HWRF", "HWFI", "HWF2", "HMON", "HMNI", "HMN2", "HFSA", "HFSB",
             "HFAI", "HFBI", "CTCX", "CTCI", "CTC2", "GDMI", "GDMN", "GDM2"):
        return "hurricane"
    if m in ("AVNO", "AVNI", "AVN2", "GFSO", "CMC", "CMCI", "CMC2", "UKX",
             "UKXI", "UKX2", "EGRR", "EMX", "EMXI", "EMX2", "ECMO", "NVGM",
             "NVGI", "NVG2", "NGX", "NGX2", "NGPS"):
        return "global"
    if m in ("TVCN", "TVCA", "TVCC", "TVCE", "TVCX", "HCCA", "GFEX", "ICON",
             "IVCN", "RVCN", "FSSE"):
        return "consensus"
    if m in ("DSHP", "SHIP", "LGEM"):       # statistical INTENSITY guidance
        return "intensity"
    if m in ("DRCL", "CLP5", "OCD5", "SHF5", "TCLP",   # track baselines / niche aids
             "RI25", "XTRP", "TABS", "TABM", "TABD", "IVRI", "RYOC"):
        return "statistical"
    return "other"


def _nhc_geojson(layer_id):
    params = {
        "where": "1=1",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": "2000",
    }
    url = f"{NHC_MAPSERVER}/{layer_id}/query?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_nhc_overlays(storm_id):
    """Return NHC cone / official track / warnings for one storm (or empty)."""
    sid = storm_id.strip().lower()
    now = time.time()
    cached = _OVERLAY_CACHE.get(sid)
    if cached and now - cached[0] < _OVERLAY_TTL:
        return cached[1]

    out = {"storm_id": sid, "available": False, "layers": {}, "meta": {}}
    try:
        for key, layer_id in NHC_LAYERS.items():
            data = _nhc_geojson(layer_id)
            feats = [
                f for f in data.get("features", [])
                if (f.get("properties") or {}).get("idp_source", "").lower().startswith(sid)
            ]
            out["layers"][key] = {"type": "FeatureCollection", "features": feats}
            if feats:
                out["available"] = True
                props = feats[0].get("properties") or {}
                out["meta"][key] = {
                    "stormname": props.get("stormname"),
                    "advisnum": props.get("advisnum"),
                    "advdate": props.get("advdate"),
                    "fcstprd": props.get("fcstprd"),
                }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        out["error"] = str(exc)

    _OVERLAY_CACHE[sid] = (now, out)
    return out


def latest_run(cur, storm_id):
    cur.execute(
        "SELECT max(init_time) AS t FROM cyclone_model_tracks WHERE storm_id=%s",
        (storm_id,),
    )
    row = cur.fetchone()
    return row["t"] if row else None


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/api/storms")
def api_storms():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT storm_id,
                   basin,
                   max(storm_name)          AS name,
                   max(init_time)           AS latest_run,
                   count(DISTINCT init_time) AS runs
            FROM cyclone_model_tracks
            GROUP BY storm_id, basin
            ORDER BY latest_run DESC
        """)
        rows = cur.fetchall()
    for r in rows:
        r["latest_run"] = r["latest_run"].isoformat() if r["latest_run"] else None
    return jsonify(rows)


@app.route("/api/runs")
def api_runs():
    storm_id = request.args["storm_id"]
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT init_time FROM cyclone_model_tracks "
            "WHERE storm_id=%s ORDER BY init_time DESC",
            (storm_id,),
        )
        runs = [r["init_time"].isoformat() for r in cur.fetchall()]
    return jsonify(runs)


@app.route("/api/tracks")
def api_tracks():
    storm_id = request.args["storm_id"]
    init_time = request.args.get("init_time")
    with db() as conn, conn.cursor() as cur:
        if not init_time:
            t = latest_run(cur, storm_id)
            init_time = t.isoformat() if t else None

        # Per-vertex points (same filter as the strand view) so the front end
        # can draw the line AND attach hover detail to every forecast position.
        cur.execute("""
            SELECT model, tau, valid_time, lat, lon, vmax_kt, mslp_mb, storm_type
            FROM cyclone_model_tracks
            WHERE storm_id=%s AND init_time=%s::timestamptz
              AND tau >= 0 AND geom IS NOT NULL AND model <> 'CARQ'
            ORDER BY model, tau
        """, (storm_id, init_time))
        rows = cur.fetchall()

        # Current (analysis) position for a marker, if available.
        cur.execute("""
            SELECT lat, lon, vmax_kt, mslp_mb
            FROM cyclone_model_tracks
            WHERE storm_id=%s AND init_time=%s::timestamptz
              AND model='CARQ' AND tau=0
            LIMIT 1
        """, (storm_id, init_time))
        cur_pos = cur.fetchone()

    models = {}
    for r in rows:
        d = models.get(r["model"])
        if d is None:
            d = models[r["model"]] = {
                "model": r["model"],
                "category": model_category(r["model"]),
                "points": [],
            }
        d["points"].append({
            "tau": r["tau"],
            "valid_time": r["valid_time"].isoformat() if r["valid_time"] else None,
            "lat": r["lat"], "lon": r["lon"],
            "vmax_kt": r["vmax_kt"], "mslp_mb": r["mslp_mb"],
            "storm_type": r["storm_type"],
        })

    # Need >= 2 points to draw a strand (matches the view's HAVING clause).
    out = sorted((d for d in models.values() if len(d["points"]) >= 2),
                 key=lambda d: d["model"])
    return jsonify({"init_time": init_time, "current": cur_pos, "models": out})


@app.route("/api/besttrack")
def api_besttrack():
    storm_id = request.args["storm_id"]
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ST_AsGeoJSON(ST_MakeLine(geom ORDER BY valid_time)) AS line
            FROM cyclone_best_track
            WHERE storm_id=%s AND geom IS NOT NULL
        """, (storm_id,))
        row = cur.fetchone()
        line = json.loads(row["line"]) if row and row["line"] else None

        cur.execute("""
            SELECT valid_time, lat, lon, vmax_kt, mslp_mb, storm_type
            FROM cyclone_best_track
            WHERE storm_id=%s
            ORDER BY valid_time
        """, (storm_id,))
        points = []
        for r in cur.fetchall():
            points.append({
                "valid_time": r["valid_time"].isoformat() if r["valid_time"] else None,
                "lat": r["lat"], "lon": r["lon"],
                "vmax_kt": r["vmax_kt"], "mslp_mb": r["mslp_mb"],
                "storm_type": r["storm_type"],
            })
    return jsonify({"storm_id": storm_id, "line": line, "points": points})


@app.route("/api/overlays")
def api_overlays():
    storm_id = request.args.get("storm_id", "")
    if not storm_id:
        return jsonify({"error": "storm_id required"}), 400
    return jsonify(fetch_nhc_overlays(storm_id))


@app.route("/api/intensity")
def api_intensity():
    storm_id = request.args["storm_id"]
    init_time = request.args.get("init_time")
    with db() as conn, conn.cursor() as cur:
        if not init_time:
            t = latest_run(cur, storm_id)
            init_time = t.isoformat() if t else None
        cur.execute("""
            SELECT model, tau, valid_time, vmax_kt, mslp_mb
            FROM cyclone_model_tracks
            WHERE storm_id=%s AND init_time=%s::timestamptz AND tau >= 0
            ORDER BY model, tau
        """, (storm_id, init_time))
        rows = cur.fetchall()

    series = {}
    for r in rows:
        m = r["model"]
        if m not in series:
            series[m] = {"model": m, "category": model_category(m), "points": []}
        series[m]["points"].append({
            "tau": r["tau"],
            "valid_time": r["valid_time"].isoformat() if r["valid_time"] else None,
            "vmax_kt": r["vmax_kt"],
            "mslp_mb": r["mslp_mb"],
        })
    return jsonify({"init_time": init_time, "series": list(series.values())})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
