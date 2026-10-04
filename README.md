# farm-check

Live: https://farm-check-mu.vercel.app

Checks a supplier's farm list before it goes into an EUDR due diligence statement: are the plot geometries usable, and was any of the land deforested after the cut-off date of 31 December 2020?

Upload a GeoJSON or CSV of farm points and polygons. Each plot is validated and repaired where possible in PostGIS, intersected with the EU's 2020 forest map (JRC GFC2020) and with tree-cover loss since then (Hansen GFC), and given a status: `high`, `review`, `fix_data` or `low`. Results come back as a map, a GeoJSON and a CSV report.

The reference data covers Huila, Colombia's largest coffee-producing department. The sample farm list is synthetic: 66 invented farms placed in real Huila municipalities, with known problems mixed in.

## What it checks

| Check | How | Severity |
|---|---|---|
| Self-intersecting or otherwise invalid polygons | `ST_IsValid`, repaired with `ST_MakeValid(…, 'method=structure')`; the reason is kept | warning (repaired) |
| Latitude and longitude swapped | outside the declared country as uploaded, inside it after `ST_FlipCoordinates` | warning (repaired) |
| Outside the declared country | against the geoBoundaries ADM0 outline | error |
| Plot over 4 ha given as a point | EUDR requires a polygon for plots of more than 4 ha | error |
| Fewer than 6 decimal places | EUDR asks for 6; the parser keeps coordinates as `Decimal` so `2.100000` still counts as 6 | warning |
| Tiny (< 0.01 ha) or very large (> 50 ha) polygons, declared area off by more than 50% | area on the `geography` type | warning |
| The same farm twice | polygons overlapping by more than 10% of the smaller one, points within 5 m | warning |
| Municipality | `ST_PointOnSurface` against ADM2 polygons split with `ST_Subdivide` | info |
| Forest at the cut-off and loss since | see below | sets the status |

Points get a circular footprint with the declared area (1 ha if none) for the forest overlay.

### Forest overlay

The forest data is one 2-band raster aligned to the Hansen 30 m grid, loaded with `raster2pgsql` as 1,520 tiles of 256×256 pixels (about 190 MB in the database):

1. Hansen GFC v1.13 loss year: values 21–25 are tree-cover loss in 2021–2025.
2. JRC GFC2020 v4: the share of each 30 m pixel that the EU's 10 m map calls forest on 31 Dec 2020, averaged with `gdalwarp -r average`.

For each plot, `ST_Clip` cuts the tiles to the footprint and `ST_PixelAsCentroids` counts pixels whose centre falls inside. Plots under about 0.3 ha may contain no pixel centre, so for those every touched pixel counts. Pixel area is corrected for latitude, and sums are capped at the plot area.

- **high**: at least 0.1 ha of 2021–2025 loss on land the EU map shows as forest in 2020.
- **review**: some smaller loss on 2020 forest; or a quarter or more of the plot was forest in 2020, which for a working farm suggests clearing the loss data hasn't picked up; or no forest data.
- **low**: everything else. Tree-cover loss on land that wasn't forest in 2020, such as renewing old coffee or shade trees, is reported but doesn't raise the status.
- **fix_data**: any error-level issue.

Why two maps: the first version used only Hansen tree cover in 2000. In Huila that marks most coffee farms as forest, because shade-grown coffee has a tree canopy, so 46 of 62 sample plots came out as "review". The JRC map was made for the EUDR and separates forest from tree crops. With it, review drops to the cases worth a second look.

## Measured numbers

Local run on 4 Oct 2026: Apple Silicon laptop (8 cores), PostgreSQL 17.6 and PostGIS 3.5 in Docker, API through uvicorn. Script: `bench/run.py`, raw output in `bench/results.json`.

| Batch | Median time (3 runs) | Per plot |
|---|---|---|
| 66 plots (the sample) | 0.67 s | 10.1 ms |
| 500 | 4.7 s | 9.3 ms |
| 2,000 | 19.8 s | 9.9 ms |
| 5,000 | 64.6 s | 12.9 ms |

Time grows about linearly with plot count. Larger batches were made by copying sample plots and shifting each by up to 5 km.

The raster join (`ST_Intersects(forest.rast, plot.footprint)` for a 5,000-plot batch) takes **96 ms** with the GiST index on the tiles' convex hulls, and **18.4 s** with index and bitmap scans turned off. The plans are in `bench/results.json`.

On the sample: 4 high (the four plots placed on post-2020 loss patches), 5 review, 2 fix_data, 55 low. All planted problems are found; `tests/test_checks.py` asserts each one against `data/sample/answer_key.json`.

## Run it locally

```sh
./scripts/prepare_rasters.sh        # downloads ~650 MB, builds data/derived/huila_forest.tif
docker compose up -d db             # PostGIS 17-3.5 on :55433 (or set DATABASE_URL to any PostGIS database)
./scripts/load_reference.sh         # schema, raster tiles, boundaries, check function (needs psql + raster2pgsql)
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/make_sample.py
.venv/bin/uvicorn app.main:app --port 8077    # http://localhost:8077
.venv/bin/python -m pytest                     # 20 tests
```

## API

| | |
|---|---|
| `POST /api/batches` | multipart `file` (GeoJSON or CSV, ≤ 4 MB, ≤ 10,000 plots) and `declared_country` (ISO3, default `COL`). Runs all checks, returns the summary. |
| `GET /api/batches/{id}` | counts per status and per issue, total areas |
| `GET /api/batches/{id}/plots.geojson` | every plot with footprint, status, issues, forest numbers and loss per year |
| `GET /api/batches/{id}/report.csv` | one line per plot |

GeoJSON input follows the EUDR convention (`ProductionPlace`, `Area`); CSV needs `farm_id, latitude, longitude, area_ha` or a `geometry` column with GeoJSON.

## Deployment

The live demo runs the FastAPI app as a Vercel Function in Frankfurt (`fra1`), with PostGIS on Neon in the same region. Static files in `public/` are served by Vercel's CDN. The reference layers were loaded with `DATABASE_URL=<neon direct URL> ./scripts/load_reference.sh`; the whole database is 40 MB because Postgres compresses the raster tiles. The 66-plot sample takes about 1 s there. `infra/main.tf` is a sketch of an AWS alternative (ECS Fargate behind an ALB, RDS PostgreSQL with PostGIS, the database URL in Secrets Manager). It passes `terraform validate` but **has not been applied** to an AWS account.

## Layout

```
app/parse.py          GeoJSON/CSV parsing, precision and ring checks (no database)
app/main.py           FastAPI endpoints
sql/01_schema.sql     reference tables, batch and plot tables, GiST indexes
sql/02_checks.sql     check_batch(): every check as one set-based UPDATE over the batch
scripts/              raster preparation, reference loading, sample generator
public/               map UI (MapLibre) and the sample file
bench/run.py          batch-size and index benchmarks
infra/main.tf         Fargate + RDS sketch (not applied)
```

## Limits

- This is a screening tool, not a compliance decision. Both forest maps have known errors at the scale of single pixels, and a 30 m pixel is about 0.08 ha, a large share of a 1 ha farm.
- Counting by pixel centre over- or under-counts at plot edges. A fractional-coverage overlay (`ST_Intersection` of pixel polygons) would be more precise and slower.
- Loss after 2020 says trees were lost, not why. "Was this forest converted to agriculture?" needs the land use today, which neither dataset gives.
- Only Colombia's boundary and the Huila raster window are loaded. Other origins need their country outline and forest tiles.
- The sample farms are invented. Their positions are drawn on land the JRC map shows as non-forest, except the cases planted to test the forest checks.

## Data

- Hansen, M. C. et al., Global Forest Change 2000–2025, v1.13, University of Maryland. https://glad.earthengine.app/view/global-forest-change
- European Commission JRC, Global Forest Cover 2020 (GFC2020), v4. https://forobs.jrc.ec.europa.eu/GFC
- geoBoundaries, Colombia ADM0 and ADM2 (open license). https://www.geoboundaries.org
- File checksums in `data/raw/SHA256SUMS`.
