from datetime import date
import unittest

from order_analysis import analysis_period, order_analysis
from order_metrics import summarize_cuts


def completed_cut(day, elapsed, estimate, length, area, parts_area):
    start = f"2026-11-{day}T20:00:00+00:00" if elapsed is not None else None
    return {"status": "done", "stats": {
        "startedAt": start, "finishedAt": f"2026-11-{day}T21:00:00+00:00",
        "actualSeconds": elapsed, "estimatedWorkSeconds": estimate,
        "lengthMm": length, "areaMm2": area, "partsAreaMm2": parts_area,
        "utilizationPercent": 100 * parts_area / area, "cutDistanceMm": length * 2,
        "travelDistanceMm": length / 2, "cutSpeedMmS": 100, "travelSpeedMmS": 250,
        "source": "operator", "approximate": False,
    }}


class OrderAnalysisTests(unittest.TestCase):
    def test_period_uses_local_close_day_and_weighs_only_comparable_fabrics(self):
        previous = completed_cut(22, 3600, 50, 2000, 4000, 1000)
        first = completed_cut(23, 3600, 1800, 5000, 10000, 8000)
        unknown_start = completed_cut(23, None, 50, 12000, 30000, 3000)
        orders = [{"id": "a", "materials": [previous, first], "stats": summarize_cuts([previous, first])},
                  {"id": "b", "materials": [unknown_start], "stats": summarize_cuts([unknown_start])}]
        period = analysis_period({"from": "2026-11-24", "to": "2026-11-24", "tz": "Europe/Moscow"})
        report = order_analysis(orders, period)
        summary = report["summary"]
        self.assertEqual((summary["completedCuts"], summary["completedOrders"], summary["pairedCount"]), (2, 2, 1))
        self.assertEqual(summary["fabricMetersPerHour"], 5)
        self.assertEqual(summary["cutMetersPerHour"], 10)
        self.assertEqual(summary["efficiencyPercent"], 50)
        self.assertAlmostEqual(summary["utilizationPercent"], 27.5)
        self.assertEqual(summary["orderActualSeconds"], 25 * 3600)
        self.assertEqual(summary["cutSpeedsMmS"], [100])
        self.assertEqual(report["days"][0]["date"], "2026-11-24")
        earlier = order_analysis(orders, analysis_period({"from": "2026-11-23", "to": "2026-11-23", "tz": "Europe/Moscow"}))
        self.assertEqual(earlier["summary"]["completedOrders"], 0)
        self.assertEqual(earlier["orders"][0]["periodCutCount"], 1)
        self.assertIsNone(earlier["orders"][0]["stats"]["deltaSeconds"])

    def test_empty_and_invalid_period(self):
        start, end, zone = analysis_period({"to": "2026-11-24", "tz": "UTC"})
        self.assertEqual(start, date(2026, 10, 26))
        report = order_analysis([], (start, end, zone))
        self.assertEqual(report["orders"], [])
        self.assertIsNone(report["summary"]["efficiencyPercent"])
        for params in ({"from": "2026-11-32"}, {"from": "2027-01-01", "to": "2026-01-01"}, {"tz": "Invalid/Zone"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                analysis_period(params)
