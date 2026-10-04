-- check_batch(batch_id): runs every geometry and forest check for one upload.
-- Each step is one set-based UPDATE over the batch, so cost grows with the
-- number of plots, not with the number of checks times plots round trips.
--
-- Issue severities:
--   error   the plot can't go into a due diligence statement as it is
--   warning it was repaired automatically or looks suspicious; a person should glance at it

CREATE OR REPLACE FUNCTION add_issue(code text, severity text, detail jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_build_array(jsonb_build_object('code', code, 'severity', severity) || detail)
$$;

CREATE OR REPLACE FUNCTION check_batch(p_batch uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_country text;
  -- Hansen GFC pixels are 0.00025 degrees (about 27.8 m at the equator).
  c_pixel_deg constant float8 := 0.00025;
BEGIN
  SELECT declared_country INTO v_country FROM batch WHERE id = p_batch;

  -- 1. Geometry validity. Self-intersecting rings ("bowties") and similar are
  --    repaired with ST_MakeValid(method=structure), which keeps the outer area.
  UPDATE plot SET geom = input_geom WHERE batch_id = p_batch;

  UPDATE plot p SET
    issues = p.issues || add_issue('invalid_geometry', 'warning',
               jsonb_build_object('reason', ST_IsValidReason(p.input_geom), 'repaired', true)),
    geom = ST_CollectionExtract(ST_MakeValid(p.input_geom, 'method=structure'), 3)
  WHERE p.batch_id = p_batch
    AND GeometryType(p.input_geom) IN ('POLYGON', 'MULTIPOLYGON')
    AND NOT ST_IsValid(p.input_geom);

  UPDATE plot p SET
    issues = p.issues || add_issue('unrepairable_geometry', 'error'),
    geom = NULL
  WHERE p.batch_id = p_batch AND p.geom IS NOT NULL AND ST_IsEmpty(p.geom);

  -- 2. Swapped latitude/longitude: outside the declared country as uploaded,
  --    inside it with x and y flipped. Fixed automatically and flagged.
  UPDATE plot p SET
    issues = p.issues || add_issue('swapped_coordinates', 'warning', '{"repaired": true}'),
    geom = ST_FlipCoordinates(p.geom)
  FROM country c
  WHERE p.batch_id = p_batch AND c.iso = v_country AND p.geom IS NOT NULL
    AND NOT ST_Intersects(c.geom, p.geom)
    AND ST_Intersects(c.geom, ST_FlipCoordinates(p.geom));

  -- 3. Outside the declared country.
  UPDATE plot p SET
    issues = p.issues || add_issue('outside_country', 'error', jsonb_build_object('declared', v_country))
  WHERE p.batch_id = p_batch AND p.geom IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM country c WHERE c.iso = v_country AND ST_Intersects(c.geom, p.geom));

  -- 4. Area (polygons) on the geography type, in hectares.
  UPDATE plot p SET area_ha = round((ST_Area(p.geom::geography) / 10000)::numeric, 3)
  WHERE p.batch_id = p_batch AND GeometryType(p.geom) IN ('POLYGON', 'MULTIPOLYGON');

  UPDATE plot p SET issues = p.issues || add_issue('tiny_area', 'warning', jsonb_build_object('area_ha', p.area_ha))
  WHERE p.batch_id = p_batch AND p.area_ha < 0.01;

  UPDATE plot p SET issues = p.issues || add_issue('large_area', 'warning', jsonb_build_object('area_ha', p.area_ha))
  WHERE p.batch_id = p_batch AND p.area_ha > 50;

  UPDATE plot p SET issues = p.issues || add_issue('area_mismatch', 'warning',
           jsonb_build_object('declared_ha', p.declared_ha, 'area_ha', p.area_ha))
  WHERE p.batch_id = p_batch AND p.area_ha IS NOT NULL AND p.declared_ha > 0
    AND abs(p.area_ha - p.declared_ha) / p.declared_ha > 0.5;

  -- 5. EUDR: plots of more than 4 ha must be described with a polygon, not a point.
  UPDATE plot p SET issues = p.issues || add_issue('polygon_required', 'error', jsonb_build_object('declared_ha', p.declared_ha))
  WHERE p.batch_id = p_batch AND GeometryType(p.geom) = 'POINT' AND p.declared_ha > 4;

  -- 6. Footprint for the forest overlay: the polygon, or a circle with the
  --    declared area (1 ha if none) around a point.
  UPDATE plot p SET footprint = CASE
      WHEN GeometryType(p.geom) = 'POINT' THEN
        ST_Buffer(p.geom::geography, sqrt(coalesce(nullif(p.declared_ha, 0), 1) * 10000 / pi()), 16)::geometry
      ELSE p.geom END
  WHERE p.batch_id = p_batch AND p.geom IS NOT NULL;

  -- 7. Municipality of the plot's representative point.
  UPDATE plot p SET adm2 = a.name
  FROM admin_area a
  WHERE p.batch_id = p_batch AND p.geom IS NOT NULL
    AND a.iso = v_country AND ST_Intersects(a.geom, ST_PointOnSurface(p.geom));

  -- 8. Plots in the same upload that overlap by more than 10% of the smaller
  --    one, or points closer than 5 m (likely the same farm twice).
  UPDATE plot p SET issues = p.issues || add_issue('overlaps_plot', 'warning', jsonb_build_object('rows', o.rows))
  FROM (
    SELECT a.id, jsonb_agg(b.row_no ORDER BY b.row_no) AS rows
    FROM plot a JOIN plot b
      ON b.batch_id = a.batch_id AND b.id <> a.id AND ST_Intersects(a.footprint, b.footprint)
    WHERE a.batch_id = p_batch
      AND (
        (GeometryType(a.geom) = 'POINT' AND GeometryType(b.geom) = 'POINT'
           AND ST_DWithin(a.geom::geography, b.geom::geography, 5))
        OR (GeometryType(a.geom) <> 'POINT' AND GeometryType(b.geom) <> 'POINT'
           AND ST_Area(ST_Intersection(a.geom, b.geom)::geography)
               > 0.1 * least(ST_Area(a.geom::geography), ST_Area(b.geom::geography)))
      )
    GROUP BY a.id
  ) o
  WHERE p.id = o.id;

  -- 9. Forest overlay on a 2-band raster aligned to the Hansen 30 m grid:
  --    band 1  Hansen GFC loss year (21..25 = loss in 2021..2025)
  --    band 2  share of the pixel the EU's JRC GFC2020 map calls forest on
  --            31 Dec 2020, the EUDR cut-off (10 m map averaged to 30 m, 0..100)
  --    Forest area is the JRC share times pixel area, so a half-forest pixel
  --    counts half. Pixel area is corrected for latitude.
  WITH px AS (
    SELECT p.id,
           c.val::int AS lossyear,
           ST_Value(cl.rast, 2, c.x, c.y) / 100.0 AS forest_share,
           (c_pixel_deg * 111320.0) ^ 2 * cos(radians(ST_Y(c.geom))) / 10000 AS px_ha
    FROM plot p
    JOIN forest f ON ST_Intersects(f.rast, p.footprint)
    -- Plots under ~0.3 ha may contain no pixel centre at all; for those, count
    -- every pixel the footprint touches instead.
    CROSS JOIN LATERAL (SELECT ST_Clip(f.rast, p.footprint, true,
                          ST_Area(p.footprint::geography) < 3000) AS rast) cl
    CROSS JOIN LATERAL ST_PixelAsCentroids(cl.rast, 1) c
    WHERE p.batch_id = p_batch AND p.footprint IS NOT NULL
  ),
  agg AS (
    SELECT id,
      sum(px_ha * forest_share) AS forest2020_ha,
      coalesce(sum(px_ha) FILTER (WHERE lossyear BETWEEN 21 AND 25), 0) AS loss_ha,
      coalesce(sum(px_ha * forest_share) FILTER (WHERE lossyear BETWEEN 21 AND 25), 0) AS loss_forest_ha
    FROM px GROUP BY id
  ),
  yrs AS (
    SELECT id, jsonb_object_agg(2000 + lossyear, round(ha::numeric, 3)) AS by_year
    FROM (SELECT id, lossyear, sum(px_ha) ha FROM px WHERE lossyear BETWEEN 21 AND 25 GROUP BY id, lossyear) s
    GROUP BY id
  )
  -- Pixel sums are capped at the footprint area: whole 0.08 ha pixels
  -- over-shoot on small plots.
  UPDATE plot p SET
    forest2020_ha      = round(least(agg.forest2020_ha, a.ha)::numeric, 3),
    loss_after_2020_ha = round(least(agg.loss_ha, a.ha)::numeric, 3),
    loss_on_forest_ha  = round(least(agg.loss_forest_ha, a.ha)::numeric, 3),
    loss_by_year = coalesce(yrs.by_year, '{}'::jsonb)
  FROM agg LEFT JOIN yrs USING (id),
       LATERAL (SELECT ST_Area(pp.footprint::geography) / 10000 AS ha FROM plot pp WHERE pp.id = agg.id) a
  WHERE p.id = agg.id;

  UPDATE plot p SET issues = p.issues || add_issue('no_forest_data', 'warning')
  WHERE p.batch_id = p_batch AND p.footprint IS NOT NULL AND p.loss_after_2020_ha IS NULL
    AND NOT p.issues @> '[{"code": "outside_country"}]';

  -- 10. Overall status per plot.
  --   high     >= 0.1 ha of 2021-2025 loss on land that was forest at the cut-off
  --   review   any smaller loss on 2020 forest, a quarter or more of the plot was
  --            forest in 2020 (coffee there now would mean clearing the map hasn't
  --            caught), or no forest data
  --   low      otherwise; tree loss on non-forest land (e.g. renewing old coffee
  --            or shade trees) is reported but doesn't raise the status
  UPDATE plot p SET risk = CASE
      WHEN p.issues @> '[{"severity": "error"}]' THEN 'fix_data'
      WHEN p.loss_on_forest_ha >= 0.1 THEN 'high'
      WHEN p.loss_on_forest_ha > 0
        OR p.loss_after_2020_ha IS NULL
        OR p.forest2020_ha >= 0.25 * ST_Area(p.footprint::geography) / 10000 THEN 'review'
      ELSE 'low' END
  WHERE p.batch_id = p_batch;

  UPDATE batch SET n_plots = (SELECT count(*) FROM plot WHERE batch_id = p_batch) WHERE id = p_batch;
END $$;
