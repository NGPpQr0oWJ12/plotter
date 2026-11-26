from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from workspace import WorkspaceConflict, WorkspaceStore


def draft():
    return {
        "source": {"filename": "Крой.DXF", "warnings": ["Не заданы единицы."]},
        "paths": [
            {"points": [[-50, 10], [2810.5, 10], [2500, 450]], "closed": True, "layer": "Крой"},
            {"points": [[26, 18.5], [14, 12]], "closed": False, "layer": "Внутренний"},
        ],
        "excluded": [1, 0],
        "view": {"zoom": 1.2, "focusX": -750, "focusY": 2100, "grid": False},
        "optimization": {"route": True, "join": False},
    }


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "workspace.json"
        self.store = WorkspaceStore(self.path)

    def test_restart_restores_edited_order_and_all_excluded_draft(self):
        self.assertEqual(self.store.read(), {"revision": 0, "workspace": None, "recovered": False})
        layout = draft()
        layout["controlToken"] = "transient"
        saved = self.store.write(0, layout)
        self.assertEqual(WorkspaceStore(self.path).read(), saved)
        self.assertEqual(saved["workspace"], draft())
        self.assertNotIn("transient", self.path.read_text(encoding="utf-8"))

    def test_nap_direction_round_trips_and_rejects_non_boolean_value(self):
        layout = draft()
        layout["optimization"]["napReversed"] = True
        saved = self.store.write(0, layout)
        self.assertEqual(WorkspaceStore(self.path).read(), saved)
        self.assertEqual(saved["workspace"], layout)

        invalid = deepcopy(layout)
        invalid["optimization"]["napReversed"] = 1
        with self.assertRaisesRegex(ValueError, "направление ворса"):
            self.store.write(saved["revision"], invalid)
        self.assertEqual(self.store.read(), saved)

        layout["optimization"]["napReversed"] = False
        reset = self.store.write(saved["revision"], layout)
        self.assertEqual(reset["workspace"], layout)

    def test_corrupt_primary_recovers_previous_and_preserves_backup_on_next_save(self):
        layout = draft()
        first = self.store.write(0, layout)
        replacement = deepcopy(layout)
        replacement["source"]["filename"] = "Другой.dwg"
        self.store.write(first["revision"], replacement)
        self.path.write_text("{", encoding="utf-8")

        store = WorkspaceStore(self.path)
        self.assertEqual(store.read(), {**first, "recovered": True})
        store.write(first["revision"], replacement)
        self.path.unlink()
        self.assertEqual(store.read(), {**first, "recovered": True})

    def test_camera_mode_and_section_survive_restart(self):
        layout = draft()
        layout["view"].update(cameraMode="work", section=3)
        saved = self.store.write(0, layout)
        self.assertEqual(WorkspaceStore(self.path).read()["workspace"]["view"], layout["view"])
        layout["view"]["section"] = True
        with self.assertRaises(ValueError):
            self.store.write(saved["revision"], layout)

    def test_stale_writer_and_invalid_data_cannot_overwrite_work(self):
        current = self.store.write(0, draft())
        with self.assertRaises(WorkspaceConflict):
            self.store.write(0, None)
        invalid = draft()
        invalid["paths"][0]["points"][0][0] = float("nan")
        with self.assertRaises(ValueError):
            self.store.write(current["revision"], invalid)
        self.assertEqual(self.store.read(), current)

    def test_nesting_settings_are_optional_and_restored(self):
        layout = draft()
        first = self.store.write(0, layout)
        settings = {"widthMm": 1400, "gapMm": 7, "marginMm": 9, "rotation": "half"}
        layout["optimization"]["nesting"] = settings
        saved = self.store.write(first["revision"], layout)
        self.assertEqual(WorkspaceStore(self.path).read(), saved)
        self.assertEqual(saved["workspace"]["optimization"]["nesting"], settings)
        layout["optimization"]["nesting"]["rotation"] = "any"
        saved = self.store.write(saved["revision"], layout)
        self.assertEqual(self.store.read()["workspace"]["optimization"]["nesting"]["rotation"], "any")
        layout["optimization"]["nesting"]["rotation"] = "invalid"
        with self.assertRaises(ValueError):
            self.store.write(saved["revision"], layout)

    def test_explicit_width_marker_round_trips(self):
        layout = draft()
        settings = {"widthMm": 1650, "gapMm": 10, "marginMm": 10, "rotation": "none", "widthExplicit": True}
        layout["optimization"]["nesting"] = settings
        saved = self.store.write(0, layout)
        self.assertEqual(self.store.read()["workspace"]["optimization"]["nesting"], settings)
        settings["widthExplicit"] = 1
        with self.assertRaises(ValueError):
            self.store.write(saved["revision"], layout)

    def test_saved_fabric_length_accepts_omitted_numeric_and_null(self):
        layout = draft()
        settings = {"widthMm": 1400, "gapMm": 7, "marginMm": 9, "rotation": "half"}
        layout["optimization"]["nesting"] = settings
        saved = self.store.write(0, layout)
        self.assertNotIn("lengthMm", WorkspaceStore(self.path).read()["workspace"]["optimization"]["nesting"])

        for length in (3600.5, None):
            with self.subTest(length=length):
                settings["lengthMm"] = length
                saved = self.store.write(saved["revision"], layout)
                reopened = WorkspaceStore(self.path).read()
                self.assertEqual(reopened["workspace"]["optimization"]["nesting"], settings)

        for invalid in (True, 0, -1, "3600", float("inf")):
            with self.subTest(length=invalid):
                settings["lengthMm"] = invalid
                with self.assertRaises(ValueError):
                    self.store.write(saved["revision"], layout)
                self.assertEqual(WorkspaceStore(self.path).read(), saved)

    def test_part_numbers_and_rotation_survive_reordering_and_restart(self):
        layout = draft()
        contour, mark = layout["paths"]
        contour.update(partNumber=12, partDisplayNumber="13/1", partRotation="none", partAngle=275.25,
                       partName="Царга", productName="Диван")
        mark.update(partNumber=12, partDisplayNumber="13/1", partAngle=275.25, partName="Царга", productName="Диван")
        layout["paths"].reverse()
        saved = self.store.write(0, layout)
        self.assertEqual(WorkspaceStore(self.path).read(), saved)
        self.assertEqual(saved["workspace"]["paths"], [mark, contour])

    def test_invalid_part_metadata_cannot_replace_saved_layout(self):
        current = self.store.write(0, draft())
        for updates in (
            ({"partNumber": True}, {}),
            ({"partName": "Царга"}, {}),
            ({"partNumber": 1, "productName": "\nДиван"}, {}),
            ({"partDisplayNumber": "2/1"}, {}),
            ({"partNumber": 1, "partDisplayNumber": "2/0"}, {}),
            ({"partNumber": 1, "partDisplayNumber": "2/1"}, {"partNumber": 1, "partDisplayNumber": "2/2"}),
            ({"partNumber": 1, "partName": "Царга"}, {"partNumber": 1, "partName": "Сиденье"}),
            ({"partNumber": 0}, {}),
            ({"partNumber": None}, {}),
            ({"partNumber": 1 << 53}, {}),
            ({"partAngle": True}, {}),
            ({"partAngle": None}, {}),
            ({"partAngle": "90"}, {}),
            ({"partAngle": -0.1}, {}),
            ({"partAngle": 360}, {}),
            ({"partAngle": float("inf")}, {}),
            ({"partAngle": float("nan")}, {}),
            ({"partRotation": "quarter"}, {}),
            ({"partNumber": 1, "partRotation": "half"}, {}),
            ({"partNumber": 1, "partRotation": "none"}, {"partNumber": 1, "partRotation": "quarter"}),
        ):
            with self.subTest(updates=updates):
                layout = draft()
                for path, metadata in zip(layout["paths"], updates):
                    path.update(metadata)
                with self.assertRaises(ValueError):
                    self.store.write(current["revision"], layout)
                self.assertEqual(self.store.read(), current)

    def test_failed_replace_keeps_existing_workspace_and_removes_temp_file(self):
        current = self.store.write(0, draft())
        with patch("workspace.os.replace", side_effect=PermissionError("запись запрещена")):
            with self.assertRaises(PermissionError):
                self.store.write(current["revision"], None)
        self.assertEqual(self.store.read(), current)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
