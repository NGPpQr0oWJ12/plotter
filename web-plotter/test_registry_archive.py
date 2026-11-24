from contextlib import closing
from email.message import Message
from gzip import compress
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import Mock, patch
import zlib

import registry_archive as archive
from server import CompanionWorkspace, PlotterServer, RequestError


PROFILE = Path(__file__).resolve().parent.parent / "research" / "machine-profile.example.json"
ENDPOINT = "/api/registry-backup"
SAMPLE = {"version": 1, "entries": [{"name": "Тестовая деталь", "id": "synthetic", "data": {"z": 1, "a": 2}}],
          "kitLayouts": [], "models": [], "drawingModels": []}


def rar_with_payload(payload):
    return archive._PREFIX + archive._file_header(len(payload), zlib.crc32(payload)) + payload + archive._END


class RegistryArchiveTests(TestCase):
    def test_round_trip_rar5_with_single_member_and_ordered_utf8_json(self):
        data = archive.write_registry_archive(SAMPLE)
        self.assertTrue(data.startswith(b"Rar!\x1a\x07\x01\x00"))
        self.assertEqual(data.count(b"registry.json.gz"), 1)
        restored = archive.read_registry_archive(data)
        self.assertEqual(restored, SAMPLE)
        self.assertEqual(list(restored["entries"][0]["data"]), ["z", "a"])

    def test_rejects_invalid_headers_crc_lengths_and_trailing_data(self):
        data = archive.write_registry_archive(SAMPLE)
        start = len(archive._PREFIX)
        for index in (0, 8, start, start + 4, start + 7, len(data) - len(archive._END) - 1):
            broken = bytearray(data)
            broken[index] ^= 1
            with self.subTest(index=index), self.assertRaises(ValueError):
                archive.read_registry_archive(bytes(broken))
        for broken in (b"PK\x03\x04unsupported", data[:-1], data + b"tail", data + data):
            with self.assertRaises(ValueError):
                archive.read_registry_archive(broken)

    def test_rejects_incomplete_multiple_gzip_members_and_invalid_json_with_valid_rar_crc(self):
        good = compress(b'{"version":1}')
        for member in (good + good, good[:-3], compress(b'[1,2]'), compress(b'{"entries":[],"entries":[]}'),
                       compress(b'{"value":NaN}'), compress(b'{"name":"\xff"}')):
            with self.assertRaises(ValueError):
                archive.read_registry_archive(rar_with_payload(member))

    def test_enforces_wire_and_expanded_limits_on_read_and_write(self):
        sample = {"entries": ["a" * 1000]}
        data = archive.write_registry_archive(sample)
        with patch.object(archive, "MAX_REGISTRY_ARCHIVE", len(data) - 1):
            with self.assertRaises(archive.RegistryArchiveTooLarge):
                archive.read_registry_archive(data)
            with self.assertRaises(archive.RegistryArchiveTooLarge):
                archive.write_registry_archive(sample)
        with patch.object(archive, "MAX_REGISTRY_JSON", 100):
            with self.assertRaises(archive.RegistryArchiveTooLarge):
                archive.read_registry_archive(data)
            with self.assertRaises(archive.RegistryArchiveTooLarge):
                archive.write_registry_archive(sample)

    def test_companion_proxy_returns_rar_bytes_and_rejects_unexpected_mime(self):
        companion = CompanionWorkspace(Path("unused.json"), "https://example.test")
        data = archive.write_registry_archive(SAMPLE)
        headers = Message()
        headers["Content-Type"] = archive.RAR_MIME
        response = BytesIO(data)
        response.headers = headers
        companion.opener = Mock()
        companion.opener.open.return_value = response
        self.assertEqual(companion.production_request(ENDPOINT, rar=True), data)
        self.assertEqual(companion.opener.open.call_args.args[0].full_url, "https://example.test" + ENDPOINT)
        response = BytesIO(data)
        response.headers = Message()
        companion.opener.open.return_value = response
        with self.assertRaises(RequestError):
            companion.production_request(ENDPOINT, rar=True)


class RegistryArchiveHttpTests(TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        with patch("server.Controller", return_value=Mock()):
            self.server = PlotterServer(0, Path(root.name) / "workspace.json", profile_path=PROFILE)
        self.server.production = Mock()
        self.server.production.export_registry.return_value = SAMPLE
        self.server.production.restore_registry.return_value = {"entries": 1}
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path=ENDPOINT, body=None, headers=None):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as client:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            return response.status, response.headers, response.read()

    def test_get_download_and_post_restore_share_one_catalog(self):
        status, headers, body = self.request("GET")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), archive.RAR_MIME)
        self.assertIn("attachment; filename=plotter-registry.rar", headers["Content-Disposition"])
        self.assertEqual(archive.read_registry_archive(body), SAMPLE)
        self.assertEqual(self.request("GET", "/demo" + ENDPOINT)[2], body)
        content_type = {"Content-Type": "application/octet-stream"}
        status, _, contents = self.request("POST", body=body, headers=content_type)
        self.assertEqual((status, json.loads(contents)), (200, {"entries": 1}))
        self.server.production.restore_registry.assert_called_once_with(SAMPLE)

    def test_rejects_non_binary_malformed_and_cross_origin_requests(self):
        valid = archive.write_registry_archive(SAMPLE)
        self.assertEqual(self.request("POST", body=valid, headers={"Content-Type": "application/json"})[0], 415)
        content_type = {"Content-Type": "application/octet-stream"}
        self.assertEqual(self.request("POST", body=b"broken RAR", headers=content_type)[0], 400)
        foreign = {**content_type, "Origin": "https://unrelated.example"}
        self.assertEqual(self.request("POST", body=valid, headers=foreign)[0], 403)
        with patch.object(archive, "MAX_REGISTRY_JSON", 10):
            self.assertEqual(self.request("POST", body=valid, headers=content_type)[0], 413)
        self.server.production.restore_registry.assert_not_called()
