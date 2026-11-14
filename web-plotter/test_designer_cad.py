import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import ezdxf
from ezdxf import units

from designer_cad import CadDesignError, edit_design, import_design, prepare_design, validate_design


def line(identifier, start, end):
    return {"id": identifier, "type": "line", "layer": "0", "start": start, "end": end}


def square():
    return {"id": "outline", "type": "polyline", "layer": "Крой", "closed": True,
            "points": [[0, 0], [20, 0], [20, 20], [0, 20]]}


class DesignerCadTests(unittest.TestCase):
    def import_document(self, document, fmt="asc"):
        with TemporaryDirectory() as directory:
            drawing = Path(directory) / "чертеж.dxf"
            document.saveas(drawing, fmt=fmt)
            data = drawing.read_bytes()
        return import_design(data, drawing.name)

    def test_native_entities_and_stable_ids_in_binary_dxf(self):
        document = ezdxf.new(units=units.IN)
        document.layers.new("Hidden").off()
        model = document.modelspace()
        model.add_line((0, 0), (1, 0))
        model.add_circle((1, 1), 0.25)
        model.add_arc((0, 0), 1, 350, 260)
        model.add_ellipse((0, 0), major_axis=(3, 0), ratio=0.5)
        model.add_lwpolyline([(0, 0, 0), (1, 0, 0), (1, 1, -0.5)], format="xyb", close=True)
        model.add_polyline2d([(2, 0), (3, 1)])
        model.add_rational_spline([(1, 0), (1, 1), (0, 1)], [1, math.sqrt(0.5), 1], degree=2)
        model.add_line((0, 0, 0), (0, 1, 1))
        model.add_line((0, 0), (1, 0), dxfattribs={"layer": "Hidden"})

        drawing = self.import_document(document, "bin")
        entities = drawing["entities"]
        self.assertEqual(entities, self.import_document(document, "bin")["entities"])
        self.assertEqual([entity["type"] for entity in entities],
                         ["line", "circle", "arc", "ellipse", "polyline", "polyline", "spline"])
        self.assertEqual(drawing["units"], "mm")
        self.assertAlmostEqual(entities[0]["end"][0], 25.4)
        self.assertAlmostEqual(entities[1]["radius"], 6.35)
        self.assertEqual(entities[2]["startAngle"], 350)
        self.assertEqual(entities[2]["sweep"], 270)
        self.assertAlmostEqual(entities[3]["ratio"], 0.5)
        self.assertEqual(entities[4]["bulges"], [0, 0, -0.5])
        self.assertTrue(entities[4]["closed"])
        self.assertEqual(entities[6]["degree"], 2)
        self.assertEqual(entities[6]["weights"], [1, math.sqrt(0.5), 1])
        self.assertIn("скрытые объекты", drawing["warnings"][0])
        self.assertIn("вне плоскости XY", drawing["warnings"][0])
        paths = prepare_design(entities)["paths"]
        self.assertAlmostEqual(math.hypot(*paths[-1]["points"][len(paths[-1]["points"]) // 2]), 25.4, places=5)

    def test_headerless_dxf_defaults_to_millimetres(self):
        data = b"0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n10\n0\n20\n0\n11\n50\n21\n25\n0\nENDSEC\n0\nEOF\n"
        drawing = import_design(data, "detail.dxf")
        self.assertEqual(drawing["entities"][0]["end"], [50, 25])
        self.assertIn("1 мм", drawing["warnings"][0])

    def test_explicit_meter_units_scale_in_ascii_and_binary_dxf(self):
        document = ezdxf.new(units=units.M)
        document.modelspace().add_line((0, 0), (0.05, 0.025))
        for fmt in ("asc", "bin"):
            with self.subTest(fmt=fmt):
                drawing = self.import_document(document, fmt)
                self.assertEqual(drawing["entities"][0]["end"], [50, 25])
                self.assertEqual(drawing["warnings"], [])

    def test_block_reflection_and_nonuniform_arc_stay_editable(self):
        document = ezdxf.new(units=units.MM)
        document.layers.new("Крой")
        block = document.blocks.new("Detail")
        block.add_arc((0, 0), 10, 0, 270)
        block.add_lwpolyline([(0, 0, 1), (20, 0, 0)], format="xyb")
        model = document.modelspace()
        model.add_blockref("Detail", (10, 20), dxfattribs={"xscale": -1, "layer": "Крой"})
        model.add_blockref("Detail", (0, 0), dxfattribs={"xscale": 2, "yscale": 0.5})

        entities = self.import_document(document)["entities"]
        reflected, reflected_polyline, ellipse, bulged_ellipse = entities
        self.assertEqual(reflected["center"], [10, 20])
        self.assertEqual(reflected["sweep"], -270)
        self.assertEqual(reflected["layer"], "Крой")
        self.assertEqual(reflected_polyline["bulges"], [-1, 0])
        self.assertEqual(ellipse["type"], "ellipse")
        self.assertAlmostEqual(ellipse["ratio"], 0.25)
        self.assertEqual(bulged_ellipse["type"], "ellipse")
        paths = prepare_design(entities)["paths"]
        self.assertEqual(paths[2]["points"][0], [20, 0])
        self.assertEqual(paths[2]["points"][-1], [0, -5])
        self.assertAlmostEqual(min(y for _, y in paths[3]["points"]), -5, delta=0.05)

    def test_preparation_preserves_gap_and_excludes_reference_notch_family(self):
        reference = {**square(), "reference": True}
        notch = {**line("notch", [5, 0], [5, 1]), "notch": True, "parentId": reference["id"]}
        fragment = line("fragment", [30, 30], [35, 30])
        result = prepare_design([reference, notch, fragment])
        self.assertEqual(result["paths"], [{"points": [[30, 30], [35, 30]], "closed": False, "layer": "0"}])

    def test_preparation_handles_authored_closed_spline_and_signed_arc(self):
        spline = {"id": "s", "type": "spline", "closed": True,
                  "fitPoints": [[0, 0], [6, 0], [6, 6], [0, 6]]}
        arc = {"id": "a", "type": "arc", "center": [0, 0],
               "radius": 10, "startAngle": 0, "sweep": -270}
        spline_path, arc_path = prepare_design([spline, arc])["paths"]
        self.assertTrue(spline_path["closed"])
        self.assertIn([6, 6], spline_path["points"])
        self.assertFalse(arc_path["closed"])
        self.assertEqual(arc_path["points"][0], [10, 0])
        self.assertEqual(arc_path["points"][-1], [0, 10])
        self.assertLess(arc_path["points"][1][1], 0)

    def test_offset_retains_reference_and_moves_inward_notch(self):
        added = edit_design([square()], "notch", {"id": "outline", "point": [8, 1], "depth": 3})
        self.assertEqual(added["entities"][-1]["end"], [8, 3])
        offset = edit_design(added["entities"], "offset", {"id": "outline", "distance": 2})
        old, old_notch, new, new_notch = offset["entities"]
        self.assertTrue(old["reference"])
        self.assertTrue(old_notch["reference"])
        self.assertEqual(offset["selectedIds"], [new["id"]])
        self.assertEqual(new_notch["parentId"], new["id"])
        self.assertEqual(new_notch["start"], [8, -2])
        self.assertEqual(new_notch["end"], [8, 1])
        cut = prepare_design(offset["entities"])["paths"]
        self.assertEqual(len(cut), 2)
        self.assertEqual(min(x for x, _ in cut[0]["points"]), -2)
        self.assertTrue(cut[1]["notch"])

    def test_offset_preserves_analytic_circle_and_handles_collapse(self):
        circle = {"id": "circle", "type": "circle", "center": [2, 3], "radius": 5}
        options = {"id": "circle", "distance": -2, "keepOriginal": False}
        new, = edit_design([circle], "offset", options)["entities"]
        self.assertEqual((new["type"], new["center"], new["radius"]), ("circle", [2, 3], 3))
        with self.assertRaises(CadDesignError):
            edit_design([circle], "offset", {**options, "distance": -5})

    def test_circle_notch_starts_on_exact_boundary(self):
        circle = {"id": "circle", "type": "circle", "center": [2, 3], "radius": 5}
        result = edit_design([circle], "notch", {"id": "circle", "point": [2, 7], "depth": 1.5})
        notch = result["entities"][-1]
        self.assertAlmostEqual(math.dist(notch["start"], [2, 8]), 0)
        self.assertAlmostEqual(math.dist(notch["end"], [2, 6.5]), 0)

    def test_trim_click_and_join_preserve_intent(self):
        horizontal = line("target", [0, 0], [20, 0])
        left = line("left", [5, -5], [5, 5])
        right = line("right", [15, -5], [15, 5])
        trimmed = edit_design([horizontal, left, right], "trim", {
            "id": "target", "cutterIds": ["left", "right"], "point": [10, 0],
        })
        self.assertEqual([entity["end"] for entity in trimmed["entities"][2:]], [[5, 0], [20, 0]])
        self.assertEqual([entity["start"] for entity in trimmed["entities"][2:]], [[0, 0], [15, 0]])

        arc = {"id": "arc", "type": "arc", "center": [10, 5], "radius": 5,
               "startAngle": -90, "sweep": 180}
        segment = line("segment", [0, 0], [10, 0])
        joined = edit_design([arc, segment], "join", {"ids": ["segment", "arc"]})
        contour, = joined["entities"]
        self.assertEqual(contour["points"], [[0, 0], [10, 0], [10, 10]])
        self.assertAlmostEqual(contour["bulges"][1], 1)

    def test_validation_rejects_invalid_geometry_and_binding(self):
        with self.assertRaises(CadDesignError):
            validate_design([line("line", [0, 0], [float("nan"), 1])])
        with self.assertRaises(CadDesignError):
            validate_design([line("same", [0, 0], [1, 1]), line("same", [0, 0], [1, 1])])
        with self.assertRaises(CadDesignError):
            validate_design([{**line("new", [0, 0], [1, 1]), "notch": True, "parentId": "missing"}])


if __name__ == "__main__":
    unittest.main()
