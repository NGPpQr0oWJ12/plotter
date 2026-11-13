import unittest
from unittest.mock import MagicMock, Mock, patch

from auto_run import AutoRunReadback
from controller import Controller, ControllerError, FIRMWARE_WITH_FINAL_ACK
from machine import Contour
from xiao_job import encode_job, finalization


FIRMWARE = 987654
PARAMETER_PROFILE = {
    "firmware": FIRMWARE, "source": "gpr", "group": 12, "field": 3,
    "offValue": -7, "onValue": 2,
}
STATUS_PROFILE = {
    "firmware": FIRMWARE, "source": "sts", "field": 4, "offValue": 0, "onValue": 1,
}


class FakeExchange:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def __call__(self, client, packet, pattern, timeout=0.45):
        self.requests.append(packet)
        reply = next(self.replies)
        if isinstance(reply, OSError):
            raise reply
        if reply == b"machine is busy":
            return None, True
        return pattern.fullmatch(reply) if reply is not None else None, False


class AutoRunReadbackTests(unittest.TestCase):
    def test_absent_or_invalid_mapping_is_not_enabled(self):
        invalid = (
            None, {}, [], {"firmware": FIRMWARE}, {**PARAMETER_PROFILE, "firmware": True},
            {**PARAMETER_PROFILE, "firmware": 0}, {**PARAMETER_PROFILE, "firmware": str(FIRMWARE)},
            {**PARAMETER_PROFILE, "source": "other"}, {**PARAMETER_PROFILE, "group": 20},
            {**PARAMETER_PROFILE, "group": 0}, {**PARAMETER_PROFILE, "field": 1},
            {**PARAMETER_PROFILE, "field": 11}, {**PARAMETER_PROFILE, "field": True},
            {**PARAMETER_PROFILE, "offValue": 2}, {**PARAMETER_PROFILE, "onValue": "2"},
            {**STATUS_PROFILE, "group": 1}, {**STATUS_PROFILE, "field": 6},
            {**STATUS_PROFILE, "offValue": -1}, {**STATUS_PROFILE, "mask": 0},
            {**STATUS_PROFILE, "mask": None}, {**STATUS_PROFILE, "mask": 2, "onValue": 1},
            {**STATUS_PROFILE, "mask": 0x100000000}, {**STATUS_PROFILE, "unknown": 1},
        )
        for profile in invalid:
            with self.subTest(profile=profile):
                self.assertIsNone(AutoRunReadback.from_profile(profile))

    def test_firmware_mismatch_performs_no_read(self):
        adapter = AutoRunReadback.from_profile(PARAMETER_PROFILE)
        exchange = Mock()
        for firmware in (None, FIRMWARE + 1, float(FIRMWARE), str(FIRMWARE)):
            with self.subTest(firmware=firmware):
                self.assertIsNone(adapter.read(None, exchange, firmware))
        exchange.assert_not_called()

    def test_parameter_read_uses_native_handshake_and_exact_value(self):
        adapter = AutoRunReadback.from_profile(PARAMETER_PROFILE)
        for wire, expected in ((b"par(12,9,-7,0)", True), (b"par(12,9,2,0)", False),
                               (b"par(12,9,0,0)", None)):
            with self.subTest(wire=wire):
                exchange = FakeExchange(b"ok", wire)
                self.assertIs(adapter.read(None, exchange, FIRMWARE), expected)
                self.assertEqual(exchange.requests, [b"gpr(0)\0", b"gpr(12)\0"])

    def test_parameter_read_rejects_busy_missing_or_malformed_replies(self):
        adapter = AutoRunReadback.from_profile(PARAMETER_PROFILE)
        cases = (
            (b"machine is busy",), (b"ack",), (TimeoutError(),), (b"ok", None),
            (b"ok", b"machine is busy"), (b"ok", b"par(11,9,-7,0)"),
            (b"ok", b"par(12,9)"), (b"ok", b"par(12,9,-7)unexpected"), (b"ok", OSError()),
        )
        for replies in cases:
            with self.subTest(replies=replies):
                exchange = FakeExchange(*replies)
                self.assertIsNone(adapter.read(None, exchange, FIRMWARE))

    def test_status_read_uses_fresh_fields_or_queries_when_omitted(self):
        adapter = AutoRunReadback.from_profile(STATUS_PROFILE)
        exchange = Mock()
        self.assertIs(adapter.read(None, exchange, FIRMWARE, [0, 0, 1, 0, 0, 4]), True)
        self.assertIs(adapter.read(None, exchange, FIRMWARE, (0, 0, 1, 1, 0, 4)), False)
        for fields in ([0, 0, 1, 2, 0, 4], [0, 0, 1, False, 0, 4], [0, 0, 1, 0, 0], "000004"):
            self.assertIsNone(adapter.read(None, exchange, FIRMWARE, fields))
        exchange.assert_not_called()
        exchange = FakeExchange(b"(0,0,1,0,0,4)")
        self.assertIs(adapter.read(None, exchange, FIRMWARE), True)
        self.assertEqual(exchange.requests, [b"sts\0"])
        self.assertIsNone(adapter.read(None, FakeExchange(b"(0,0,1,0,0)"), FIRMWARE))

    def test_mask_ignores_unrelated_flags_and_rejects_unmapped_state(self):
        adapter = AutoRunReadback.from_profile({**STATUS_PROFILE, "mask": 12, "onValue": 8})
        for value, expected in ((16, True), (24, False), (28, None), (0x100000000, None)):
            with self.subTest(value=value):
                self.assertIs(adapter.read(None, Mock(), FIRMWARE, [0, 0, 1, value, 0, 4]), expected)

        adapter = AutoRunReadback.from_profile({
            **PARAMETER_PROFILE, "mask": 0x80000000, "offValue": 0, "onValue": 0x80000000,
        })
        self.assertIs(adapter.read(None, FakeExchange(b"ok", b"par(12,9,-2147483648)"), FIRMWARE), False)


class ControllerAutoRunTests(unittest.TestCase):
    # Synthetic firmware and status field. These fixtures do not define the installed board's mapping.
    firmware = FIRMWARE_WITH_FINAL_ACK + 100
    off = b"(0,0,1,0,0,4)\0"
    on = b"(0,0,1,1,0,4)\0"
    unknown = b"(0,0,1,2,0,4)\0"
    pose = b"(2000,2000,0,0,0)\0"
    previous = b"(0,1,5,2,4)\0"

    def setUp(self):
        profile = {**STATUS_PROFILE, "firmware": self.firmware}
        self.board = f"(1,2,{self.firmware})".encode("ascii") + b"\0"
        self.controller = Controller("192.0.2.10", upload_profile=lambda: 0.12,
                                     auto_run_readback=AutoRunReadback.from_profile(profile))
        self.socket = MagicMock()
        self.socket.__enter__.return_value = self.socket
        factory = patch("controller.socket.socket", return_value=self.socket)
        factory.start()
        self.addCleanup(factory.stop)
        line = Contour(((20.0, 30.0), (30.0, 30.0)), False, "0")
        self.job = encode_job((line,), tool=4, units="mm", width=2400, height=1750)
        self.finish = finalization(paged=True, page_distance_mm=30.12)

    def replies(self, *replies):
        self.socket.recvfrom.side_effect = [(reply, self.controller.endpoint) for reply in replies]

    def arm(self, status):
        self.replies(status, self.pose, self.previous, self.board)
        return self.controller.control_session("arm", ready=True)

    def test_upload_rechecks_mode_before_sending_geometry(self):
        control = self.arm(self.off)
        self.assertIs(control["autoRunOff"], True)
        for changed_mode in (self.on, self.unknown):
            with self.subTest(changed_mode=changed_mode), self.assertRaises(ControllerError):
                self.replies(self.off, self.pose, self.pose, self.board, self.previous, changed_mode)
                self.controller.upload_job(self.job, self.finish, 0.12,
                                           control_token=control["controlToken"], auto_run_off=True)
        self.assertFalse(any(call.args[0] == b"hpgl1\0" for call in self.socket.sendto.call_args_list))

    def test_current_off_readback_allows_upload_without_attestation(self):
        control = self.arm(self.on)
        self.assertIs(control["autoRunOff"], False)
        self.replies(self.off, self.pose, self.pose, self.board, self.previous, self.off,
                     b"hpgl1\0", b"[2]\0", b"endok\0", b"(0,1,2,2,4)\0")
        result = self.controller.upload_job(self.job, self.finish, 0.12, control_token=control["controlToken"])
        self.assertTrue(result["uploaded"])
        self.assertTrue(result["jobReceipt"])
        self.assertEqual(result["indexLimit"], 2)
        self.assertFalse(any(call.args[0].startswith(b"cmd(") for call in self.socket.sendto.call_args_list))


if __name__ == "__main__":
    unittest.main()
