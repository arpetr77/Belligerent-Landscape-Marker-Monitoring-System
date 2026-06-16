-- PostgreSQL + PostGIS schema
-- psql -d belliger_db -f schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE marker_classes (
    code        TEXT PRIMARY KEY,
    label_ru    TEXT NOT NULL,
    color_hex   TEXT NOT NULL DEFAULT '#888888',
    icon_name   TEXT
);

INSERT INTO marker_classes VALUES
    ('crater',      'Воронка / кратер',           '#E24B4A', 'ti-circle'),
    ('trench',      'Окоп / траншея',              '#EF9F27', 'ti-line'),
    ('ruin',        'Разрушенная постройка',       '#7F77DD', 'ti-building'),
    ('embankment',  'Бруствер / насыпь',           '#639922', 'ti-trending-up'),
    ('metal',       'Скопление металла / техники', '#888780', 'ti-car'),
    ('burn',        'Пожарище / гарь',             '#D85A30', 'ti-flame')
ON CONFLICT DO NOTHING;

CREATE TABLE missions (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          TEXT NOT NULL,
    flight_date   DATE,
    operator      TEXT,
    alt_m         FLOAT,
    area_wkt      TEXT,
    status        TEXT NOT NULL DEFAULT 'uploaded',  -- uploaded|processing|done|error
    gpx_url       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE images (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    mission_id         UUID NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    filename           TEXT NOT NULL,
    storage_url        TEXT,
    location           GEOGRAPHY(POINT, 4326),
    altitude_m         FLOAT,
    heading_deg        FLOAT,
    speed_kmh          FLOAT,
    satellites         INT,
    hdop               FLOAT,
    captured_at        TIMESTAMPTZ,
    processing_status  TEXT NOT NULL DEFAULT 'pending',  -- pending|done|error
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON images(mission_id);
CREATE INDEX ON images USING GIST(location);

CREATE TABLE markers (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    image_id      UUID NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    mission_id    UUID NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    geom          GEOGRAPHY(POINT, 4326) NOT NULL,
    marker_class  TEXT NOT NULL REFERENCES marker_classes(code),
    confidence    FLOAT NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    bbox_x        INT,
    bbox_y        INT,
    bbox_w        INT,
    bbox_h        INT,
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON markers(mission_id);
CREATE INDEX ON markers(marker_class);
CREATE INDEX ON markers USING GIST(geom);

-- Удобное view для API → GeoJSON
CREATE VIEW markers_geojson AS
SELECT
    m.id,
    m.mission_id,
    m.marker_class,
    mc.label_ru,
    mc.color_hex,
    m.confidence,
    ST_AsGeoJSON(m.geom)::json AS geometry,
    m.detected_at,
    i.filename AS source_image,
    i.storage_url AS image_url
FROM markers m
JOIN marker_classes mc ON mc.code = m.marker_class
JOIN images i ON i.id = m.image_id;
