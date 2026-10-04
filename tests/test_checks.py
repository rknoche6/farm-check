"""End-to-end: upload the synthetic sample through the API and compare the flags
with the answer key written by scripts/make_sample.py. Needs the db running
with the reference layers loaded (docker compose up -d db && ./scripts/load_reference.sh).
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample"


@pytest.fixture(scope="module")
def result():
    with TestClient(app) as client:
        with open(SAMPLE / "huila_farms.geojson", "rb") as f:
            r = client.post("/api/batches", files={"file": ("huila_farms.geojson", f)})
        assert r.status_code == 201, r.text
        summary = r.json()
        plots = client.get(f"/api/batches/{summary['id']}/plots.geojson").json()
        report = client.get(f"/api/batches/{summary['id']}/report.csv")
    by_row = {f["properties"]["row"]: f["properties"] for f in plots["features"]}
    return summary, by_row, report


def codes(p):
    return {i["code"] for i in p["issues"]}


KEY = json.loads((SAMPLE / "answer_key.json").read_text())


@pytest.mark.parametrize("code", [c for c in KEY if c not in ("forest_loss", "loss_off_forest", "inside_2020_forest")])
def test_every_planted_problem_is_found(result, code):
    _, by_row, _ = result
    for row in KEY[code]:
        assert code in codes(by_row[row]), f"row {row} should have {code}, got {codes(by_row[row])}"


def test_plots_on_recent_loss_are_high_risk(result):
    _, by_row, _ = result
    for row in KEY["forest_loss"]:
        p = by_row[row]
        assert p["status"] == "high", p
        assert p["loss_on_forest_ha"] >= 0.1
        assert all(2021 <= int(y) <= 2025 for y in p["loss_by_year"])


def test_loss_on_non_forest_is_not_deforestation(result):
    _, by_row, _ = result
    for row in KEY["loss_off_forest"]:
        p = by_row[row]
        assert p["loss_after_2020_ha"] > 0
        assert p["loss_on_forest_ha"] < 0.1
        assert p["status"] != "high", p


def test_farm_inside_2020_forest_needs_review(result):
    _, by_row, _ = result
    for row in KEY["inside_2020_forest"]:
        assert by_row[row]["status"] in ("review", "high"), by_row[row]


def test_errors_block_the_plot(result):
    _, by_row, _ = result
    for row in KEY["outside_country"] + KEY["polygon_required"]:
        assert by_row[row]["status"] == "fix_data"


def test_swapped_points_are_repaired_into_colombia(result):
    _, by_row, _ = result
    for row in KEY["swapped_coordinates"]:
        assert by_row[row]["municipality"], by_row[row]


def test_report_has_one_line_per_plot(result):
    summary, _, report = result
    assert report.status_code == 200
    assert len(report.text.strip().splitlines()) == summary["n_plots"] + 1


def test_bad_upload_is_a_422():
    with TestClient(app) as client:
        r = client.post("/api/batches", files={"file": ("x.csv", b"name\nfoo\n")})
    assert r.status_code == 422
    assert "latitude" in r.json()["error"]
