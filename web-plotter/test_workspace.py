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

    def test_part_numbers_and_rotation_survive_reordering_and_restart(self):
        layout = draft()
        contour, mark = layout["paths"]
        contour.update(partNumber=12, partRotation="none")
        mark["partNumber"] = 12
        layout["paths"].reverse()
        saved = self.store.write(0, layout)
        self.assertEqual(WorkspaceStore(self.path).read(), saved)
        self.assertEqual(saved["workspace"]["paths"], [mark, contour])

    def test_invalid_part_metadata_cannot_replace_saved_layout(self):
        current = self.store.write(0, draft())
        for updates in (
            ({"partNumber": True}, {}),
            ({"partNumber": 0}, {}),
            ({"partNumber": None}, {}),
            ({"partNumber": 1 << 53}, {}),
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
