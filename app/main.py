"""HTTP API for the farm list checker.

POST /api/batches                 upload a GeoJSON or CSV farm list, run all checks
GET  /api/batches/{id}            summary: counts per status and per issue
GET  /api/batches/{id}/plots.geojson   every plot with its footprint, flags and forest numbers
GET  /api/batches/{id}/report.csv one line per plot, for spreadsheets
GET  /healthz
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .parse import InputError, parse_upload

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://farm:farm@localhost:55433/farmcheck")
# Vercel Functions accept request bodies up to 4.5 MB.
MAX_BYTES = 4 * 1024 * 1024
PUBLIC = Path(__file__).resolve().parent.parent / "public"

class _LazyPool:
    """Opens the pool on first use, so a cold serverless instance pays for it
    only when a request needs the database. prepare_threshold=None because
    Neon's pooled endpoint (PgBouncer, transaction mode) can't keep prepared
    statements across transactions."""

    _pool: ConnectionPool | None = None

    def connection(self):
        if self._pool is None:
            self._pool = ConnectionPool(
                DATABASE_URL, min_size=0, max_size=4, open=True,
                kwargs={"row_factory": dict_row, "prepare_threshold": None},
            )
        return self._pool.connection()


pool = _LazyPool()
app = FastAPI(title="farm-check")


@app.get("/healthz")
def healthz() -> dict:
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.post("/api/batches", status_code=201)
async def create_batch(file: UploadFile = File(...), declared_country: str = Form("COL")) -> dict:
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "file is larger than 4 MB")
    try:
        rows = parse_upload(file.filename or "", data)
    except InputError as e:
        raise HTTPException(422, str(e)) from e

    batch_id = uuid.uuid4()
    t0 = time.perf_counter()
    with pool.connection() as conn, conn.transaction():
        if not conn.execute("SELECT 1 FROM country WHERE iso = %s", [declared_country]).fetchone():
            raise HTTPException(422, f"no reference boundary loaded for country {declared_country!r}")
        conn.execute(
            "INSERT INTO batch (id, filename, declared_country) VALUES (%s, %s, %s)",
            [batch_id, file.filename, declared_country],
        )
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO plot (batch_id, row_no, farm_ref, declared_ha, input_geom, issues)
                   VALUES (%s, %s, %s, %s,
                           CASE WHEN %s::text IS NULL THEN NULL
                                ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326) END,
                           %s)""",
                [
                    (batch_id, r.row_no, r.farm_ref, r.declared_ha, g, g, Jsonb(r.issues))
                    for r in rows
                    for g in [json.dumps(r.geometry) if r.geometry else None]
                ],
            )
        conn.execute("SELECT check_batch(%s)", [batch_id])
        ms = round((time.perf_counter() - t0) * 1000)
        conn.execute("UPDATE batch SET check_ms = %s WHERE id = %s", [ms, batch_id])
    return summary(str(batch_id))


@app.get("/api/batches/{batch_id}")
def summary(batch_id: str) -> dict:
    with pool.connection() as conn:
        b = conn.execute("SELECT * FROM batch WHERE id = %s", [_uuid(batch_id)]).fetchone()
        if not b:
            raise HTTPException(404, "batch not found")
        risk = conn.execute(
            "SELECT coalesce(risk, 'fix_data') AS risk, count(*) AS n FROM plot WHERE batch_id = %s GROUP BY 1",
            [b["id"]],
        ).fetchall()
        issues = conn.execute(
            """SELECT i->>'code' AS code, i->>'severity' AS severity, count(*) AS n
               FROM plot, jsonb_array_elements(issues) i WHERE batch_id = %s
               GROUP BY 1, 2 ORDER BY 3 DESC""",
            [b["id"]],
        ).fetchall()
        totals = conn.execute(
            """SELECT round(sum(area_ha), 2) AS polygon_ha,
                      round(sum(loss_after_2020_ha), 2) AS loss_after_2020_ha,
                      round(sum(loss_on_forest_ha), 2) AS loss_on_forest_ha
               FROM plot WHERE batch_id = %s""",
            [b["id"]],
        ).fetchone()
    return {
        "id": str(b["id"]),
        "filename": b["filename"],
        "declared_country": b["declared_country"],
        "created_at": b["created_at"].isoformat(),
        "n_plots": b["n_plots"],
        "check_ms": b["check_ms"],
        "status": {r["risk"]: r["n"] for r in risk},
        "issues": issues,
        "totals": {k: float(v) if v is not None else None for k, v in totals.items()},
    }


@app.get("/api/batches/{batch_id}/plots.geojson")
def plots_geojson(batch_id: str) -> Response:
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT json_build_object('type', 'FeatureCollection', 'features', coalesce(json_agg(
                 json_build_object(
                   'type', 'Feature',
                   'geometry', ST_AsGeoJSON(coalesce(footprint, geom, input_geom), 6)::json,
                   'properties', json_build_object(
                     'row', row_no, 'farm_ref', farm_ref, 'status', coalesce(risk, 'fix_data'),
                     'kind', lower(GeometryType(coalesce(geom, input_geom))),
                     'municipality', adm2, 'area_ha', area_ha, 'declared_ha', declared_ha,
                     'forest2020_ha', forest2020_ha, 'loss_after_2020_ha', loss_after_2020_ha,
                     'loss_on_forest_ha', loss_on_forest_ha,
                     'loss_by_year', loss_by_year, 'issues', issues)
                 ) ORDER BY row_no), '[]'::json)) AS fc
               FROM plot WHERE batch_id = %s""",
            [_uuid(batch_id)],
        ).fetchone()
    return Response(json.dumps(row["fc"]), media_type="application/geo+json")


@app.get("/api/batches/{batch_id}/report.csv")
def report_csv(batch_id: str) -> Response:
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT row_no, farm_ref, coalesce(risk, 'fix_data') AS status,
                      lower(GeometryType(coalesce(geom, input_geom))) AS kind, adm2 AS municipality,
                      area_ha, declared_ha, forest2020_ha, loss_after_2020_ha, loss_on_forest_ha,
                      (SELECT string_agg(i->>'code', ' ') FROM jsonb_array_elements(issues) i) AS issues
               FROM plot WHERE batch_id = %s ORDER BY row_no""",
            [_uuid(batch_id)],
        ).fetchall()
    if not rows:
        raise HTTPException(404, "batch not found")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="farm-check-{batch_id}.csv"'})


def _uuid(s: str) -> uuid.UUID:
    try:
        return uuid.UUID(s)
    except ValueError as e:
        raise HTTPException(404, "batch not found") from e


@app.exception_handler(HTTPException)
def _http_error(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# On Vercel, public/ is served by the CDN before requests reach this app.
# Locally, this mount serves the same files.
if PUBLIC.is_dir():
    app.mount("/", StaticFiles(directory=PUBLIC, html=True), name="public")
