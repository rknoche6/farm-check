#!/usr/bin/env sh
# Loads schema, forest raster and boundaries into the database at $DATABASE_URL
# (default: the docker compose db). Needs psql and raster2pgsql on PATH
# (PostGIS client tools, e.g. from conda-forge: postgis, gdal, postgresql).
# For Neon, use the direct (non-pooled) connection string for the bulk load.
# Run from the repo root: ./scripts/load_reference.sh
set -eu
DB="${DATABASE_URL:-postgresql://farm:farm@localhost:55433/farmcheck}"
PSQL="psql $DB -v ON_ERROR_STOP=1 -q"

$PSQL -f sql/01_schema.sql

# Forest raster: band 1 = Hansen GFC lossyear (0 = no loss, 1..25 = loss in 2001..2025),
# band 2 = JRC GFC2020 forest share (0..100).
# 256x256 tiles, with a GiST index on each tile's extent (-I)
# and constraints registered in raster_columns (-C).
echo "loading forest raster"
raster2pgsql -d -s 4326 -t 256x256 -I -C -M -N 255 data/derived/huila_forest.tif public.forest 2>/dev/null | $PSQL > /dev/null

echo "loading boundaries"
python3 scripts/geojson_to_sql.py data/raw/col_adm0.geojson data/raw/col_adm2.geojson | $PSQL

$PSQL -f sql/02_checks.sql
$PSQL <<'SQL'
ANALYZE country; ANALYZE admin_area; ANALYZE forest;
SELECT 'country' t, count(*) FROM country
UNION ALL SELECT 'admin_area pieces', count(*) FROM admin_area
UNION ALL SELECT 'forest tiles', count(*) FROM forest;
SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size;
SQL
