#!/usr/bin/env sh
# Loads the reference layers into the running db container.
# Run from the repo root: ./scripts/load_reference.sh
set -eu
PSQL="docker compose exec -T db psql -v ON_ERROR_STOP=1 -U farm -d farmcheck -q"

$PSQL -f /sql/01_schema.sql

# Forest raster: band 1 = Hansen GFC lossyear (0 = no loss, 1..25 = loss in 2001..2025),
# band 2 = treecover2000 (% canopy), band 3 = JRC GFC2020 forest share (0..100).
# 256x256 tiles, with a GiST index on each tile's extent (-I)
# and constraints registered in raster_columns (-C).
echo "loading forest raster"
docker compose exec -T db sh -c "raster2pgsql -d -s 4326 -t 256x256 -I -C -M -N 255 /data/derived/huila_forest.tif public.forest | psql -q -U farm -d farmcheck" > /dev/null

echo "loading boundaries"
docker compose exec -T db sh -c "ogr2ogr --version" > /dev/null 2>&1 && HAVE_OGR=1 || HAVE_OGR=0
if [ "$HAVE_OGR" = 0 ]; then
  # The PostGIS image has no ogr2ogr, so the GeoJSON goes in through ST_GeomFromGeoJSON.
  python3 scripts/geojson_to_sql.py data/raw/col_adm0.geojson data/raw/col_adm2.geojson | $PSQL
fi

$PSQL <<'SQL'
ANALYZE country; ANALYZE admin_area; ANALYZE forest;
SELECT 'country' t, count(*) FROM country
UNION ALL SELECT 'admin_area pieces', count(*) FROM admin_area
UNION ALL SELECT 'forest tiles', count(*) FROM forest;
SQL
