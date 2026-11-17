from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request
from zipfile import ZipFile

from app_update import (
    AppUpdates, GitHubRedirect, RELEASES_URL, REQUIRED, Release, UpdateError,
    download_release, extract_update, fetch_central_update, fetch_release_page, find_companion_update,
    latest_update, read_companion_revision, read_release_notes,
)


class AppUpdateTests(unittest.TestCase):
    def test_public_requests_use_https_without_a_token_and_limit_download_size(self):
        opener = Mock()
        opener.open.return_value = BytesIO(b'[{"tag_name":"v1.6.0"}]')
        self.assertEqual(fetch_release_page(opener, 1)[0]["tag_name"], "v1.6.0")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f"{RELEASES_URL}?per_page=30&page=1")
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

    def test_server_only_releases_do_not_hide_the_companion_update(self):
        asset = {"name": "plotter-companion-v1.5.3-windows-x64.zip", "digest": "sha256:" + "a" * 64,
                 "state": "uploaded", "size": 512}
        server_only = {"tag_name": "v1.5.4", "draft": False, "prerelease": False, "assets": []}
        releases = [server_only, {**server_only, "tag_name": "v1.5.3", "assets": [asset]}]
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: BytesIO(json.dumps(releases).encode())
        self.assertEqual(find_companion_update(opener, "1.5.2").version, "1.5.3")
        self.assertIsNone(find_companion_update(opener, "1.5.3"))

    def test_trusted_central_manifest_uses_fixed_paths_and_validates_metadata(self):
        opener = Mock()
        origin = "https://central.invalid"
        metadata = {"version": "2.5.0", "revision": 1, "size": 4, "digest": "a" * 64,
                    "url": "https://untrusted.invalid/packages/bad.zip"}
        opener.open.return_value = BytesIO(json.dumps(metadata).encode())
        release = fetch_central_update(opener, origin)
        self.assertEqual((release.version, release.revision, release.central), ("2.5.0", 1, True))
        self.assertEqual(opener.open.call_args.args[0].full_url, origin + "/api/companion-update")
        with TemporaryDirectory() as directory:
            opener.open.return_value = BytesIO(b"test")
            destination = Path(directory) / "update.zip"
            download_release(opener, release, destination, central_origin=origin)
            self.assertEqual(opener.open.call_args.args[0].full_url, origin + "/api/companion-update.zip")
            self.assertEqual(destination.read_bytes(), b"test")
        for fields in ({"revision": True}, {"revision": -1}, {"size": False}, {"digest": "A" * 64},
                       {"version": "v2.5.0"}):
            with self.subTest(fields=fields), self.assertRaises(UpdateError):
                opener.open.return_value = BytesIO(json.dumps({**metadata, **fields}).encode())
                fetch_central_update(opener, origin)

    def test_same_version_newer_central_build_discovery_and_missing_manifest_fallback(self):
        origin = "https://central.invalid"
        opener = Mock()
        with TemporaryDirectory() as directory, patch("app_update.find_companion_update") as github:
            root = Path(directory)
            (root / "apply-companion-update.ps1").touch()
            self.assertEqual(read_companion_revision(root), 0)
            (root / "COMPANION_REVISION").write_bytes(b"1\r\n")
            manager = AppUpdates(root, "2.5.0", root, (opener, origin))
            metadata = {"version": "2.5.0", "revision": 2, "size": 4, "digest": "a" * 64}
            opener.open.return_value = BytesIO(json.dumps(metadata).encode())
            manager._refresh()
            status = manager.status()
            self.assertEqual((status["revision"], status["availableVersion"], status["availableRevision"]),
                             (1, "2.5.0", 2))
            opener.open.return_value = BytesIO(json.dumps({**metadata, "revision": 1}).encode())
            manager._refresh()
            self.assertIsNone(manager.status()["availableVersion"])
            github.assert_not_called()

            opener.open.side_effect = HTTPError(origin, 404, "not present", {}, BytesIO())
            github.return_value = Release("2.6.0", "legacy.zip", 4, "a" * 64)
            manager._refresh()
            status = manager.status()
            self.assertEqual((status["availableVersion"], status["availableRevision"]), ("2.6.0", 0))
            github.assert_called_once()

    def test_start_same_version_requires_current_revision_when_supplied(self):
        with TemporaryDirectory() as directory, patch("app_update.Thread") as thread:
            root = Path(directory)
            (root / "apply-companion-update.ps1").touch()
            manager = AppUpdates(root, "2.5.0", root)
            release = Release("2.5.0", "test.zip", 4, "a" * 64, revision=1, central=True)
            manager._available = release
            verify_idle = Mock()
            for mismatched in (0, 2, True, "1"):
                with self.subTest(revision=mismatched), self.assertRaises(UpdateError):
                    manager.start("2.5.0", verify_idle, revision=mismatched)
            thread.assert_not_called()
            manager.start("2.5.0", verify_idle, revision=1)
            thread.assert_called_once_with(target=manager._prepare, args=(release, verify_idle), daemon=True)
            thread.return_value.start.assert_called_once()

    def test_installed_release_reads_only_matching_changelog_section(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CHANGELOG.md").write_text(
                "# Notes\n\n## 1.5.3\n\n- Later\n\n## 1.5.2\n\n- Current\n- Other\n\n## 1.5.1\n- Earlier\n",
                encoding="utf-8-sig",
            )
            self.assertEqual(read_release_notes(root, "1.5.2"), ["Current", "Other"])
            self.assertEqual(read_release_notes(root, "1.5.4"), [])

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
            self.assertEqual(extract_update(path, root / "valid", release), 0)
            self.assertEqual((root / "valid/VERSION").read_text(), "1.6.0")

            invalid = Release(release.version, release.name, release.size, "0" * 64)
            with self.assertRaises(UpdateError):
                extract_update(path, root / "wrong-digest", invalid)
            for payload in (
                {**files, "VERSION": "1.5.0"},
                {**files, "COMPANION_REVISION": "2147483648"},
                {**files, "../outside.txt": ""},
                {**files, "static/AUX.txt": ""},
                {**files, "static/App.js": ""},
            ):
                release = build(payload)
                with self.assertRaises(UpdateError):
                    extract_update(path, root / "invalid", release)
                self.assertFalse((root / "invalid").exists())
            release = build({**files, "COMPANION_REVISION": "4\n"})
            self.assertEqual(release.revision, 0)
            self.assertEqual(extract_update(path, root / "valid-marked-github", release), 4)

    def test_staged_central_archive_requires_exact_revision_and_passes_it_to_windows(self):
        origin = "https://central.invalid"
        opener = Mock()
        with TemporaryDirectory() as directory, patch("app_update.subprocess.run") as launch, \
                patch("app_update.subprocess.CREATE_NO_WINDOW", 0, create=True), \
                patch.dict("app_update.os.environ", {"SystemRoot": "Windows"}):
            root = Path(directory)
            (root / "apply-companion-update.ps1").touch()
            manager = AppUpdates(root, "2.5.0", root, (opener, origin))
            manager._checking = True
            files = {name: "" for name in REQUIRED} | {"VERSION": "2.5.0"}
            verify_idle = Mock()
            for marker in (None, "2", "+1", "\ufeff1", "1\n"):
                with self.subTest(marker=marker):
                    memory = BytesIO()
                    with ZipFile(memory, "w") as archive:
                        for name, content in files.items():
                            archive.writestr(name, content)
                        if marker is not None:
                            archive.writestr("COMPANION_REVISION", marker)
                    contents = memory.getvalue()
                    release = Release("2.5.0", "test.zip", len(contents), sha256(contents).hexdigest(),
                                      revision=1, central=True)
                    opener.open.return_value = BytesIO(contents)
                    manager._prepare(release, verify_idle)
                    if marker != "1\n":
                        self.assertEqual(manager.status()["state"], "failed")
                        launch.assert_not_called()
                        verify_idle.assert_not_called()
                    else:
                        self.assertEqual(manager.status()["state"], "restarting")
                        verify_idle.assert_called_once()
                        args = launch.call_args.args[0]
                        self.assertEqual(args[args.index("-ExpectedRevision") + 1], "1")
            opener.open.return_value = BytesIO(contents)
            launch.side_effect = CalledProcessError(5, ["PowerShell"], stderr=b"irrelevant path")
            manager._prepare(release, verify_idle)
            self.assertEqual(manager.status()["message"], "Windows не запустила задачу обновления помощника (код 5).")

    def test_update_result_compares_the_full_build_and_ignores_stale_failures(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apply-companion-update.ps1").touch()
            (root / "COMPANION_REVISION").write_bytes(b"1")
            manager = AppUpdates(root, "2.5.0", root)
            manager._checking = True
            result = root / "update-result.json"
            for revision, state, expected in ((0, "failed", "idle"), (0, "done", "idle"),
                                               (1, "done", "done"), (2, "failed", "failed")):
                with self.subTest(revision=revision, state=state):
                    result.write_text(json.dumps({"version": "2.5.0", "revision": revision, "state": state}), encoding="ascii")
                    self.assertEqual(manager.status()["state"], expected)

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
