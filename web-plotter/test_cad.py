from collections import Counter
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import ezdxf
from ezdxf import units

from cad import CadImportError, import_drawing, repair_closed_contours


class CadImportTests(unittest.TestCase):
    def import_document(self, document, fmt="asc"):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "drawing.dxf"
            document.saveas(file, fmt=fmt)
            return import_drawing(file.read_bytes(), "деталь.DXF")

    def test_inch_contour_and_visible_layers_in_binary_dxf(self):
        doc = ezdxf.new("R2013", units=units.IN)
        doc.layers.new("Крой")
        doc.layers.new("Эскиз").off()
        model = doc.modelspace()
        model.add_lwpolyline([(0, 0), (2, 0), (2, 1), (0, 1)], close=True, dxfattribs={"layer": "Крой"})
        model.add_line((0, 0), (10, 10), dxfattribs={"layer": "Эскиз"})

        drawing = self.import_document(doc, fmt="bin")

        self.assertEqual(drawing["filename"], "деталь.DXF")
        self.assertEqual(drawing["units"], "mm")
        self.assertEqual(drawing["paths"], [
            {"points": [[0, 0], [50.8, 0], [50.8, 25.4], [0, 25.4]], "closed": True, "layer": "Крой"},
        ])
        self.assertIn("скрытые объекты", drawing["warnings"][0])

    def test_curves_and_out_of_plane_geometry(self):
        doc = ezdxf.new(units=units.MM)
        model = doc.modelspace()
        model.add_circle((25, 25), 10)
        model.add_arc((0, 0), 10, 0, 90)
        model.add_lwpolyline([(0, 0, 1), (20, 0, 0)], format="xyb")
        model.add_ellipse((0, 0), major_axis=(10, 0), ratio=0.5)
        model.add_spline([(0, 0), (4, 10), (8, -5), (12, 0)])
        model.add_line((0, 0, 0), (10, 10, 3))

        drawing = self.import_document(doc)
        circle, arc, bulge, ellipse, spline = drawing["paths"]

        self.assertTrue(circle["closed"])
        self.assertTrue(ellipse["closed"])
        self.assertFalse(arc["closed"])
        self.assertEqual(arc["points"][0], [10, 0])
        self.assertEqual(arc["points"][-1], [0, 10])
        self.assertAlmostEqual(min(y for x, y in bulge["points"]), -10, delta=0.05)
        self.assertEqual(spline["points"][0], [0, 0])
        self.assertEqual(spline["points"][-1], [12, 0])
        self.assertIn("контуры вне плоскости XY", drawing["warnings"][0])

    def test_nested_block_array_transforms_and_layer_inheritance(self):
        doc = ezdxf.new(units=units.MM)
        doc.layers.new("Крой")
        inner = doc.blocks.new("INNER", base_point=(1, 0))
        inner.add_line((1, 0), (3, 0))
        outer = doc.blocks.new("OUTER")
        outer.add_blockref("INNER", (3, 0), dxfattribs={"xscale": 2})
        ref = doc.modelspace().add_blockref("OUTER", (10, 20), dxfattribs={"rotation": 90, "layer": "Крой"})
        ref.grid(size=(1, 2), spacing=(0, 8))

        drawing = self.import_document(doc)

        self.assertEqual(drawing["paths"], [
            {"points": [[10, 23], [10, 27]], "closed": False, "layer": "Крой"},
            {"points": [[10, 31], [10, 35]], "closed": False, "layer": "Крой"},
        ])

    def test_unitless_warning_and_invalid_input(self):
        doc = ezdxf.new(units=0)
        doc.modelspace().add_line((0, 0), (8, 8))
        self.assertIn("1 мм", self.import_document(doc)["warnings"][0])

        for data, name in [(b"not a drawing", "bad.dxf"), (b"not a drawing", "bad.dwg"), (b"", "empty.dxf")]:
            with self.subTest(name=name), self.assertRaises(CadImportError):
                import_drawing(data, name)

    def test_import_recovers_fragmented_outline_with_embedded_notch(self):
        doc = ezdxf.new(units=units.MM)
        model = doc.modelspace()
        model.add_line((0, 0), (20, 0))
        model.add_lwpolyline([(20, -5), (20, 0), (20, 10), (0, 10), (0, 0)])

        drawing = self.import_document(doc)

        self.assertEqual(drawing["closedCount"], 1)
        self.assertEqual(drawing["paths"], [
            {"points": [[20, -5], [20, 0]], "closed": False, "layer": "0"},
            {"points": [[0, 0], [20, 0], [20, 10], [0, 10]], "closed": True, "layer": "0"},
        ])

    def test_saved_outline_repair_preserves_geometry_metadata_and_exclusions(self):
        paths = [
            {"points": [[0, 0], [20, 0]], "closed": False, "layer": "0", "partNumber": 8},
            {"points": [[40, 0], [40, 5]], "closed": False, "layer": "0", "partNumber": 9},
            {"points": [[20, -5], [20, 0], [20, 10], [0, 10], [0, 0]],
             "closed": False, "layer": "0", "partNumber": 8, "partRotation": "none"},
            {"points": [[50, 0], [51, 5]], "closed": False, "layer": "0", "partNumber": 9},
        ]

        repaired = repair_closed_contours(paths, [3, 1])

        def segments(paths):
            result = Counter()
            for path in paths:
                points = list(map(tuple, path["points"]))
                if path["closed"]:
                    points.append(points[0])
                result.update(tuple(sorted(edge)) for edge in pairwise(points))
            return result

        self.assertEqual(repaired.closed_count, 1)
        self.assertEqual(repaired.sources, [[1], [2], [0, 2], [3]])
        self.assertEqual(repaired.excluded, [0, 3])
        self.assertEqual(repaired.paths[2]["partNumber"], 8)
        self.assertEqual(repaired.paths[2]["partRotation"], "none")
        self.assertEqual(segments(repaired.paths), segments(paths))
        self.assertEqual(repair_closed_contours(repaired.paths).closed_count, 0)

    def test_repair_avoids_duplicate_edges_and_cross_part_loops(self):
        def open_path(points, number):
            return {"points": points, "closed": False, "layer": "0", "partNumber": number}

        base = open_path([[0, 0], [20, 0]], 1)
        sides = open_path([[20, 0], [20, 10], [0, 10], [0, 0]], 1)
        separate = {**sides, "partNumber": 2}
        gap = open_path([[20, 0], [20, 10], [0, 10], [0, 0.01]], 1)
        for paths in ([base, sides, base], [base, separate], [base, gap]):
            with self.subTest(paths=paths):
                self.assertEqual(repair_closed_contours(paths).paths, paths)

    def test_small_single_path_roundoff_keeps_original_endpoint(self):
        points = [[0, 0], [20, 0], [20, 10], [0, 10], [0, 0.0005]]
        outline = {"points": points, "closed": False, "layer": "0"}
        self.assertEqual(repair_closed_contours([outline]).paths, [{**outline, "closed": True}])

    def test_manual_gap_repair_skips_crossing_edges_larger_gaps_and_exclusions(self):
        shapes = [
            [[0, 0], [20, 0], [20, 20], [-10, -9.991], [-10, 20], [0, 0.012]],
            [[0, 0], [20, 0], [20, 20], [0, 20], [0, 0.021]],
            [[0, 0], [20, 0], [20, 20], [0, 20], [0, 0.012]],
        ]
        paths = [{"points": points, "closed": False, "layer": "Крой", "partNumber": index + 1}
                 for index, points in enumerate(shapes)]
        result = repair_closed_contours(paths, [2], bridge_small_gaps=True)
        self.assertEqual(result.closed_count, 0)
        self.assertEqual(result.paths, paths)
        self.assertEqual(result.excluded, [2])

    def test_repair_recovers_perimeter_when_slit_endpoint_is_inside_its_first_edge(self):
        points = [[20, -125], [20, 10], [0, 10], [0, 0], [20, 0]]
        outline = {"points": points, "closed": False, "layer": "0"}
        repaired = repair_closed_contours([outline])

        self.assertEqual(repaired.paths, [
            {"points": [[20, -125], [20, 0]], "closed": False, "layer": "0"},
            {"points": [[20, 0], [20, 10], [0, 10], [0, 0]], "closed": True, "layer": "0"},
        ])
        self.assertEqual(repaired.sources, [[0], [0]])
        self.assertEqual(repair_closed_contours(repaired.paths).closed_count, 0)
