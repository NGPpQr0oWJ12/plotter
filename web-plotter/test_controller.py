import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from unittest.mock import MagicMock, patch

from controller import Controller, ControllerBusyError, ControllerError, Hold, JOG_STOP
from machine import Contour
from panel_readback import PanelReadback
from xiao_job import encode_job, finalization


START = b"(1000,2000,0,-26997,0)\0"
STATUS = b"(0,0,1,0,0,4)\0"
STATUS_RUNNING = b"(0,0,1,0,0,3)\0"
STATUS_PAUSED = b"(0,0,1,0,0,9)\0"
STATUS_CANCELLED = b"(0,0,1,0,0,5)\0"
JOB = b"(0,1,5,2,4)\0"
EMPTY_JOB = b"(-1,0,0,0,0)\0"
UPLOADED_JOB = b"(0,1,2,2,4)\0"
BOARD = b"(1,2,6220618)\0"
SESSION = "test-hold-session-1"


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = ("192.0.2.10", 1234)
        self.controller = Controller(self.endpoint[0], upload_profile=lambda: 0.12, z_travel=100)
        self.socket = MagicMock()
        factory = patch("controller.socket.socket", return_value=self.socket)
        self.addCleanup(factory.stop)
        factory.start()
        self.socket.__enter__.return_value = self.socket
        self.addCleanup(self.controller.stop_active_hold)

    def replies(self, *replies):
        self.socket.recvfrom.side_effect = [(reply, self.endpoint) for reply in replies]

    def commands(self):
        return [call.args[0] for call in self.socket.sendto.call_args_list if call.args[0].startswith(b"cmd(")]

    def test_position_uses_the_expected_sender_and_reports_available_directions(self):
        self.socket.recvfrom.side_effect = [
            (b"(9,9,9,9,9)\0", (self.endpoint[0], 1235)),
            (b"(1,16538,0,-26997,0)\0", self.endpoint),
        ]
        result = self.controller.read_position()
        self.assertTrue(result["responded"])
        self.assertEqual(result["fields"], [0.01, 165.38, 0.0, -269.97, 0.0])
        self.assertEqual(result["hold"], {"xMinus": False, "xPlus": True, "yMinus": True, "yPlus": True,
                                         "zMinus": True, "zPlus": True, "wMinus": True, "wPlus": True})
        self.socket.bind.assert_called_once_with(("0.0.0.0", 1234))
        self.socket.sendto.assert_called_once_with(b"pos\0", self.endpoint)

    def test_status_event_is_reported_without_claiming_current_state(self):
        self.replies(STATUS)
        reading = self.controller.read_status()
        self.assertEqual(reading["fields"], [0, 0, 1, 0, 0, 4])
        self.assertEqual(reading["lastEvent"], {"code": 4, "label": "Обработка завершена"})
        self.socket.sendto.assert_called_once_with(b"sts\0", self.endpoint)

    def test_manual_speed_uses_fresh_status_only_for_matching_firmware(self):
        # The field and firmware are synthetic; the installed board remains unmapped.
        board = b"(1,2,9000000)\0"
        slow = b"(0,0,1,1,0,4)\0"
        self.controller.manual_speed_readback = PanelReadback.from_profile(
            {"firmware": 9000000, "source": "sts", "field": 4, "fastValue": 0, "slowValue": 1},
            {"fastValue": "fast", "slowValue": "slow"},
        )
        for status, firmware, mode in ((STATUS, board, "fast"), (slow, board, "slow"), (slow, BOARD, None),
                                       (b"(0,0,1,3,0,4)\0", board, None)):
            with self.subTest(status=status, firmware=firmware):
                self.replies(status, START, JOB, firmware)
                self.assertEqual(self.controller.read_live()["manualSpeed"], mode)

        self.replies(b"machine is busy\0")
        self.assertIsNone(self.controller.read_live()["manualSpeed"])
        self.assertEqual(self.commands(), [])

    def test_manual_speed_toggle_uses_observed_mode_instead_of_assuming_a_change(self):
        board = b"(1,2,9000000)\0"
        self.controller.manual_speed_readback = PanelReadback.from_profile(
            {"firmware": 9000000, "source": "gpr", "group": 19, "field": 2, "fastValue": 0, "slowValue": 1},
            {"fastValue": "fast", "slowValue": "slow"},
        )
        self.replies(STATUS, START, JOB, board, b"ok\0", b"par(19,0)\0")
        control = self.controller.control_session("arm", ready=True)
        self.assertEqual(control["manualSpeed"], "fast")
        self.replies(STATUS, START, START, board, b"ok\0", b"par(19,0)\0")
        result = self.controller.operate("manual-speed", control_token=control["controlToken"])
        self.assertTrue(result["sent"])
        self.assertEqual(result["manualSpeed"], "fast")
        self.replies(STATUS, START, START, board, b"machine is busy\0")
        result = self.controller.operate("manual-speed", control_token=control["controlToken"])
        self.assertTrue(result["sent"])
        self.assertIsNone(result["manualSpeed"])
        self.assertEqual(self.commands(), [b"cmd(1056)\0", b"cmd(1056)\0"])

    def test_panel_snapshot_sends_only_documented_reads(self):
        self.replies(STATUS, BOARD, b"ok\0", b"par(1,0,1234567,1)\0", b"par(4,2500,1650,100)\0",
                     b"par(18,0,0,0,0)\0", b"par(19,1)\0")
        sample = self.controller.read_panel_snapshot()
        self.assertEqual(sample["firmware"], 6220618)
        self.assertEqual(sample["status"], [0, 0, 1, 0, 0, 4])
        self.assertEqual(sample["parameters"]["1"], [1, 0, 0, 1])
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list],
                         [b"sts\0", b"dog\0", b"gpr(0)\0", b"gpr(1)\0", b"gpr(4)\0", b"gpr(18)\0", b"gpr(19)\0"])

    def test_panel_snapshot_stops_at_busy_status(self):
        self.replies(b"machine is busy\0")
        with self.assertRaises(ControllerError):
            self.controller.read_panel_snapshot()
        self.socket.sendto.assert_called_once_with(b"sts\0", self.endpoint)

    def test_extended_panel_snapshot_reads_the_same_groups_as_native_parameter_reader(self):
        self.replies(STATUS, BOARD, b"ok\0", *(f"par({group},0)\0".encode("ascii") for group in range(1, 20)))
        sample = self.controller.read_panel_snapshot(extended=True)
        self.assertEqual(list(sample["parameters"]), [str(group) for group in range(1, 20)])
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list][3:],
                         [f"gpr({group})\0".encode("ascii") for group in range(1, 20)])

    def test_job_index_reports_loaded_geometry_and_zero_index(self):
        self.replies(JOB, b"(-1,0,0,0,0)\0")
        reading = self.controller.read_job_index()
        self.assertTrue(reading["jobLoaded"])
        self.assertEqual((reading["currentIndex"], reading["indexLimit"]), (0, 5))
        empty = self.controller.read_job_index()
        self.assertFalse(empty["jobLoaded"])
        self.assertNotIn("currentIndex", empty)

    def test_lowered_head_has_no_token(self):
        self.replies(b"(1000,1000,-25,0,0)\0")
        reading = self.controller.read_position()
        self.assertNotIn("pilotToken", reading)
        self.assertEqual({name for name, allowed in reading["hold"].items() if allowed}, {"zPlus", "zMinus"})
        self.assertFalse(any(reading["operations"].values()))

    def test_hold_starts_once_renew_does_not_send_motion_release_checks_position(self):
        after = b"(1000,2250,0,-26997,0)\0"
        self.replies(START, STATUS, START, START, after, after)
        token = self.controller.read_position()["pilotToken"]
        result = self.controller.hold("start", SESSION, "y", 1, token, True)
        self.assertEqual(result["phase"], "holding")
        self.assertEqual(self.controller.hold("renew", SESSION)["phase"], "holding")
        released = self.controller.hold("stop", SESSION)
        self.assertTrue(released["positionStable"])
        self.assertEqual(released["position"]["y"], 22.5)
        self.assertIn("pilotToken", released)
        self.assertEqual(self.commands(), [b"cmd(1012,50000)\0", JOG_STOP, JOG_STOP, JOG_STOP])
        self.assertEqual(self.controller.hold("stop", SESSION), released)
        self.assertEqual(len(self.commands()), 4)

    def test_hold_has_controller_side_limit_to_work_area(self):
        edge = b"(239850,0,0,0,0)\0"
        self.replies(edge, STATUS, edge, edge, edge, edge)
        token = self.controller.read_position()["pilotToken"]
        self.assertEqual(self.controller.hold("start", SESSION, "x", 1, token, True)["phase"], "holding")
        self.controller.hold("stop", SESSION)
        self.assertEqual(self.commands()[0], b"cmd(1010,150)\0")

    def test_release_waits_for_deceleration_before_rearming(self):
        decelerating = b"(1020,2000,0,-26997,0)\0"
        stopped = b"(1090,2000,0,-26997,0)\0"
        self.replies(START, STATUS, START, START, decelerating, stopped, stopped)
        token = self.controller.read_position()["pilotToken"]
        self.assertEqual(self.controller.hold("start", SESSION, "x", 1, token, True)["phase"], "holding")
        release = self.controller.hold("stop", SESSION)
        self.assertTrue(release["positionStable"])
        self.assertEqual(release["position"]["x"], 10.9)
        self.assertIn("pilotToken", release)

    def test_stop_recovers_after_a_missing_position_reply_and_deceleration(self):
        now = [0.0]
        received = iter([
            b"(1020,2000,0,-26997,0)\0", None,
            b"(1050,2000,0,-26997,0)\0",
            b"(1090,2000,0,-26997,0)\0", b"(1090,2000,0,-26997,0)\0",
        ])

        def delay(seconds):
            now[0] += seconds

        def receive(_):
            reply = next(received)
            if reply is None:
                delay(self.socket.settimeout.call_args.args[0])
                raise TimeoutError
            return reply, self.endpoint

        self.socket.recvfrom.side_effect = receive
        with patch("controller.monotonic", side_effect=lambda: now[0]), patch("controller.sleep", side_effect=delay):
            release = self.controller._send_hold_stop(self.socket, Hold(SESSION, 0, reason="released"))
        self.assertTrue(release["positionStable"])
        self.assertEqual(release["positionCheck"], "stable")
        self.assertEqual(release["positionSamples"], 4)
        self.assertEqual(release["position"]["x"], 10.9)
        self.assertGreater(now[0], 0.75)
        self.assertEqual(self.commands(), [JOG_STOP] * 3)

    def test_stop_distinguishes_movement_from_insufficient_replies(self):
        for moving, expected in [(False, "insufficient"), (True, "changed")]:
            with self.subTest(moving=moving):
                now = [0.0]
                received = [0]

                def delay(seconds):
                    now[0] += seconds

                def receive(_):
                    received[0] += 1
                    if not moving and received[0] > 1:
                        delay(self.socket.settimeout.call_args.args[0])
                        raise TimeoutError
                    return f"({received[0] * 100},2000,0,-26997,0)\0".encode(), self.endpoint

                self.socket.recvfrom.side_effect = receive
                with patch("controller.monotonic", side_effect=lambda: now[0]), patch("controller.sleep", side_effect=delay):
                    release = self.controller._send_hold_stop(self.socket, Hold(SESSION, 0, reason="released"))
                self.assertFalse(release["positionStable"])
                self.assertEqual(release["positionCheck"], expected)
                self.assertNotIn("pilotToken", release)

    def test_session_renews_without_hardware_io_and_never_revives_an_expired_token(self):
        self.replies(STATUS, START, JOB)
        with patch("controller.monotonic", return_value=100.0) as clock:
            token = self.controller.control_session("arm", ready=True)["controlToken"]
            before = self.socket.sendto.call_count
            clock.return_value = 999.0
            self.assertEqual(self.controller.control_session("renew", token), {"armed": True, "expiresIn": 900})
            clock.return_value = 1001.0
            self.controller.require_control(token)
            self.assertEqual(self.controller.control_session("renew", "another"), {"armed": False})
            clock.return_value = 1899.0
            self.assertEqual(self.controller.control_session("renew", token), {"armed": False})
        self.assertEqual(self.socket.sendto.call_count, before)

    def test_unconfirmed_stop_keeps_control_and_blocks_motion_until_fresh_stable_positions(self):
        self.replies(STATUS, START, JOB, STATUS, START, START)
        token = self.controller.control_session("arm", ready=True)["controlToken"]
        self.assertEqual(self.controller.hold("start", SESSION, "x", 1, control_token=token)["phase"], "holding")
        self.socket.recvfrom.side_effect = TimeoutError
        with patch("controller.HOLD_SETTLE_SECONDS", 0.04):
            release = self.controller.hold("stop", SESSION)
        self.assertTrue(release["motionInhibited"])
        self.assertTrue(release["stopSent"])
        self.assertEqual(release["positionCheck"], "unreachable")
        self.controller.require_control(token)
        with self.assertRaisesRegex(ControllerError, "Остановка ручного движения"):
            self.controller.hold("start", "test-hold-session-2", "x", 1, control_token=token)
        with self.assertRaisesRegex(ControllerError, "Остановка ручного движения"):
            self.controller.operate("home", control_token=token)
        with self.assertRaisesRegex(ControllerError, "Остановка ручного движения"):
            self.controller.upload_job(*self.upload_fixture(), 0.12, ready=True, auto_run_off=True)
        self.replies(STATUS, START, JOB, STATUS, START, JOB)
        with patch("controller.monotonic", return_value=200.0) as clock:
            self.assertTrue(self.controller.read_live()["motionInhibited"])
            clock.return_value = 200.2
            self.assertFalse(self.controller.read_live()["motionInhibited"])
        self.assertEqual(self.commands(), [b"cmd(1010,50000)\0", JOG_STOP, JOG_STOP, JOG_STOP])

    def test_coordinate_move_sends_once_and_releases_motion_only_after_stable_target_readback(self):
        old_reached_event = b"(0,0,1,0,0,48)\0"
        target = b"(12345,6789,0,-26997,0)\0"
        self.replies(STATUS, START, JOB)
        with patch("controller.monotonic", return_value=100.0) as clock, patch("controller.sleep"):
            token = self.controller.control_session("arm", ready=True)["controlToken"]
            self.replies(old_reached_event, START, START)
            result = self.controller.operate("move-to", control_token=token, x=123.45, y=67.89)
            self.assertTrue(result["sent"])
            self.assertEqual(result["coordinateMove"], {"state": "moving", "x": 123.45, "y": 67.89})
            for action in ("move-to", "home"):
                with self.assertRaisesRegex(ControllerError, "Дождитесь завершения перемещения"):
                    self.controller.operate(action, control_token=token, x=10, y=20)
            with self.assertRaises(ControllerError):
                self.controller.hold("start", SESSION, "x", 1, control_token=token)
            with self.assertRaises(ControllerError):
                self.controller.upload_job(*self.upload_fixture(), 0.12, ready=True, auto_run_off=True)
            with self.assertRaises(ControllerError):
                self.controller.control_session("verify-stop", token, ready=True)
            for seconds, pose, expected in ((1, START, "moving"), (2, target, "moving"), (3, target, "reached")):
                self.replies(old_reached_event, pose, JOB)
                clock.return_value = 100.0 + seconds
                self.assertEqual(self.controller.read_live()["coordinateMove"]["state"], expected)
            self.replies(STATUS, target, target)
            unchanged = self.controller.operate("move-to", control_token=token, x=123.45, y=67.89)
            self.assertFalse(unchanged["sent"])
            self.assertEqual(unchanged["coordinateMove"]["state"], "reached")
        self.assertEqual(self.commands(), [b"cmd(1036,12345,6789)\0"])

    def test_move_x_uses_exact_work_coordinate_and_keeps_y_from_preflight(self):
        self.replies(STATUS, START, JOB)
        token = self.controller.control_session("arm", ready=True)["controlToken"]
        position = b"(237000,5364,0,-26997,0)\0"
        self.replies(STATUS, position, position)
        with patch("controller.sleep"):
            move = self.controller.move_x(2004.8, control_token=token)
        self.assertEqual(move["coordinateMove"], {"state": "moving", "x": 2004.8, "y": 53.64})
        self.assertEqual(self.commands(), [b"cmd(1036,200480,5364)\0"])

    def test_coordinate_move_rejects_invalid_target_unsafe_pose_and_active_event(self):
        self.replies(STATUS, START, JOB)
        token = self.controller.control_session("arm", ready=True)["controlToken"]
        for x, y in ((None, 5), (True, 5), ("1", 5), (float("inf"), 5), (0, float("nan")), (-1, 0), (2400.01, 0), (0, 1750.01)):
            with self.subTest(x=x, y=y), self.assertRaisesRegex(ControllerError, "Координата"):
                self.controller.operate("move-to", control_token=token, x=x, y=y)
        with self.assertRaisesRegex(ControllerError, "Сеанс закончился"):
            self.controller.operate("move-to", x=5, y=5)
        self.replies(b"(0,0,1,0,0,22)\0")
        with self.assertRaisesRegex(ControllerError, "текущей операции"):
            self.controller.operate("move-to", control_token=token, x=5, y=5)
        lowered = b"(1000,2000,100,0,0)\0"
        self.replies(STATUS, lowered, lowered)
        with self.assertRaisesRegex(ControllerError, "поднятый нож"), patch("controller.sleep"):
            self.controller.operate("move-to", control_token=token, x=5, y=5)
        self.assertEqual(self.commands(), [])

    def test_coordinate_move_timeout_requires_explicit_verification_at_wrong_position(self):
        self.replies(STATUS, START, JOB)
        with patch("controller.monotonic", return_value=100.0) as clock, patch("controller.sleep"):
            token = self.controller.control_session("arm", ready=True)["controlToken"]
            self.replies(STATUS, START, START)
            self.controller.operate("move-to", control_token=token, x=20, y=30)
            for seconds, expected in ((2, "moving"), (121, "unconfirmed"), (122, "unconfirmed")):
                self.replies(STATUS, START, JOB)
                clock.return_value = 100.0 + seconds
                self.assertEqual(self.controller.read_live()["coordinateMove"]["state"], expected)
            with self.assertRaisesRegex(ControllerError, "Перемещение не подтверждено"):
                self.controller.operate("home", control_token=token)
            self.replies(STATUS, START, START)
            verified = self.controller.control_session("verify-stop", token, ready=True)
            self.assertIsNone(verified["coordinateMove"])
            self.replies(STATUS, START, START)
            self.assertTrue(self.controller.operate("move-to", control_token=token, x=0, y=0)["sent"])
        self.assertEqual(self.commands(), [b"cmd(1036,2000,3000)\0", b"cmd(1036,0,0)\0"])

    def test_coordinate_move_loss_of_readback_cannot_confirm_arrival(self):
        self.replies(STATUS, START, JOB)
        with patch("controller.monotonic", return_value=100.0) as clock, patch("controller.sleep"):
            token = self.controller.control_session("arm", ready=True)["controlToken"]
            self.replies(STATUS, START, START)
            self.controller.operate("move-to", control_token=token, x=20, y=30)
            for seconds, expected in ((1, "moving"), (6, "unconfirmed")):
                clock.return_value = 100.0 + seconds
                self.socket.recvfrom.side_effect = TimeoutError
                self.assertEqual(self.controller.read_live()["coordinateMove"]["state"], expected)
            target = b"(2000,3000,0,-26997,0)\0"
            for seconds, expected in ((7, "unconfirmed"), (8, "reached")):
                self.replies(STATUS, target, JOB)
                clock.return_value = 100.0 + seconds
                self.assertEqual(self.controller.read_live()["coordinateMove"]["state"], expected)
        self.assertEqual(self.commands(), [b"cmd(1036,2000,3000)\0"])

    def test_failed_stop_send_requires_explicit_verification_without_motion(self):
        self.replies(STATUS, START, JOB, STATUS, START, START)
        token = self.controller.control_session("arm", ready=True)["controlToken"]
        self.assertEqual(self.controller.hold("start", SESSION, "x", 1, control_token=token)["phase"], "holding")
        self.socket.sendto.side_effect = OSError
        release = self.controller.hold("stop", SESSION)
        self.socket.sendto.side_effect = None
        self.assertFalse(release["stopSent"])
        self.assertEqual(release["positionCheck"], "stop-not-sent")
        self.assertTrue(release["motionInhibited"])
        self.replies(STATUS, START, JOB, STATUS, START, JOB, STATUS, START, START)
        self.assertTrue(self.controller.read_live()["motionInhibited"])
        self.assertTrue(self.controller.read_live()["motionInhibited"])
        with self.assertRaisesRegex(ControllerError, "Подтвердите"):
            self.controller.control_session("verify-stop", token, ready=False)
        verified = self.controller.control_session("verify-stop", token, ready=True)
        self.assertTrue(verified["armed"])
        self.assertFalse(verified["motionInhibited"])
        self.assertEqual(verified["position"]["x"], 10)
        self.assertEqual(self.commands(), [b"cmd(1010,50000)\0", JOG_STOP, JOG_STOP, JOG_STOP])

    def test_release_arriving_first_cancels_start(self):
        self.replies(START)
        token = self.controller.read_position()["pilotToken"]
        self.assertEqual(self.controller.hold("stop", SESSION)["phase"], "cancelled")
        late = self.controller.hold("start", SESSION, "x", 1, token, True)
        self.assertEqual(late["phase"], "cancelled")
        self.assertFalse(self.commands())

    def test_release_during_preflight_prevents_motion(self):
        self.replies(START)
        token = self.controller.read_position()["pilotToken"]
        entered, proceed = Event(), Event()

        def pending(client, reference):
            entered.set()
            if not proceed.wait(1.0):
                raise AssertionError("Test release never arrived")
            return reference, 4

        with patch.object(self.controller, "_preflight", side_effect=pending), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.controller.hold, "start", SESSION, "x", 1, token, True)
            self.assertTrue(entered.wait(1.0))
            release = pool.submit(self.controller.hold, "stop", SESSION)
            with self.controller._hold_condition:
                self.assertTrue(self.controller._hold_condition.wait_for(
                    lambda: self.controller._active_hold.reason == "released", timeout=1.0,
                ))
            proceed.set()
            self.assertEqual(first.result(1.0)["phase"], "cancelled")
            self.assertEqual(release.result(1.0)["phase"], "cancelled")
        self.assertFalse(self.commands())

    def test_lost_heartbeat_sends_stop_and_does_not_rearm(self):
        self.replies(START, STATUS, START, START, START, START)
        token = self.controller.read_position()["pilotToken"]
        with patch("controller.HOLD_LEASE_SECONDS", 0.04):
            result = self.controller.hold("start", SESSION, "y", -1, token, True)
            self.assertEqual(result["phase"], "holding")
            with self.controller._hold_condition:
                self.assertTrue(self.controller._hold_condition.wait_for(
                    lambda: self.controller._active_hold is None, timeout=1.0,
                ))
        expired = self.controller.hold("renew", SESSION)
        self.assertEqual(expired["phase"], "expired")
        self.assertNotIn("pilotToken", expired)
        self.assertEqual(self.commands(), [b"cmd(1013,2000)\0", JOG_STOP, JOG_STOP, JOG_STOP])

    def test_stale_pose_prevents_hold(self):
        moved = b"(1050,2000,0,-26997,0)\0"
        self.replies(START, STATUS, moved, moved)
        token = self.controller.read_position()["pilotToken"]
        self.assertEqual(self.controller.hold("start", SESSION, "x", -1, token, True)["phase"], "failed")
        self.assertFalse(self.commands())

    def test_feed_calls_native_cycle_without_writing_configuration(self):
        self.replies(START, STATUS, START, START)
        token = self.controller.read_position()["pilotToken"]
        self.assertTrue(self.controller.operate("feed", token, True)["sent"])
        self.assertEqual(self.commands(), [b"cmd(1048)\0"])
        with self.assertRaises(ControllerError):
            self.controller.operate("feed", token, True)

    def test_feed_mode_reads_parameter_group_without_writing_settings(self):
        self.replies(b"ok\0", b"par(1,23,0,1,0,0,0,0,0,0)\0", b"par(18,0,0,0,0,0,0,0,0,0)\0")
        result = self.controller.read_feed_mode()
        self.assertTrue(result["responded"])
        self.assertEqual(result["feedMode"], 1)
        self.assertEqual(result["feedLayout"], 0)
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list],
                         [b"gpr(0)\0", b"gpr(1)\0", b"gpr(18)\0"])

    def test_cut_requires_operator_confirmation_and_loaded_geometry(self):
        self.replies(START, STATUS, START, START, b"(0,1,0,0,0)\0")
        token = self.controller.read_position()["pilotToken"]
        with self.assertRaises(ControllerError):
            self.controller.operate("cut", token, True, False)
        with self.assertRaises(ControllerError):
            self.controller.operate("cut", token, True, True)
        self.assertFalse(self.commands())

    def test_loaded_cut_can_be_started_and_cancelled(self):
        self.replies(START, STATUS, START, START, JOB, STATUS)
        token = self.controller.read_position()["pilotToken"]
        self.assertTrue(self.controller.operate("cut", token, True, True)["sent"])
        self.assertTrue(self.controller.operate("cancel-cut")["sent"])
        self.assertEqual(self.commands(), [b"cmd(1002)\0", b"cmd(1006)\0"])

    def upload_fixture(self):
        contour = Contour(((20.0, 30.0), (30.0, 30.0)), False, "0")
        return (encode_job((contour,), tool=4, units="mm", width=2400, height=1750),
                finalization(paged=True, page_distance_mm=30.12))

    def test_upload_confirms_file_without_sending_a_cut_command(self):
        self.replies(START, STATUS, START, START, BOARD, b"hpgl1\0", b"[2]\0", b"endok\0")
        pose = self.controller.read_position()
        self.assertTrue(pose["operations"]["upload"])
        job, finish = self.upload_fixture()
        result = self.controller.upload_job(job, finish, 0.12, pose["pilotToken"], True, True)
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["points"], 2)
        self.assertFalse(result["uploadUncertain"])
        self.assertFalse(self.commands())
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list][-4:],
                         [b"dog\0", b"hpgl1\0", job.chunks[0].payload, b"end30120)\0"])

    def test_upload_requires_confirmed_auto_run_off_and_current_xiao_profile(self):
        self.replies(START)
        token = self.controller.read_position()["pilotToken"]
        job, finish = self.upload_fixture()
        with self.assertRaises(ControllerError):
            self.controller.upload_job(job, finish, 0.12, token, True, False)
        with self.assertRaises(ControllerError):
            self.controller.upload_job(job, finish, 0.13, token, True, True)
        self.socket.sendto.assert_called_once_with(b"pos\0", self.endpoint)

    def test_unconfirmed_upload_blocks_cut_until_explicit_recovery(self):
        pre_commit = [START, STATUS, START, START, BOARD, b"hpgl1\0", b"[2]\0"]
        after_failure = [START, STATUS, START, START, JOB]
        self.socket.recvfrom.side_effect = [
            *((reply, self.endpoint) for reply in pre_commit), TimeoutError,
            *((reply, self.endpoint) for reply in after_failure),
        ]
        token = self.controller.read_position()["pilotToken"]
        job, finish = self.upload_fixture()
        result = self.controller.upload_job(job, finish, 0.12, token, True, True)
        self.assertFalse(result["uploaded"])
        self.assertTrue(result["commitSent"])
        pose = self.controller.read_position()
        self.assertTrue(pose["uploadUncertain"])
        with self.assertRaises(ControllerError):
            self.controller.operate("cut", pose["pilotToken"], True, True)
        self.assertFalse(self.commands())
        recovery = self.controller.operate("cut", pose["pilotToken"], True, True, True)
        self.assertFalse(recovery["uploadUncertain"])
        self.assertEqual(self.commands(), [b"cmd(1002)\0"])

    def test_live_status_has_no_motion_token_and_session_can_dispatch_multiple_commands(self):
        self.replies(STATUS, START, JOB, STATUS, START, JOB,
                     STATUS, START, START, STATUS, START, START)
        live = self.controller.read_live()
        self.assertNotIn("pilotToken", live)
        self.assertEqual((live["currentIndex"], live["indexLimit"]), (0, 5))
        session = self.controller.control_session("arm", ready=True, auto_run_off=False)
        token = session["controlToken"]
        vacuum = self.controller.operate("vacuum", control_token=token)
        self.assertTrue(vacuum["sent"])
        self.assertEqual(vacuum["actuatorChanges"], {"vacuumEnabled": True})
        clamp = self.controller.operate("clamp", control_token=token)
        self.assertTrue(clamp["sent"])
        self.assertEqual(clamp["actuatorChanges"], {"clampLowered": True})
        with self.assertRaises(ControllerError):
            self.controller.operate("pause-cut", control_token=token)
        cancelled = self.controller.operate("cancel-cut", control_token=token)
        self.assertTrue(cancelled["sent"])
        self.assertEqual(cancelled["actuatorChanges"], {"clampLowered": False, "vacuumEnabled": False})
        self.assertEqual(self.commands(), [b"cmd(1047)\0", b"cmd(1055)\0", b"cmd(1006)\0"])
        self.controller.control_session("disarm", token=token)
        with self.assertRaises(ControllerError):
            self.controller.operate("vacuum", control_token=token)

    def test_vacuum_commands_use_the_native_button_and_preserve_the_loaded_job(self):
        token = self.arm_upload_session()
        uploaded = self.session_upload(token, before=EMPTY_JOB)
        receipt = uploaded["jobReceipt"]
        before = self.controller._run.snapshot()
        self.socket.sendto.reset_mock()
        for action, enabled in (("vacuum", True), ("vacuum-off", False)):
            with self.subTest(action=action):
                self.replies(STATUS, START, START)
                result = self.controller.operate(action, control_token=token)
                self.assertTrue(result["sent"])
                self.assertEqual(result["operation"], action)
                self.assertEqual(result["actuatorChanges"], {"vacuumEnabled": enabled})
                self.assertNotIn("jobReceipt", result)
                self.assertEqual(result["run"]["state"], "ready")
                self.assertEqual(result["run"]["events"], before["events"])
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list],
                         [b"sts\0", b"pos\0", b"pos\0", b"cmd(1047)\0"] * 2)
        self.replies(STATUS, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["jobReceipt"], receipt)

    def test_raise_clamp_uses_idle_cancel_with_both_actuator_changes(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token, before=EMPTY_JOB)["jobReceipt"]
        self.socket.sendto.reset_mock()
        self.replies(STATUS, START, START)
        result = self.controller.operate("raise-clamp", control_token=token)
        self.assertTrue(result["sent"])
        self.assertEqual(result["operation"], "raise-clamp")
        self.assertEqual(result["actuatorChanges"], {"clampLowered": False, "vacuumEnabled": False})
        self.assertIsNone(result["jobReceipt"])
        self.assertEqual(result["run"]["state"], "idle")
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list],
                         [b"sts\0", b"pos\0", b"pos\0", b"cmd(1006)\0"])
        with self.assertRaises(ControllerError):
            self.controller.operate("cut", control_token=token, job_receipt=receipt)
        self.assertEqual(self.commands(), [b"cmd(1006)\0"])

    def test_actuator_snapshot_tracks_sent_commands_across_control_sessions(self):
        initial = self.controller.actuator_snapshot()
        self.assertTrue(initial["epoch"])
        self.assertNotEqual(initial["epoch"], Controller(self.endpoint[0]).actuator_snapshot()["epoch"])
        self.assertEqual(initial, {"epoch": initial["epoch"], "revision": 0,
                                   "clampLowered": None, "vacuumEnabled": None})
        token = self.arm_upload_session()
        expected = initial
        for revision, action, clamp_lowered, vacuum_enabled in (
            (1, "clamp", True, None),
            (2, "vacuum", True, True),
            (3, "vacuum-off", True, False),
            (4, "vacuum", True, True),
            (5, "raise-clamp", False, False),
        ):
            with self.subTest(action=action, revision=revision):
                self.replies(STATUS, START, START)
                result = self.controller.operate(action, control_token=token)
                expected = {**initial, "revision": revision, "clampLowered": clamp_lowered,
                            "vacuumEnabled": vacuum_enabled}
                self.assertEqual(result["actuators"], expected)

        cancelled = self.controller.operate("cancel-cut", control_token=token)
        expected = {**expected, "revision": 6}
        self.assertEqual(cancelled["actuators"], expected)
        cancelled["actuators"]["vacuumEnabled"] = True
        self.assertEqual(self.controller.actuator_snapshot(), expected)
        self.controller.control_session("disarm", token)
        self.replies(STATUS, START, JOB, b"machine is busy\0", STATUS, START, JOB)
        self.assertEqual(self.controller.read_live()["actuators"], expected)
        busy = self.controller.read_live()
        self.assertTrue(busy["busy"])
        self.assertEqual(busy["actuators"], expected)
        with patch.object(self.controller, "_io", side_effect=ControllerBusyError("busy")):
            self.assertEqual(self.controller.read_live()["actuators"], expected)
        self.socket.recvfrom.side_effect = TimeoutError
        offline = self.controller.read_live()
        self.assertFalse(offline["responded"])
        self.assertEqual(offline["actuators"], expected)
        self.replies(STATUS, START, JOB)
        self.assertEqual(self.controller.control_session("arm", ready=True)["actuators"], expected)
        self.assertEqual(self.commands(), [b"cmd(1055)\0", b"cmd(1047)\0", b"cmd(1047)\0", b"cmd(1047)\0",
                                           b"cmd(1006)\0", b"cmd(1006)\0"])

    def test_actuator_snapshot_ignores_rejected_and_failed_dispatch(self):
        token = self.arm_upload_session()
        self.replies(STATUS, START, START)
        expected = self.controller.operate("clamp", control_token=token)["actuators"]

        self.replies(b"machine is busy\0")
        with self.assertRaises(ControllerError):
            self.controller.operate("vacuum", control_token=token)

        def fail_actuator_send(packet, endpoint):
            if packet == b"cmd(1047)\0":
                raise OSError("mock send failure")
            return len(packet)

        self.replies(STATUS, START, START)
        with patch.object(self.socket, "sendto", side_effect=fail_actuator_send), self.assertRaises(ControllerError):
            self.controller.operate("vacuum", control_token=token)
        self.assertEqual(self.controller.actuator_snapshot(), expected)
        self.replies(STATUS, START, START)
        self.assertEqual(self.controller.operate("set-zero", control_token=token)["actuators"], expected)

    def test_material_commands_require_control_and_an_idle_stationary_controller(self):
        token = self.arm_upload_session()
        material_actions = ("raise-clamp", "vacuum", "vacuum-off")
        for action in material_actions:
            with self.subTest(action=action):
                with self.assertRaises(ControllerError):
                    self.controller.operate(action)
                self.replies(b"machine is busy\0")
                with self.assertRaises(ControllerError):
                    self.controller.operate(action, control_token=token)
                self.replies(STATUS, START, b"(2000,2000,0,-26997,0)\0")
                with self.assertRaises(ControllerError):
                    self.controller.operate(action, control_token=token)
        self.replies(STATUS, START, START, START, START)
        self.controller.hold("start", SESSION, "x", 1, control_token=token)
        try:
            for action in material_actions:
                with self.subTest(action=action), self.assertRaises(ControllerError):
                    self.controller.operate(action, control_token=token)
        finally:
            self.controller.hold("stop", SESSION)
        self.assertNotIn(b"cmd(1006)\0", self.commands())
        self.assertNotIn(b"cmd(1047)\0", self.commands())

    def test_control_session_can_raise_z_and_disarm_stops_the_hold(self):
        lowered = b"(1000,2000,-500,0,0)\0"
        self.replies(STATUS, lowered, JOB, STATUS, lowered, lowered, START, START)
        token = self.controller.control_session("arm", ready=True)["controlToken"]
        result = self.controller.hold("start", SESSION, "z", 1, control_token=token)
        self.assertEqual(result["phase"], "holding")
        self.controller.control_session("disarm", token=token)
        result = self.controller.hold("stop", SESSION)
        self.assertTrue(result["stopSent"])
        self.assertNotIn("pilotToken", result)
        self.assertEqual(self.commands(), [b"cmd(1014,10000)\0", JOG_STOP, JOG_STOP, JOG_STOP])

    def arm_upload_session(self, auto_run_off=True):
        self.replies(STATUS, START, JOB)
        return self.controller.control_session("arm", ready=True, auto_run_off=auto_run_off)["controlToken"]

    def session_upload(self, token, before=JOB, after=UPLOADED_JOB):
        self.replies(STATUS, START, START, BOARD, before, b"hpgl1\0", b"[2]\0", b"endok\0", after)
        job, finish = self.upload_fixture()
        return self.controller.upload_job(job, finish, 0.12, control_token=token, auto_run_off=True,
                                          filename="раскрой.dxf")

    def test_session_upload_requires_auto_run_confirmation_each_time(self):
        token = self.arm_upload_session(auto_run_off=True)
        job, finish = self.upload_fixture()
        self.socket.sendto.reset_mock()
        for confirmation in (None, False, "true", 1):
            with self.subTest(confirmation=confirmation), self.assertRaises(ControllerError):
                self.controller.upload_job(job, finish, 0.12, control_token=token, auto_run_off=confirmation)
        with self.assertRaises(ControllerError):
            self.controller.upload_job(job, finish, 0.12, control_token="invalid", auto_run_off=True)
        self.socket.sendto.assert_not_called()

        token = self.arm_upload_session(auto_run_off=False)
        result = self.session_upload(token, before=EMPTY_JOB)
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["indexLimit"], 2)
        self.assertFalse(result["verificationRequired"])
        self.assertIsInstance(result["jobReceipt"], str)
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list][-5:],
                         [b"idx\0", b"hpgl1\0", job.chunks[0].payload, b"end30120)\0", b"idx\0"])
        self.assertFalse(self.commands())
        self.socket.sendto.reset_mock()
        self.assertTrue(self.controller.control_session("renew", token)["armed"])
        with self.assertRaisesRegex(ControllerError, "Auto Run"):
            self.controller.upload_job(job, finish, 0.12, control_token=token)
        self.socket.sendto.assert_not_called()

    def test_upload_without_pretransfer_index_does_not_send_graph(self):
        token = self.arm_upload_session()
        self.replies(STATUS, START, START, BOARD, b"machine is busy\0")
        job, finish = self.upload_fixture()
        with self.assertRaises(ControllerError):
            self.controller.upload_job(job, finish, 0.12, control_token=token, auto_run_off=True)
        self.assertNotIn(b"hpgl1\0", [call.args[0] for call in self.socket.sendto.call_args_list])
        self.assertFalse(self.commands())

    def test_manual_auto_run_confirmation_does_not_allow_upload_during_external_pause(self):
        token = self.arm_upload_session()
        self.replies(STATUS_PAUSED, START, START)
        job, finish = self.upload_fixture()
        with self.assertRaisesRegex(ControllerError, "Дождитесь окончания"):
            self.controller.upload_job(job, finish, 0.12, control_token=token, auto_run_off=True)
        self.assertNotIn(b"hpgl1\0", [call.args[0] for call in self.socket.sendto.call_args_list])
        self.assertFalse(self.commands())

    def test_end_ack_without_matching_zero_index_revokes_previous_receipt(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        for after in (JOB, b"(1,1,2,2,4)\0"):
            with self.subTest(after=after):
                result = self.session_upload(token, after=after)
                self.assertFalse(result["uploaded"])
                self.assertTrue(result["commitSent"])
                self.assertTrue(result["uploadUncertain"])
                self.assertNotIn("jobReceipt", result)
                with self.assertRaises(ControllerError):
                    self.controller.operate("cut", control_token=token, job_receipt=receipt, job_verified=True)
                self.assertFalse(self.commands())

    def test_same_count_cut_requires_verification_and_pause_toggle_requires_observed_state(self):
        token = self.arm_upload_session()
        result = self.session_upload(token, before=UPLOADED_JOB)
        self.assertTrue(result["verificationRequired"])
        receipt = result["jobReceipt"]
        for action, value in (("cut", None), ("cut", receipt), ("range", "other"), ("pause-cut", receipt)):
            with self.subTest(action=action, receipt=value), self.assertRaises(ControllerError):
                self.controller.operate(action, control_token=token, job_receipt=value)
        self.assertFalse(self.commands())

        self.replies(STATUS, START, START, UPLOADED_JOB)
        self.assertTrue(self.controller.operate("range", control_token=token, job_receipt=receipt)["sent"])
        self.replies(STATUS, START, START, UPLOADED_JOB)
        started = self.controller.operate("cut", control_token=token, job_receipt=receipt, job_verified=True)
        self.assertEqual(started["run"]["state"], "starting")
        self.replies(STATUS, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "starting")
        with self.assertRaises(ControllerError):
            self.controller.operate("pause-cut", control_token=token)

        self.replies(STATUS_RUNNING, START, UPLOADED_JOB)
        running = self.controller.read_live()["run"]
        self.assertEqual((running["state"], running["filename"], running["indexLimit"]),
                         ("running", "раскрой.dxf", 2))
        self.replies(STATUS_RUNNING, UPLOADED_JOB)
        self.assertEqual(self.controller.operate("pause-cut", control_token=token)["run"]["state"], "pausing")
        with self.assertRaises(ControllerError):
            self.controller.operate("resume-cut", control_token=token)
        self.replies(STATUS_PAUSED, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "paused")
        self.replies(STATUS_PAUSED, UPLOADED_JOB)
        self.assertEqual(self.controller.operate("resume-cut", control_token=token)["run"]["state"], "resuming")

        self.replies(STATUS_RUNNING)
        self.assertEqual(self.controller.operate("cancel-cut", control_token=token)["run"]["state"], "cancelling")
        self.replies(STATUS_CANCELLED, START, EMPTY_JOB)
        stopped = self.controller.read_live()["run"]
        self.assertEqual((stopped["state"], stopped["completedPages"]), ("cancelled", 0))
        self.assertIsNotNone(stopped["endedAt"])
        self.assertEqual(self.commands(), [b"cmd(1009)\0", b"cmd(1002)\0", b"cmd(1005)\0",
                                           b"cmd(1005)\0", b"cmd(1006)\0"])

    def test_busy_then_link_loss_keep_run_interlocked_until_operator_confirms_stop(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS, START, START, UPLOADED_JOB)
        self.controller.operate("cut", control_token=token, job_receipt=receipt)
        self.replies(STATUS_RUNNING, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "running")
        self.replies(b"machine is busy\0")
        busy = self.controller.read_live()
        self.assertTrue(busy["busy"])
        self.assertEqual(busy["run"]["state"], "running")
        self.assertFalse(busy["run"]["fresh"])
        self.assertNotIn("index", busy["run"])

        self.socket.recvfrom.side_effect = TimeoutError
        lost = self.controller.read_live()["run"]
        self.assertEqual(lost["state"], "unknown")
        self.assertFalse(lost["fresh"])
        for action in ("cut", "range", "feed", "home", "raise-clamp", "vacuum-off", "pause-cut", "resume-cut", "acknowledge-run"):
            with self.subTest(action=action), self.assertRaises(ControllerError):
                self.controller.operate(action, control_token=token, job_receipt=receipt)
        with self.assertRaises(ControllerError):
            self.controller.hold("start", SESSION, "x", 1, control_token=token)
        with self.assertRaises(ControllerError):
            self.session_upload(token)
        self.assertEqual(self.commands(), [b"cmd(1002)\0"])

        self.replies(STATUS, START, START)
        recovered = self.controller.operate("acknowledge-run", control_token=token, job_verified=True)
        self.assertTrue(recovered["acknowledged"])
        self.assertFalse(recovered["sent"])
        self.assertEqual(recovered["run"]["state"], "idle")
        self.assertEqual(self.commands(), [b"cmd(1002)\0"])

    def test_panel_cut_of_confirmed_upload_is_tracked_from_index_zero(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "ready")
        self.replies(STATUS_RUNNING, START, UPLOADED_JOB)
        live = self.controller.read_live()
        self.assertTrue(live["stopAvailable"])
        self.assertIsNone(live["externalMotion"])
        self.assertEqual(live["jobReceipt"], receipt)
        run = live["run"]
        self.assertEqual((run["state"], run["index"], run["indexLimit"]), ("running", 0, 2))
        self.assertTrue(run["startedFromPanel"])
        self.assertEqual(run["startedReceipt"], receipt)
        with self.assertRaises(ControllerError):
            self.controller.operate("cut", control_token=token, job_receipt=receipt)

        self.replies(STATUS, START, b"machine is busy\0")
        self.assertEqual(self.controller.read_live()["run"]["state"], "running")
        self.replies(STATUS, START, UPLOADED_JOB)
        complete = self.controller.read_live()
        self.assertFalse(complete["stopAvailable"])
        self.assertEqual((complete["run"]["state"], complete["run"]["completedPages"]), ("completed", 1))
        self.assertEqual(complete["run"]["startedAt"], run["startedAt"])
        self.assertEqual(complete["run"]["startedReceipt"], receipt)
        self.assertFalse(self.commands())

    def test_confirmed_stop_can_cancel_untracked_panel_cut_without_rearming(self):
        self.replies(STATUS_PAUSED, START, JOB)
        live = self.controller.read_live()
        self.assertEqual(live["externalMotion"], "paused")
        self.assertTrue(live["stopAvailable"])
        self.assertEqual(live["run"]["state"], "idle")
        self.replies(STATUS_PAUSED)
        pending = self.controller.operate("cancel-cut", confirmed_stop=True)
        self.assertFalse(pending["stopAvailable"])
        self.assertEqual(pending["run"]["state"], "cancelling")
        with self.assertRaises(ControllerError):
            self.controller.operate("cancel-cut", confirmed_stop=True)
        self.replies(STATUS_CANCELLED, START, EMPTY_JOB)
        live = self.controller.read_live()
        self.assertEqual(live["run"]["state"], "cancelled")
        self.assertFalse(live["stopAvailable"])
        self.assertEqual(self.commands(), [b"cmd(1006)\0"])

    def test_live_cut_poll_skips_optional_panel_readbacks(self):
        self.controller.auto_run_readback = MagicMock()
        self.controller.manual_speed_readback = MagicMock()
        self.replies(STATUS_RUNNING, START, JOB)
        self.assertTrue(self.controller.read_live()["stopAvailable"])
        self.assertEqual([call.args[0] for call in self.socket.sendto.call_args_list],
                         [b"sts\0", b"pos\0", b"idx\0"])

    def test_confirmed_stop_does_not_send_idle_cancel_or_assume_manual_feed_can_stop(self):
        for event in (STATUS, b"(0,0,1,0,0,50)\0", b"machine is busy\0"):
            with self.subTest(event=event):
                self.replies(event)
                with self.assertRaisesRegex(ControllerError, "Активный рез не подтверждён"):
                    self.controller.operate("cancel-cut", confirmed_stop=True)
        self.assertFalse(self.commands())

    def test_confirmed_stop_uses_recent_panel_cut_when_followup_status_is_busy(self):
        self.replies(STATUS_RUNNING, START, JOB, b"machine is busy\0")
        self.assertTrue(self.controller.read_live()["stopAvailable"])
        stopped = self.controller.operate("cancel-cut", confirmed_stop=True)
        self.assertEqual(stopped["baselineEvent"]["code"], 3)
        self.assertEqual(self.commands(), [b"cmd(1006)\0"])

    def test_confirmed_stop_rejects_a_stale_external_cut_without_new_status(self):
        self.replies(STATUS_RUNNING, START, JOB, b"machine is busy\0")
        self.assertTrue(self.controller.read_live()["stopAvailable"])
        with patch("controller.monotonic", return_value=monotonic() + 6):
            with self.assertRaisesRegex(ControllerError, "Активный рез не подтверждён"):
                self.controller.operate("cancel-cut", confirmed_stop=True)
        self.assertFalse(self.commands())

    def test_confirmed_stop_does_not_send_idle_cancel_when_tracked_cut_already_finished(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS_RUNNING, START, UPLOADED_JOB, STATUS, STATUS, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["startedReceipt"], receipt)
        with self.assertRaisesRegex(ControllerError, "уже сообщил об окончании"):
            self.controller.operate("cancel-cut", confirmed_stop=True)
        self.assertEqual(self.controller.read_live()["run"]["state"], "completed")
        self.assertFalse(self.commands())

    def test_confirmed_stop_can_cancel_pending_start_with_sticky_completion(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS, START, START, UPLOADED_JOB, STATUS)
        self.controller.operate("cut", control_token=token, job_receipt=receipt)
        self.assertEqual(self.controller.operate("cancel-cut", confirmed_stop=True)["run"]["state"], "cancelling")
        self.assertEqual(self.commands(), [b"cmd(1002)\0", b"cmd(1006)\0"])

    def test_cut_preflight_blocks_a_panel_start_before_live_poll(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS_RUNNING)
        with self.assertRaisesRegex(ControllerError, "Дождитесь окончания"):
            self.controller.operate("cut", control_token=token, job_receipt=receipt)
        self.replies(STATUS_RUNNING, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["startedReceipt"], receipt)
        self.assertFalse(self.commands())

    def test_cut_preflight_rejects_matching_job_with_nonzero_index(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS, START, START, b"(1,1,2,2,4)\0")
        with self.assertRaisesRegex(ControllerError, "уже прошёл часть задания"):
            self.controller.operate("cut", control_token=token, job_receipt=receipt)
        self.assertFalse(self.commands())

    def complete_session_cut(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS, START, START, UPLOADED_JOB)
        self.controller.operate("cut", control_token=token, job_receipt=receipt)
        self.replies(STATUS, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["completedPages"], 0)
        self.replies(STATUS_RUNNING, START, UPLOADED_JOB, STATUS, START, UPLOADED_JOB)
        self.controller.read_live()
        completed = self.controller.read_live()["run"]
        self.assertEqual((completed["state"], completed["completedPages"]), ("completed", 1))
        return token

    def test_completed_cut_and_material_feed_are_observed_separately(self):
        token = self.complete_session_cut()
        self.replies(b"(0,0,1,0,0,50)\0", START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "feeding")
        with self.assertRaises(ControllerError):
            self.controller.operate("feed", control_token=token)
        self.replies(b"(0,0,1,0,0,49)\0", START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "completed")
        self.assertEqual(self.commands(), [b"cmd(1002)\0"])

    def test_cut_progress_freezes_after_index_regression_and_finishes_at_verified_end(self):
        monitor = self.controller._run
        monitor.start(12, 4, "Запуск", "receipt")
        monitor.observe(3, "Начата обработка", (4, 1, 12), busy=False)
        self.assertEqual(monitor.snapshot()["completedIndex"], 3)

        monitor.observe(3, "Начата обработка", (10, 1, 12), busy=True)
        self.assertEqual(monitor.snapshot()["completedIndex"], 3)
        monitor.observe(3, "Начата обработка", (0, 1, 12), busy=False)
        monitor.observe(3, "Начата обработка", (10, 1, 12), busy=False)
        self.assertEqual(monitor.snapshot()["completedIndex"], 3)
        monitor.observe(4, "Обработка завершена", None, busy=False)
        self.assertEqual(monitor.snapshot()["completedIndex"], 3)
        monitor.observe(4, "Обработка завершена", (0, 1, 12), busy=False)
        complete = monitor.snapshot()
        self.assertEqual((complete["completedIndex"], complete["index"]), (12, 0))
        self.assertEqual(complete["state"], "completed")
        monitor.observe(4, "Обработка завершена", (0, 1, 0), busy=False)
        after_reset = monitor.snapshot()
        self.assertEqual((after_reset["completedIndex"], after_reset["indexLimit"]), (12, 12))
        self.assertNotIn("index", after_reset)

    def test_recent_manual_feed_is_not_attributed_to_completed_cut(self):
        token = self.complete_session_cut()
        self.replies(STATUS, START, START)
        self.controller.operate("feed", control_token=token)
        self.replies(b"(0,0,1,0,0,50)\0", START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "completed")

    def test_sticky_completion_does_not_link_later_feed_to_completed_cut(self):
        self.complete_session_cut()
        with patch("run_monitor.monotonic", return_value=monotonic() + 10):
            self.replies(STATUS, START, UPLOADED_JOB, b"(0,0,1,0,0,50)\0", START, UPLOADED_JOB)
            self.controller.read_live()
            self.assertEqual(self.controller.read_live()["run"]["state"], "completed")

    def test_cancel_during_feed_waits_for_feed_stop_verification(self):
        token = self.complete_session_cut()
        feed_started = b"(0,0,1,0,0,50)\0"
        self.replies(feed_started, START, UPLOADED_JOB, feed_started)
        self.controller.read_live()
        self.controller.operate("cancel-cut", control_token=token)
        self.replies(STATUS_CANCELLED, START, EMPTY_JOB)
        self.assertEqual(self.controller.read_live()["run"]["state"], "unknown")
        with self.assertRaises(ControllerError):
            self.session_upload(token)
        self.assertEqual(self.commands(), [b"cmd(1002)\0", b"cmd(1006)\0"])

    def test_failed_reupload_after_completed_cut_does_not_reuse_run_details(self):
        token = self.complete_session_cut()
        failed = self.session_upload(token, after=JOB)["run"]
        self.assertEqual((failed["state"], failed["completedPages"], failed["pages"]), ("error", 0, 0))
        self.assertIsNone(failed["startedAt"])
        self.assertIsNone(failed["endedAt"])
        self.assertIsNone(failed["updatedAt"])
        self.assertEqual(len(failed["events"]), 2)
        self.assertTrue(self.session_upload(token)["uploaded"])

    def test_index_change_before_cut_invalidates_receipt_without_movement(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(STATUS, START, START, JOB)
        with self.assertRaises(ControllerError):
            self.controller.operate("cut", control_token=token, job_receipt=receipt)
        self.assertFalse(self.commands())
        self.replies(STATUS, START, UPLOADED_JOB)
        live = self.controller.read_live()
        self.assertIsNone(live["jobReceipt"])
        self.assertTrue(live["uploadUncertain"])

    def test_live_busy_keeps_receipt_and_rearming_revokes_it(self):
        token = self.arm_upload_session()
        receipt = self.session_upload(token)["jobReceipt"]
        self.replies(b"machine is busy\0")
        self.assertNotIn("jobReceipt", self.controller.read_live())
        self.replies(STATUS, START, UPLOADED_JOB)
        self.assertEqual(self.controller.read_live()["jobReceipt"], receipt)
        self.replies(STATUS, START, UPLOADED_JOB)
        new_session = self.controller.control_session("arm", ready=True, auto_run_off=True)
        self.assertIsNone(new_session["jobReceipt"])
        with self.assertRaises(ControllerError):
            self.controller.operate("range", control_token=new_session["controlToken"], job_receipt=receipt)
        self.assertFalse(self.commands())

    def test_timeout_and_port_contention_never_send_actuation(self):
        self.socket.recvfrom.side_effect = TimeoutError
        self.assertFalse(self.controller.read_position()["responded"])
        self.socket.bind.side_effect = OSError
        self.assertFalse(self.controller.read_status()["responded"])
        self.assertFalse(self.commands())


if __name__ == "__main__":
    unittest.main()
