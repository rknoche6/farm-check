import json

import pytest

from app.parse import InputError, parse_upload


def fc(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()


def feat(geom, **props):
    return {"type": "Feature", "geometry": geom, "properties": props}


def test_keeps_trailing_zero_precision():
    # 2.100000 has six decimals even though float("2.100000") == 2.1
    data = b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},' \
           b'"geometry":{"type":"Point","coordinates":[-75.900000,2.100000]}}]}'
    [row] = parse_upload("a.geojson", data)
    assert row.issues == []


def test_flags_low_precision_points():
    [row] = parse_upload("a.geojson", fc(feat({"type": "Point", "coordinates": [-75.9, 2.1]})))
    assert row.issues[0]["code"] == "low_precision"
    assert row.issues[0]["decimals"] == 1


def test_eudr_property_names():
    [row] = parse_upload("a.geojson", fc(feat({"type": "Point", "coordinates": [-75.912345, 2.112345]},
                                              ProductionPlace="HU-001", Area=1.5)))
    assert (row.farm_ref, row.declared_ha) == ("HU-001", 1.5)


def test_closes_open_ring():
    ring = [[-75.9, 2.1], [-75.89, 2.1], [-75.89, 2.11]]
    [row] = parse_upload("a.geojson", fc(feat({"type": "Polygon", "coordinates": [ring]})))
    assert "ring_not_closed" in [i["code"] for i in row.issues]
    assert row.geometry["coordinates"][0][0] == row.geometry["coordinates"][0][-1]


def test_csv_points():
    rows = parse_upload("a.csv", b"farm_id,latitude,longitude,area_ha\nF1,2.123456,-75.123456,1.2\nF2,,,\n")
    assert rows[0].geometry == {"type": "Point", "coordinates": [-75.123456, 2.123456]}
    assert rows[1].issues[0]["code"] == "empty_geometry"


def test_rejects_unreadable_files():
    with pytest.raises(InputError):
        parse_upload("a.geojson", b"{not json")
    with pytest.raises(InputError):
        parse_upload("a.csv", b"name,city\nx,y\n")
