"""Read a farm list (GeoJSON or CSV) into rows the database can check.

Two input shapes are accepted:

* GeoJSON FeatureCollection. Property names follow the EUDR geolocation
  GeoJSON convention ("ProductionPlace", "Area") with plain fallbacks
  ("farm_id", "area_ha").
* CSV with columns farm_id, latitude, longitude, area_ha and an optional
  "geometry" column holding a GeoJSON polygon.

Coordinates are parsed as Decimal so the number of decimal places survives:
EUDR asks for at least 6 decimals, and 2.100000 must not read as 2.1.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

MIN_DECIMALS = 6
MAX_ROWS = 10_000


class InputError(ValueError):
    """The file can't be read at all (as opposed to a problem with one plot)."""


@dataclass
class Row:
    row_no: int
    farm_ref: str | None
    declared_ha: float | None
    geometry: dict[str, Any] | None  # GeoJSON geometry with float coordinates
    issues: list[dict[str, Any]] = field(default_factory=list)


def _decimals(d: Decimal) -> int:
    exp = d.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _walk(coords: Any):
    """Yield every [x, y] position in a GeoJSON coordinates array."""
    if coords and isinstance(coords[0], (Decimal, int, float)):
        yield coords
    else:
        for c in coords:
            yield from _walk(c)


def _to_float(coords: Any) -> Any:
    if isinstance(coords, list):
        return [_to_float(c) for c in coords]
    return float(coords)


def _check_geometry(row: Row, geom: dict[str, Any]) -> None:
    gtype = geom.get("type")
    if gtype not in ("Point", "Polygon", "MultiPolygon"):
        row.issues.append({"code": "unsupported_geometry", "severity": "error", "type": gtype})
        return
    positions = list(_walk(geom.get("coordinates") or []))
    if not positions:
        row.issues.append({"code": "empty_geometry", "severity": "error"})
        return
    # A single vertex like 2.1234500 legitimately prints as 2.12345, so the rule
    # looks at the typical precision of the plot, not the worst vertex.
    decimals = sorted(_decimals(Decimal(str(v))) for p in positions for v in p[:2])
    typical = decimals[len(decimals) // 2]
    if typical < MIN_DECIMALS:
        row.issues.append({"code": "low_precision", "severity": "warning", "decimals": typical})
    if any(abs(float(p[0])) > 180 or abs(float(p[1])) > 90 for p in positions):
        # Could still be swapped lat/lon; the database tries flipping before giving up.
        row.issues.append({"code": "coordinate_out_of_range", "severity": "warning"})
    if gtype == "Polygon":
        for ring in geom["coordinates"]:
            if len(ring) < 4 or ring[0] != ring[-1]:
                row.issues.append({"code": "ring_not_closed", "severity": "warning"})
                ring.append(ring[0])
                break
    row.geometry = {"type": gtype, "coordinates": _to_float(geom["coordinates"])}


def _num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(Decimal(str(v)))
    except InvalidOperation:
        return None


def parse_geojson(text: str) -> list[Row]:
    try:
        doc = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as e:
        raise InputError(f"not valid JSON: {e.msg} at line {e.lineno}") from e
    if doc.get("type") != "FeatureCollection":
        raise InputError("expected a GeoJSON FeatureCollection")
    rows = []
    for i, f in enumerate(doc.get("features") or [], start=1):
        props = f.get("properties") or {}
        row = Row(
            row_no=i,
            farm_ref=str(props.get("ProductionPlace") or props.get("farm_id") or props.get("name") or "") or None,
            declared_ha=_num(props.get("Area", props.get("area_ha"))),
            geometry=None,
        )
        if f.get("geometry"):
            _check_geometry(row, f["geometry"])
        else:
            row.issues.append({"code": "empty_geometry", "severity": "error"})
        rows.append(row)
    return rows


def parse_csv(text: str) -> list[Row]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise InputError("CSV has no header row")
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    need_point = {"latitude", "longitude"} <= cols.keys()
    if not need_point and "geometry" not in cols:
        raise InputError("CSV needs latitude and longitude columns, or a geometry column")
    rows = []
    for i, rec in enumerate(reader, start=1):
        get = lambda k: (rec.get(cols[k]) or "").strip() if k in cols else ""
        row = Row(row_no=i, farm_ref=get("farm_id") or None, declared_ha=_num(get("area_ha")), geometry=None)
        if get("geometry"):
            try:
                _check_geometry(row, json.loads(get("geometry"), parse_float=Decimal))
            except json.JSONDecodeError:
                row.issues.append({"code": "unreadable_geometry", "severity": "error"})
        elif need_point and get("latitude") and get("longitude"):
            try:
                lat, lon = Decimal(get("latitude")), Decimal(get("longitude"))
            except InvalidOperation:
                row.issues.append({"code": "unreadable_geometry", "severity": "error"})
            else:
                _check_geometry(row, {"type": "Point", "coordinates": [lon, lat]})
        else:
            row.issues.append({"code": "empty_geometry", "severity": "error"})
        rows.append(row)
    return rows


def parse_upload(filename: str, data: bytes) -> list[Row]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise InputError("file is not UTF-8 text") from e
    name = (filename or "").lower()
    if name.endswith((".geojson", ".json")) or text.lstrip().startswith("{"):
        rows = parse_geojson(text)
    else:
        rows = parse_csv(text)
    if not rows:
        raise InputError("the file contains no plots")
    if len(rows) > MAX_ROWS:
        raise InputError(f"at most {MAX_ROWS} plots per upload (got {len(rows)})")
    return rows
