from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from urllib.request import Request
from zipfile import ZipFile

from app_update import AppUpdates, GitHubRedirect, METADATA_URL, REQUIRED, Release, UpdateError, download_release, extract_update, fetch_latest, latest_update


class AppUpdateTests(unittest.TestCase):
    def test_public_requests_use_https_without_a_token_and_limit_download_size(self):
        opener = Mock()
        opener.open.return_value = BytesIO(b'{"tag_name":"v1.6.0"}')
        self.assertEqual(fetch_latest(opener)["tag_name"], "v1.6.0")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, METADATA_URL)
        self.assertIsNone(request.get_header("Authorization"))
        release = Release("1.6.0", "plotter-companion-v1.6.0-windows-x64.zip", 4, "0" * 64)
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "update.zip"
            opener.open.return_value = BytesIO(b"abcd")
            download_release(opener, release, destination)
            self.assertEqual(destination.read_bytes(), b"abcd")
            self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))
            for data in (b"abc", b"abcde"):
                opener.open.return_value = BytesIO(data)
                with self.assertRaises(UpdateError):
                    download_release(opener, release, Path(directory) / f"size-{len(data)}.zip")

    def test_download_redirects_only_to_expected_https_hosts(self):
        redirect = GitHubRedirect()
        source = Request("https://github.com/a/b/releases/download/v1.6.0/test.zip")
        for url in ("https://unexpected.invalid/test", "http://release-assets.githubusercontent.com/test",
                    "https://github.com@unexpected.invalid/test"):
            with self.assertRaises(UpdateError):
                redirect.redirect_request(source, None, 302, "", {}, url)
        target = "https://release-assets.githubusercontent.com/test"
        self.assertEqual(redirect.redirect_request(source, None, 302, "", {}, target).full_url, target)

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
