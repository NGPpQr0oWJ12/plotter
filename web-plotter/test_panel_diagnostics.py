import unittest

from panel_diagnostics import compare_panel_samples


def sample(status=None, *, firmware=6220618, parameters=None):
    result = {"firmware": firmware, "status": status if status is not None else [0, 0, 1, 0, 0, 4]}
    if parameters is not None:
        result["parameters"] = parameters
    return result


class PanelDiagnosticsTests(unittest.TestCase):
    def test_reports_stable_status_bit_but_ignores_last_event_and_drift(self):
        first = sample([4, 0, 1, 0, 0, 4])
        second = sample([12, 0, 7, 0, 0, 9])
        noisy_second = sample([12, 0, 6, 0, 0, 9])
        self.assertEqual(compare_panel_samples([first, first, second, noisy_second, first, first]), [
            {"source": "sts", "field": 1, "aValue": 0, "bValue": 8, "mask": 8},
        ])

    def test_keeps_stable_scalar_values_and_handles_signed_flag_word(self):
        first = sample(parameters={"19": [19, 12], "1": [1, 0, 2, 5]})
        second = sample(parameters={"1": [1, -2147483648, 5, 10], "19": [19, 12]})
        self.assertEqual(compare_panel_samples([first, first, second, second, first, first]), [
            {"source": "gpr", "group": 1, "field": 2, "aValue": 0, "bValue": 2147483648, "mask": 2147483648},
            {"source": "gpr", "group": 1, "field": 4, "aValue": 5, "bValue": 10},
        ])

    def test_rejects_missing_samples_layout_changes_and_mixed_firmware(self):
        first = sample(parameters={"1": [1, 0, 0]})
        for invalid in (
            [first] * 5,
            [first] * 5 + [sample(firmware=6220619, parameters={"1": [1, 0, 0]})],
            [first] * 5 + [sample(parameters={"1": [1, 0]})],
            [first] * 5 + [sample(parameters={"18": [18, 0, 0]})],
            [first] * 5 + [sample(parameters={"1": [18, 0, 0]})],
            [first] * 5 + [sample(parameters={"1": [1, 0, True]})],
        ):
            with self.subTest(samples=invalid):
                with self.assertRaises(ValueError):
                    compare_panel_samples(invalid)

    def test_change_that_does_not_return_to_baseline_is_not_a_candidate(self):
        first = sample([0, 0, 1, 0, 0, 4])
        second = sample([4, 0, 1, 0, 0, 4])
        self.assertEqual(compare_panel_samples([first, first, second, second, second, second]), [])


if __name__ == "__main__":
    unittest.main()
