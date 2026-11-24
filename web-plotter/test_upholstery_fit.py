import unittest
from copy import deepcopy

import numpy as np

from upholstery import UpholsteryError, calculate
from upholstery_fit import _FoamLaw, _SelfContact, _Shell, _panels, _stitches, simulate
from test_upholstery import grid


def fixture():
    return {"nodes": [{"id": "foam", "name": "ППУ", "vertices": [
        [0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0]],
        "triangles": [[0, 1, 2], [0, 2, 3]], "faces": ["face", "face"]}]}


def markup(**fit):
    return {"panels": [{"id": "cover", "nodeId": "foam", "faces": []}],
            "fit": {"foamNodeIds": ["foam"], **fit}}


class UpholsteryFitTests(unittest.TestCase):
    def test_horizontal_sheet_compresses_foam_by_weight(self):
        # 1 kg/m2 exerts 9.80665 Pa. 2 kPa modulus over 50 mm gives 0.245166 mm.
        state = markup(massGsm=1000, foamModulusKPa=2, foamThicknessMm=50)
        result = simulate(fixture(), state)
        self.assertTrue(result["converged"], result["metrics"])
        self.assertAlmostEqual(result["metrics"]["maxCompressionMm"], .24516625, places=3)
        self.assertAlmostEqual(result["foam"][0]["vertices"][0][2], -.24516625, places=3)
        self.assertLess(result["panels"][0]["maxTensionNPerMm"], .0001)
        self.assertEqual(result, simulate(fixture(), state))

    def test_fixed_coupon_matches_orthotropic_tension_and_grain(self):
        state = markup(massGsm=0, warpReductionPct=10, warpNPerMm=18,
                       fixedEdges=[{"panelId": "cover", "index": i} for i in range(4)])
        pattern = simulate(fixture(), state)["panels"][0]
        self.assertAlmostEqual(pattern["maxWarpPct"], 100 / .9 - 100)
        self.assertAlmostEqual(pattern["maxWeftPct"], 0.)
        self.assertAlmostEqual(pattern["maxTensionNPerMm"], 2.)
        self.assertTrue(all(abs(v[2]) < 1e-9 for v in pattern["vertices"]))
        # Rotate grain on an otherwise identical rest panel with a prescribed x stretch.
        calc = calculate(fixture(), markup())
        p = calc["panels"][0]
        p["sourceVertices"] = [[1.05 * u, v, 0] for u, v in p["vertices"]]
        state["fit"]["warpReductionPct"] = 0
        p["grain"] = [[0, 0], [0, 1]]
        result = simulate(fixture(), state, calculated=calc)["panels"][0]
        self.assertAlmostEqual(result["maxWarpPct"], 0.)
        self.assertAlmostEqual(result["maxWeftPct"], 5.)

    def test_energy_gradient_in_bent_sheared_panel(self):
        state = markup(massGsm=0)
        shell = _Shell(_panels(calculate(fixture(), state), state["fit"]))
        x = shell.initial.copy()
        x[2] += [4, -3, 7]
        direction = np.array([[.2, -.5, .4], [.4, .3, -.2], [-.3, .7, .5], [1., .2, -.2]])
        energy, grad = shell.energy(x)
        difference = (shell.energy(x + direction * 1e-5)[0] - shell.energy(x - direction * 1e-5)[0]) / 2e-5
        self.assertGreater(energy, 0.)
        self.assertAlmostEqual(difference, np.sum(grad * direction), places=6)

    def test_partial_stitch_and_reversed_bank_interpolate_both_meshes(self):
        state = markup()
        calc = calculate(fixture(), state)
        a = calc["panels"][0]
        b = deepcopy(a)
        b["id"] = "right"
        panels = _panels({"panels": [a, b]}, {})
        stitch = [{"id": "seam", "a": {"panelId": "cover", "vertices": [0, 1], "fractions": [0, 1],
                                         "range": [.25, .75]},
                   "b": {"panelId": "right", "vertices": [0, 1, 2], "fractions": [0, .5, 1],
                         "range": [1., 0.]}}]
        m = _stitches(panels, stitch, 8)
        self.assertEqual(m.shape, (3, 8))
        np.testing.assert_allclose(m @ np.array([0., 100., 0., 0., 75., 50., 25., 0.]), 0.)

    def test_foam_curve_and_zone_override(self):
        curve = [{"compressionPct": 0, "pressureKPa": 0}, {"compressionPct": 10, "pressureKPa": .1},
                 {"compressionPct": 50, "pressureKPa": 25}]
        law = _FoamLaw({"foamCurve": curve, "foamThicknessMm": 50})
        energy, pressure = law.response(np.array([5.0, 25.]))
        np.testing.assert_allclose(pressure, [.0001, .025])
        self.assertGreater(energy[1], energy[0])
        state = markup(foamZones={"foam": {"foamThicknessMm": 50, "foamCurve": curve}}, massGsm=1000)
        result = simulate(fixture(), state)
        self.assertAlmostEqual(result["metrics"]["maxCompressionMm"], .4903325, places=3)
        with self.assertRaises(UpholsteryError):
            _FoamLaw({"foamCurve": curve[::-1]})
        with self.assertRaises(UpholsteryError):
            simulate(fixture(), markup(foamNodeIds=["absent"]))

    def test_stable_fixed_edge_survives_index_change(self):
        state = markup(massGsm=0)
        calc = calculate(fixture(), state)
        edge = calc["panels"][0]["edges"][0]
        state["fit"]["fixedEdges"] = [{"panelId": "cover", "loop": 99, "index": 99, "edgeId": edge["id"]}]
        self.assertEqual(simulate(fixture(), state, calculated=calc)["metrics"]["fixedVertices"], 2)
        state["fit"]["fixedEdges"][0]["edgeId"] = "gone"
        with self.assertRaisesRegex(UpholsteryError, "кромка"):
            simulate(fixture(), state, calculated=calc)

    def test_declared_sew_lines_close_and_tensions_relax_when_anchor_released(self):
        model = fixture()
        for pid, lo, hi in (("left", 0, 45), ("right", 55, 100)):
            model["nodes"].append({**model["nodes"][0], "id": pid,
                                   "vertices": [[lo, 0, 0], [hi, 0, 0], [hi, 100, 0], [lo, 100, 0]]})
        state = {"panels": [{"id": p, "nodeId": p, "faces": []} for p in ("left", "right")],
                 "seams": [{"id": "s1", "nodeId": "left", "kind": "seam", "anchors": [{"vertex": 1}, {"vertex": 2}]},
                           {"id": "s2", "nodeId": "right", "kind": "seam", "anchors": [{"vertex": 0}, {"vertex": 3}]}],
                 "pairs": [{"id": "sewn", "a": {"panelId": "left", "seamId": "s1"},
                            "b": {"panelId": "right", "seamId": "s2"}}],
                 "fit": {"foamNodeIds": ["foam"], "massGsm": 0, "friction": 0}}
        calc = calculate(model, state)
        pinned = []
        for panel, location in zip(calc["panels"], [0, 100]):
            edge = next(edge for edge in panel["edges"] if all(point[0] == location for point in edge["sourcePoints"]))
            pinned.append({"panelId": panel["id"], "edgeId": edge["id"]})
        state["fit"]["fixedEdges"] = pinned
        fit = simulate(model, state, calculated=calc)
        self.assertEqual(fit["metrics"]["stitchSamples"], 2)
        self.assertTrue(fit["converged"], fit["metrics"])
        self.assertLess(fit["metrics"]["seamGapMm"], .5)
        self.assertGreater(fit["panels"][0]["maxTensionNPerMm"], .1)
        state["fit"]["fixedEdges"] = pinned[:1]
        loose = simulate(model, state, calculated=calc)
        self.assertLess(loose["panels"][0]["maxTensionNPerMm"], .01)

    def test_nearby_free_panel_generates_opposing_contact_forces(self):
        calc = calculate(fixture(), markup())
        lower = calc["panels"][0]
        top = deepcopy(lower)
        top["id"] = "top"
        top["sourceVertices"] = [[x * .5 + 15, y * .5 + 10, z + .3] for x, y, z in top["sourceVertices"]]
        calc["panels"].append(top)
        panels = _panels(calc, {})
        shell = _Shell(panels)
        stitches = _stitches(panels, [], len(shell.initial))
        contact = _SelfContact(shell.initial, shell, stitches, 1.)
        energy, grad, penetration = contact.energy(shell.initial)
        self.assertGreater(energy, 0.)
        self.assertAlmostEqual(penetration, .7)
        np.testing.assert_allclose(grad.sum(axis=0), 0., atol=1e-10)
        self.assertLess(grad[panels["top"].offset:, 2].sum(), 0.)

    def test_infeasible_fixed_stitch_never_reports_convergence(self):
        calc = calculate(fixture(), markup())
        a = calc["panels"][0]
        b = deepcopy(a)
        b["id"] = "other"
        b["sourceVertices"] = [[x, y, z + 5.] for x, y, z in b["sourceVertices"]]
        calc["panels"].append(b)
        calc["fitStitches"] = [{"a": {"panelId": "cover", "vertices": [0, 1], "fractions": [0, 1]},
                                "b": {"panelId": "other", "vertices": [0, 1], "fractions": [0, 1]}}]
        pins = [{"panelId": pid, "index": i} for pid in ("cover", "other") for i in range(4)]
        result = simulate(fixture(), markup(fixedEdges=pins), calculated=calc)
        self.assertFalse(result["converged"])
        self.assertAlmostEqual(result["metrics"]["seamGapMm"], 5.)

    def test_calibration_requires_all_supplied_measurements(self):
        fabric = dict(massGsm=500, warpNPerMm=12, weftNPerMm=8, shearNPerMm=1.5, bendingNmm=.05)
        state = markup(calibrated=True, **fabric)
        self.assertFalse(simulate(fixture(), state)["calibrated"])
        state["fit"].update(foamThicknessMm=60, foamModulusKPa=20, friction=.2)
        self.assertFalse(simulate(fixture(), state)["calibrated"])
        state["fit"]["foamCurve"] = [{"compressionPct": 0, "pressureKPa": 0}, {"compressionPct": 20, "pressureKPa": 4}]
        self.assertTrue(simulate(fixture(), state)["calibrated"])

    def test_curved_fastened_cover_compresses_soft_foam_more(self):
        source = grid(9, lambda i, j: [i * 15., j * 15., 24. - .9 * ((i - 4)**2 + (j - 4)**2)])
        state = markup(foamNodeIds=["surface"], foamThicknessMm=70, foamModulusKPa=20,
                       warpReductionPct=2, weftReductionPct=2)
        state["panels"][0]["nodeId"] = "surface"
        calc = calculate(source, state)
        state["fit"]["fixedEdges"] = [{"panelId": "cover", "edgeId": e["id"]} for e in calc["panels"][0]["edges"]]
        first = simulate(source, state, calculated=calc)
        state["fit"]["foamModulusKPa"] = 80
        second = simulate(source, state, calculated=calc)
        self.assertTrue(first["converged"], first["metrics"])
        self.assertTrue(second["converged"], second["metrics"])
        self.assertGreater(first["metrics"]["maxCompressionMm"], second["metrics"]["maxCompressionMm"])
        self.assertGreater(first["panels"][0]["maxTensionNPerMm"], 0.1)


if __name__ == "__main__":
    unittest.main()
