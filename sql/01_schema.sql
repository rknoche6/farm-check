-- Reference layers and batch tables. Everything is stored in EPSG:4326;
-- areas are computed on the geography type so they are in real square metres.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- Country outline (geoBoundaries ADM0) used for the "inside declared country" check.
CREATE TABLE IF NOT EXISTS country (
  iso   text PRIMARY KEY,
  name  text NOT NULL,
  geom  geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS country_geom_gix ON country USING gist (geom);

-- Municipalities (geoBoundaries ADM2), subdivided so point-in-polygon tests
-- touch small polygons instead of whole municipality outlines.
CREATE TABLE IF NOT EXISTS admin_area (
  id        serial PRIMARY KEY,
  iso       text NOT NULL,
  shape_id  text NOT NULL,
  name      text NOT NULL,
  geom      geometry(Polygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_area_geom_gix ON admin_area USING gist (geom);

-- One upload. declared_country is what the supplier says the plots are in.
CREATE TABLE IF NOT EXISTS batch (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at        timestamptz NOT NULL DEFAULT now(),
  filename          text,
  declared_country  text NOT NULL,
  n_plots           int NOT NULL DEFAULT 0,
  check_ms          int
);

-- One row per plot in the upload.
--   input_geom: exactly what was uploaded (kept for the audit trail)
--   geom:       the geometry the checks ran on after automatic repairs
--   footprint:  polygon used for the forest overlay (the plot itself, or a
--               circle of the declared area around a point)
CREATE TABLE IF NOT EXISTS plot (
  id               bigserial PRIMARY KEY,
  batch_id         uuid NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
  row_no           int NOT NULL,
  farm_ref         text,
  declared_ha      numeric,
  input_geom       geometry(Geometry, 4326),
  geom             geometry(Geometry, 4326),
  footprint        geometry(Geometry, 4326),
  area_ha          numeric,
  adm2             text,
  issues           jsonb NOT NULL DEFAULT '[]',
  forest2020_ha    numeric,   -- JRC GFC2020 forest inside the footprint
  loss_after_2020_ha numeric, -- Hansen tree-cover loss 2021-2025, any land
  loss_on_forest_ha numeric,  -- the part of that loss on land JRC mapped as forest in 2020
  loss_by_year     jsonb,
  risk             text,
  UNIQUE (batch_id, row_no)
);
CREATE INDEX IF NOT EXISTS plot_batch_idx ON plot (batch_id);
CREATE INDEX IF NOT EXISTS plot_geom_gix ON plot USING gist (geom);
CREATE INDEX IF NOT EXISTS plot_footprint_gix ON plot USING gist (footprint);
