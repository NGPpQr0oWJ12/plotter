import unittest
from unittest.mock import patch

from demo_controller import DemoController


class DemoManualMotionTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        fake_clock = patch("demo_controller.monotonic", side_effect=lambda: self.now)
        fake_clock.start()
        self.addCleanup(fake_clock.stop)
        self.controller = DemoController(1200, 800)
        self.token = self.controller.control_session("arm", ready=True)["controlToken"]

    def operate(self, action, **values):
        return self.controller.operate(action, control_token=self.token, **values)

    def sample(self, seconds):
        self.now += seconds
        return self.controller.read_live()["position"]

    def test_go_zero_and_home_report_intermediate_xy_and_final_position(self):
        self.operate("move-to", x=300, y=90)
        self.sample(3)
        self.operate("set-zero")
        self.operate("move-to", x=60, y=210)
        self.sample(3)
        self.operate("go-zero")
        position = self.sample(.3)
        self.assertTrue(60 < position["x"] < 300 and 90 < position["y"] < 210)
        self.assertEqual(self.sample(3), dict(x=300, y=90, z=0, w=0, a=0))
        self.operate("home")
        position = self.sample(.3)
        self.assertTrue(0 < position["x"] < 300 and 0 < position["y"] < 90)
        self.assertEqual(self.sample(3), dict(x=0, y=0, z=0, w=0, a=0))
        self.assertIsNone(self.controller.read_live()["coordinateMove"])
        self.assertEqual(self.controller.read_live()["lastEvent"]["code"], 2)

    def test_jog_renew_returns_actual_pose_and_stop_or_lease_expiry_freezes_it(self):
        session = "demo-hold-session"
        self.controller.hold("start", session, "x", 1, control_token=self.token)
        self.now += .15
        first = self.controller.hold("renew", session)["position"]
        self.assertAlmostEqual(first["x"], 4.5)
        self.now += .15
        stopped = self.controller.hold("stop", session)["position"]
        self.assertGreater(stopped["x"], first["x"])
        self.assertEqual(self.sample(1), stopped)
        session = "demo-other-session"
        self.controller.hold("start", session, "y", 1, control_token=self.token)
        expired = self.sample(4)
        self.assertGreater(expired["y"], stopped["y"])
        self.assertEqual(self.sample(1), expired)

    def test_stopping_during_home_preserves_measured_position(self):
        self.operate("move-to", x=170, y=230)
        self.sample(2)
        self.operate("home")
        before_stop = self.sample(.2)
        stopped = self.operate("cancel-cut")
        self.assertIsNone(stopped["coordinateMove"])
        self.assertEqual(stopped["position"], before_stop)
        self.assertEqual(self.sample(2), before_stop)
