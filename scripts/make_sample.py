"""Generate a synthetic farm list for southern Huila (Colombia's largest coffee
department) with known problems mixed in.

The farms are invented. Their locations are not: plots are placed inside real
municipalities, and a handful sit on pixels where Hansen GFC records forest
loss after 2020, so the forest check has something to find.

Writes:
  data/sample/huila_farms.geojson   the upload (EUDR-style properties)
  data/sample/huila_farms.csv       the point rows as CSV
  data/sample/answer_key.json       which rows got which problem (used by tests)

Usage: .venv/bin/python scripts/make_sample.py   (needs the db running)
"""
from __future__ import annotations

import json
import math
import os
import random
from pathlib import Path

import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://farm:farm@localhost:55433/farmcheck")
OUT = Path(__file__).resolve().parent.parent / "data" / "sample"
BBOX = (-76.35, 1.65, -75.35, 2.75)  # Pitalito, Garzón, La Plata, Acevedo area
SEED = 20261004
M_PER_DEG = 111_320.0


def _seven(v: float) -> float:
    """Round to 7 decimals, as GPS apps export them."""
    return round(v, 7)


def polygon_around(lon: float, lat: float, ha: float, rnd: random.Random, n: int = 7) -> list:
    """An irregular, valid polygon of roughly `ha` hectares around a point."""
    # Area of a regular n-gon with circumradius R is n/2 * R^2 * sin(2*pi/n).
    r_m = math.sqrt(ha * 10_000 / (n / 2 * math.sin(2 * math.pi / n)))
    start = rnd.uniform(0, 2 * math.pi)
    ring = []
    for k in range(n):
        a = start + 2 * math.pi * k / n + rnd.uniform(-0.25, 0.25)
        rr = r_m * rnd.uniform(0.9, 1.1)
        ring.append([_seven(lon + rr * math.cos(a) / (M_PER_DEG * math.cos(math.radians(lat)))),
                     _seven(lat + rr * math.sin(a) / M_PER_DEG)])
    ring.append(ring[0])
    return ring


def main() -> None:
    rnd = random.Random(SEED)
    with psycopg.connect(DB) as conn:
        # Random points inside municipalities in the bounding box, kept only where
        # the EU's 2020 map shows no forest (band 3 < 10%): farms sit on farmland.
        # `woods` are points deep in 2020 forest, for the "farm inside forest" case.
        pts = conn.execute(
            """WITH g AS (
                 SELECT (ST_Dump(ST_GeneratePoints(
                    ST_Intersection(ST_Union(geom), ST_MakeEnvelope(%s, %s, %s, %s, 4326)), 600, %s))).geom g
                 FROM admin_area WHERE iso = 'COL' AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326))
               SELECT ST_X(g.g), ST_Y(g.g), ST_Value(f.rast, 3, g.g) AS forest_pct
               FROM g JOIN forest f ON ST_Intersects(f.rast, g.g)""",
            [*BBOX, SEED % 1000, *BBOX],
        ).fetchall()
        land = [(x, y) for x, y, v in pts if v is not None and v < 10][:90]
        woods = [(x, y) for x, y, v in pts if v is not None and v >= 95][:2]
        # Patches of contiguous 2021-2025 loss of at least 0.3 ha: reclassify band 1
        # to recent-loss / other, polygonize each tile, keep the larger patches.
        # Split them by what the EU's 2020 map says was there (band 3):
        #   on_forest      the patch centre was >= 70% forest in 2020  -> deforestation
        #   off_forest     <= 10% forest in 2020 (tree crops, shade trees) -> not deforestation
        patches = conn.execute(
            """WITH p AS (
                 SELECT ST_PointOnSurface(d.geom) c, ST_Area(d.geom::geography) / 10000 ha, f.rast
                 FROM forest f,
                      ST_DumpAsPolygons(ST_Reclass(f.rast, 1, '[0-20]:0, [21-25]:1, (25-255]:0', '8BUI', 0)) d
                 WHERE f.rast && ST_MakeEnvelope(%s, %s, %s, %s, 4326) AND d.val = 1)
               SELECT ST_X(c), ST_Y(c), ST_Value(rast, 3, c)
               FROM p WHERE ha >= 0.3 ORDER BY md5(ST_AsText(c))""",
            list(BBOX),
        ).fetchall()
        loss = [(x, y) for x, y, m in patches if m is not None and m >= 70][:4]
        loss_off = [(x, y) for x, y, m in patches if m is not None and m <= 10][:2]
        conn.rollback()

    rnd.shuffle(land)
    feats, key = [], {}

    def add(geom, ref, area, problem=None):
        feats.append({"type": "Feature", "geometry": geom,
                      "properties": {"ProductionPlace": ref, "Area": area, "ProducerCountry": "CO"}})
        if problem:
            key.setdefault(problem, []).append(len(feats))

    # 34 clean polygons and 16 clean points.
    for i in range(34):
        lon, lat = land.pop()
        ha = round(rnd.uniform(0.6, 3.5), 2)
        add({"type": "Polygon", "coordinates": [polygon_around(lon, lat, ha, rnd)]}, f"HU-{i + 1:03d}", ha)
    for i in range(16):
        lon, lat = land.pop()
        add({"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}, f"HU-P{i + 1:02d}",
            round(rnd.uniform(0.5, 3.5), 2))

    # Plots on recent forest loss.
    for j, (lon, lat) in enumerate(loss):
        add({"type": "Polygon", "coordinates": [polygon_around(lon, lat, 1.5, rnd)]}, f"HU-L{j + 1}", 1.5,
            "forest_loss")
    # Recent tree loss on land that wasn't forest in 2020: reported, not deforestation.
    for j, (lon, lat) in enumerate(loss_off):
        add({"type": "Polygon", "coordinates": [polygon_around(lon, lat, 1.5, rnd)]}, f"HU-N{j + 1}", 1.5,
            "loss_off_forest")

    # Farms drawn inside land that was forest in 2020 with no recorded loss.
    for j, (lon, lat) in enumerate(woods):
        add({"type": "Polygon", "coordinates": [polygon_around(lon, lat, 1.2, rnd)]}, f"HU-F{j + 1}", 1.2,
            "inside_2020_forest")

    # Known data problems.
    lon, lat = land.pop()
    add({"type": "Point", "coordinates": [round(lat, 6), round(lon, 6)]}, "HU-S1", 1.2, "swapped_coordinates")
    lon, lat = land.pop()
    add({"type": "Point", "coordinates": [round(lat, 6), round(lon, 6)]}, "HU-S2", 2.0, "swapped_coordinates")
    lon, lat = land.pop()
    d = 0.0012
    add({"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat + d], [lon, lat + d], [lon + d, lat], [lon, lat]]]},
        "HU-B1", 1.0, "invalid_geometry")  # bowtie
    lon, lat = land.pop()
    add({"type": "Point", "coordinates": [round(lon, 3), round(lat, 3)]}, "HU-R1", 1.0, "low_precision")
    lon, lat = land.pop()
    add({"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}, "HU-G1", 6.5, "polygon_required")
    add({"type": "Point", "coordinates": [-78.52, -0.21]}, "HU-X1", 1.0, "outside_country")  # Quito
    lon, lat = land.pop()
    add({"type": "Polygon", "coordinates": [polygon_around(lon, lat, 0.004, rnd)]}, "HU-T1", 0.4, "tiny_area")
    # The same farm twice under two references.
    dup = feats[3]["geometry"]
    add(json.loads(json.dumps(dup)), "HU-D1", feats[3]["properties"]["Area"], "overlaps_plot")
    key.setdefault("overlaps_plot", []).append(4)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "huila_farms.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    with open(OUT / "huila_farms.csv", "w") as f:
        f.write("farm_id,latitude,longitude,area_ha\n")
        for ft in feats:
            if ft["geometry"]["type"] == "Point":
                x, y = ft["geometry"]["coordinates"]
                f.write(f'{ft["properties"]["ProductionPlace"]},{y},{x},{ft["properties"]["Area"]}\n')
    (OUT / "answer_key.json").write_text(json.dumps(key, indent=1))
    web = OUT.parent.parent / "public" / "sample"
    web.mkdir(parents=True, exist_ok=True)
    (web / "huila_farms.geojson").write_text((OUT / "huila_farms.geojson").read_text())
    print(f"{len(feats)} plots, {len(patches)} loss patches ({len(loss)} on forest, {len(loss_off)} off forest), problems: { {k: len(v) for k, v in key.items()} }")


if __name__ == "__main__":
    main()
