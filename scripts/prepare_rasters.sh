#!/usr/bin/env sh
# Downloads the forest data and builds data/derived/huila_forest.tif (2 bands, Hansen 30 m grid).
# Run from the repo root. Needs curl and docker. ~1.2 GB of downloads.
set -eu
GDAL="docker run --rm -v $PWD/data:/d ghcr.io/osgeo/gdal:alpine-small-latest"
WIN="-76.75 3.95 -74.35 1.45"   # ulx uly lrx lry: Huila plus a margin
mkdir -p data/raw data/derived
cd data/raw
for l in lossyear; do
  [ -f hansen_${l}_10N_080W.tif ] || curl -sSLo hansen_${l}_10N_080W.tif \
    "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2025-v1.13/Hansen_GFC-2025-v1.13_${l}_10N_080W.tif"
done
[ -f jrc_gfc2020_v4_N10_W80.tif ] || curl -sSLo jrc_gfc2020_v4_N10_W80.tif \
  "https://ies-ows.jrc.ec.europa.eu/iforce/gfc2020/download.py?version=v4&type=tile&lat=N10&lon=W80"
[ -f col_adm0.geojson ] || curl -sSLo col_adm0.geojson https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/gbOpen/COL/ADM0/geoBoundaries-COL-ADM0_simplified.geojson
[ -f col_adm2.geojson ] || curl -sSLo col_adm2.geojson https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/gbOpen/COL/ADM2/geoBoundaries-COL-ADM2_simplified.geojson
shasum -a 256 -c --ignore-missing SHA256SUMS
cd ../..
$GDAL sh -c "
for l in lossyear; do gdal_translate -q -projwin $WIN -co COMPRESS=DEFLATE -co TILED=YES /d/raw/hansen_\${l}_10N_080W.tif /d/derived/huila_\${l}.tif; done
gdal_translate -q -projwin $WIN -co COMPRESS=DEFLATE -co TILED=YES /d/raw/jrc_gfc2020_v4_N10_W80.tif /d/derived/huila_jrc10m.tif
gdalwarp -q -overwrite -te -76.75 1.45 -74.35 3.95 -tr 0.00025 0.00025 -r average -ot Float32 /d/derived/huila_jrc10m.tif /d/derived/jrc_frac.tif
gdal_translate -q -ot Byte -scale 0 1 0 100 /d/derived/jrc_frac.tif /d/derived/huila_jrc_pct.tif
gdalbuildvrt -q -separate /d/derived/stack.vrt /d/derived/huila_lossyear.tif /d/derived/huila_jrc_pct.tif
gdal_translate -q -co COMPRESS=DEFLATE -co TILED=YES /d/derived/stack.vrt /d/derived/huila_forest.tif
rm /d/derived/stack.vrt /d/derived/jrc_frac.tif"
echo "built data/derived/huila_forest.tif"
