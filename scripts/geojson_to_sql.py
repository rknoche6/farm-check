"""Turn the geoBoundaries ADM0/ADM2 GeoJSON files into INSERT statements.

Usage: python3 scripts/geojson_to_sql.py ADM0.geojson ADM2.geojson | psql ...
Municipalities are cut into pieces of at most 256 vertices with ST_Subdivide.
"""
import json
import sys


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main(adm0_path: str, adm2_path: str) -> None:
    out = sys.stdout
    out.write("BEGIN;\nTRUNCATE country; TRUNCATE admin_area RESTART IDENTITY;\n")
    for f in json.load(open(adm0_path))["features"]:
        p = f["properties"]
        out.write(
            "INSERT INTO country VALUES ({iso}, {name}, "
            "ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON({g}), 4326))));\n".format(
                iso=q(p["shapeGroup"]), name=q(p["shapeName"]), g=q(json.dumps(f["geometry"]))
            )
        )
    for f in json.load(open(adm2_path))["features"]:
        p = f["properties"]
        out.write(
            "INSERT INTO admin_area (iso, shape_id, name, geom) "
            "SELECT {iso}, {sid}, {name}, ST_Subdivide(ST_CollectionExtract(ST_MakeValid("
            "ST_SetSRID(ST_GeomFromGeoJSON({g}), 4326)), 3), 256);\n".format(
                iso=q(p["shapeGroup"]), sid=q(p["shapeID"]), name=q(p["shapeName"]),
                g=q(json.dumps(f["geometry"])),
            )
        )
    out.write("COMMIT;\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
