from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from app_update import AppUpdates, REQUIRED, Release, UpdateError, extract_update, latest_update


class AppUpdateTests(unittest.TestCase):
    def test_only_new_stable_releases_with_verified_companion_asset_are_offered(self):
        release = {"tag_name": "v1.10.0", "draft": False, "prerelease": False, "assets": [
            {"name": "plotter-companion-v1.10.0-windows-x64.zip", "digest": "sha256:" + "a" * 64,
             "state": "uploaded", "size": 512},
        ]}
        self.assertEqual(latest_update(release, "1.9.9").version, "1.10.0")
        self.assertIsNone(latest_update(release, "1.10.0"))
        self.assertIsNone(latest_update({**release, "prerelease": True}, "1.9.9"))
        self.assertIsNone(latest_update({**release, "assets": []}, "1.9.9"))
        del release["assets"][0]["digest"]
        self.assertIsNone(latest_update(release, "1.9.9"))

    def test_extract_checks_digest_version_and_windows_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "test.zip"

            def build(entries):
                with ZipFile(path, "w") as archive:
                    for filename, content in entries.items():
                        archive.writestr(filename, content)
                return Release("1.6.0", "test.zip", path.stat().st_size, sha256(path.read_bytes()).hexdigest())

            files = {name: "" for name in REQUIRED} | {"VERSION": "1.6.0"}
            release = build(files)
            extract_update(path, root / "valid", release)
            self.assertEqual((root / "valid/VERSION").read_text(), "1.6.0")

            invalid = Release(release.version, release.name, release.size, "0" * 64)
            with self.assertRaises(UpdateError):
                extract_update(path, root / "wrong-digest", invalid)
            for payload in (
                {**files, "VERSION": "1.5.0"},
                {**files, "../outside.txt": ""},
                {**files, "static/AUX.txt": ""},
                {**files, "static/App.js": ""},
            ):
                release = build(payload)
                with self.assertRaises(UpdateError):
                    extract_update(path, root / "invalid", release)
                self.assertFalse((root / "invalid").exists())

    def test_restart_blocks_changes_but_allows_stop_and_reported_failure_reopens_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apply-companion-update.ps1").touch()
            manager = AppUpdates(root, "1.5.0", root)
            manager._checking = True
            manager._state = "restarting"
            with self.assertRaises(UpdateError), manager.protected_request():
                self.fail("Restart must block ordinary writes")
            with manager.protected_request(allow_restart=True):
                self.assertEqual(manager._requests, 1)
            (root / "update-result.json").write_text('{"state":"failed","message":"rolled back"}', encoding="ascii")
            self.assertEqual(manager.status()["state"], "failed")
            with manager.protected_request():
                self.assertEqual(manager._requests, 1)
