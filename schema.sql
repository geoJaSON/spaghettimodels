-- Schema for tropical-cyclone "spaghetti model" track guidance (NHC ATCF a-deck).
-- The Python loader creates this automatically; this file is for reference or
-- to apply manually (psql "$DATABASE_URL" -f schema.sql).

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS cyclone_model_tracks (
    id           BIGSERIAL PRIMARY KEY,
    storm_id     TEXT        NOT NULL,             -- e.g. ep022026
    basin        TEXT        NOT NULL,             -- AL, EP, CP, WP, ...
    cyclone_num  INT         NOT NULL,             -- annual cyclone number
    storm_name   TEXT,                             -- e.g. Boris (when known)
    model        TEXT        NOT NULL,             -- ATCF TECH: AVNO, HWRF, CMC...
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

CREATE INDEX IF NOT EXISTS idx_cmt_storm ON cyclone_model_tracks (storm_id, init_time, model);
CREATE INDEX IF NOT EXISTS idx_cmt_valid ON cyclone_model_tracks (valid_time);
CREATE INDEX IF NOT EXISTS idx_cmt_geom  ON cyclone_model_tracks USING GIST (geom);

-- One LINESTRING per (storm, model run, model) = one strand of spaghetti.
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
    ST_MakeLine(geom ORDER BY tau)   AS track
FROM cyclone_model_tracks
WHERE tau >= 0
  AND geom IS NOT NULL
  AND model <> 'CARQ'                -- analysis aid, not a forecast
GROUP BY storm_id, basin, cyclone_num, model, init_time
HAVING count(*) >= 2;
