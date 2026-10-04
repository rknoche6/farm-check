"""Benchmarks: how check time grows with batch size, and what the raster index buys.

Usage: .venv/bin/python bench/run.py   (db running, API on :8077)
Writes bench/results.json.
"""
from __future__ import annotations

import copy
import json
import os
import platform
import random
import statistics
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("API", "http://localhost:8077")
DB = os.environ.get("DATABASE_URL", "postgresql://farm:farm@localhost:55433/farmcheck")
SAMPLE = json.loads((ROOT / "data" / "sample" / "huila_farms.geojson").read_text())


def scaled(n: int, rnd: random.Random) -> bytes:
    """n plots made by copying sample plots and shifting each by up to ~5 km."""
    feats = []
    base = [f for f in SAMPLE["features"] if f["properties"]["ProductionPlace"] != "HU-X1"]
    for i in range(n):
        f = copy.deepcopy(base[i % len(base)])
        dx, dy = rnd.uniform(-0.045, 0.045), rnd.uniform(-0.045, 0.045)

        def shift(c):
            return [shift(x) for x in c] if isinstance(c[0], list) else [round(c[0] + dx, 7), round(c[1] + dy, 7)]

        g = f["geometry"]
        if g["type"] == "Point":
            g["coordinates"] = shift(g["coordinates"]) if abs(g["coordinates"][0]) > 10 else g["coordinates"]
        else:
            g["coordinates"] = shift(g["coordinates"])
        f["properties"]["ProductionPlace"] = f"B-{i:05d}"
        feats.append(f)
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode()


def time_uploads(sizes, repeats=3):
    rnd = random.Random(7)
    out = []
    with httpx.Client(base_url=API, timeout=600) as c:
        for n in sizes:
            body = scaled(n, rnd)
            runs = []
            for _ in range(repeats):
                t0 = time.perf_counter()
                r = c.post("/api/batches", files={"file": ("bench.geojson", body)})
                wall = (time.perf_counter() - t0) * 1000
                r.raise_for_status()
                runs.append({"wall_ms": round(wall), "check_ms": r.json()["check_ms"]})
            med = statistics.median(x["wall_ms"] for x in runs)
            out.append({"plots": n, "median_wall_ms": round(med), "ms_per_plot": round(med / n, 2), "runs": runs})
            print(out[-1]["plots"], "plots:", out[-1]["median_wall_ms"], "ms")
    return out


INDEX_QUERY = """
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT p.id, count(*)
FROM plot p
JOIN forest f ON ST_Intersects(f.rast, p.footprint)
WHERE p.batch_id = (SELECT id FROM batch ORDER BY n_plots DESC, created_at DESC LIMIT 1)
GROUP BY p.id
"""


def index_effect():
    res = {}
    with psycopg.connect(DB) as conn:
        for label, setting in [("with GiST index", "on"), ("index disabled", "off")]:
            conn.execute(f"SET enable_indexscan = {setting}; SET enable_bitmapscan = {setting}")
            plans = [conn.execute(INDEX_QUERY).fetchone()[0][0] for _ in range(3)]
            res[label] = {
                "median_ms": round(statistics.median(p["Execution Time"] for p in plans), 1),
                "plan_nodes": sorted({n for n in _nodes(plans[0]["Plan"])}),
            }
            print(label, res[label]["median_ms"], "ms")
    return res


def _nodes(plan):
    yield plan["Node Type"] + (f" on {plan['Relation Name']}" if "Relation Name" in plan else "")
    for child in plan.get("Plans", []):
        yield from _nodes(child)


if __name__ == "__main__":
    with psycopg.connect(DB) as conn:
        pg = conn.execute("SELECT version(), postgis_full_version()").fetchone()
        tiles = conn.execute("SELECT count(*) FROM forest").fetchone()[0]
    results = {
        "date": time.strftime("%Y-%m-%d"),
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} cores",
        "postgres": pg[0].split(" on ")[0],
        "postgis": pg[1].split(" ")[1].strip('"'),
        "raster_tiles": tiles,
        "batch_sizes": time_uploads([66, 500, 2000, 5000]),
        "raster_index": index_effect(),
    }
    (ROOT / "bench" / "results.json").write_text(json.dumps(results, indent=1))
    print("wrote bench/results.json")
