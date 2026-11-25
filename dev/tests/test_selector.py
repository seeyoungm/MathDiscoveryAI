from copy import deepcopy
import unittest

from mdai.selector import check_adequacy, fit_selector, split_for_group


OPTIONS = ("rewrite", "balance")
EARLY = dict(input_bits=4, output_bits=1, source_and_count=16, source_depth=4)
LATE = dict(input_bits=8, output_bits=1, source_and_count=16, source_depth=4)


def sample():
    """Invented complete menus from toy task groups, not project evidence."""
    return [
        {"task_id": f"fake-{i}", "group_id": f"group-{i//2}", "family": "A",
         "partition": "development", "repeat": rep, "features": dict(LATE if i % 4 < 2 else EARLY),
         "action": act, "outcome": {"equivalent": True, "depth_ok": True,
         "and_reduction": 7 if (i % 4 < 2) == (act == "balance") else 0}}
        for i in range(160) for rep in range(2) for act in OPTIONS
    ]


class SelectorTests(unittest.TestCase):
    def test_fit_is_explicit_and_deterministic(self):
        data = sample()
        with self.assertRaises(PermissionError):
            fit_selector(data, OPTIONS)
        model, report = fit_selector(data, OPTIONS, allow_fit=True)
        shuffled, replay = fit_selector(reversed(data), reversed(OPTIONS), allow_fit=True)
        self.assertEqual(model, shuffled)
        self.assertEqual(report, replay)
        self.assertEqual(model.rank("A", EARLY)[0][0], "rewrite")
        self.assertEqual(model.rank("A", LATE)[0][0], "balance")
        self.assertEqual(report["ranked_dev_mean_AND_saved"], 7)
        self.assertLess(report["constant_dev_mean_AND_saved"], 7)
        self.assertFalse(report["claims_measured_runtime_or_generalization"])
        self.assertEqual(report["timing_readiness"], "not_assessed")

    def test_dev_outcomes_do_not_change_ranking(self):
        data = sample()
        model, report = fit_selector(data, OPTIONS, allow_fit=True)
        for row in data:
            if split_for_group(row["group_id"]) == "dev":
                row["outcome"]["and_reduction"] = 7 - row["outcome"]["and_reduction"]
        changed, new_report = fit_selector(data, OPTIONS, allow_fit=True)
        self.assertEqual(model, changed)
        self.assertEqual(report["ranked_dev_mean_AND_saved"], 7)
        self.assertEqual(new_report["ranked_dev_mean_AND_saved"], 0)

    def test_candidate_fields_and_non_dev_rejected(self):
        data = sample()[:8]
        bad = deepcopy(data)
        bad[0]["features"]["candidate_gate_count"] = 2
        with self.assertRaisesRegex(ValueError, "whitelist"):
            check_adequacy(bad, OPTIONS)
        bad = deepcopy(data)
        bad[0]["partition"] = "A_final"
        with self.assertRaisesRegex(ValueError, "development"):
            check_adequacy(bad, OPTIONS)
        bad = deepcopy(data)
        bad[0]["family"] = "C"
        with self.assertRaises(ValueError):
            check_adequacy(bad, OPTIONS)
        m, _ = fit_selector(sample(), OPTIONS, allow_fit=True)
        with self.assertRaisesRegex(ValueError, "whitelist"):
            m.rank("A", {**EARLY, "equivalent": True})

    def test_coverage_and_incomplete_menu(self):
        data = sample()
        report = check_adequacy(data[:8], OPTIONS)
        self.assertFalse(report["ready"])
        self.assertIn("too_few_train_groups", report["reasons"])
        self.assertIn("too_few_dev_groups", report["reasons"])
        report = check_adequacy(data[1:], OPTIONS)
        self.assertIn("incomplete_logged_action_menu", report["reasons"])
        with self.assertRaisesRegex(ValueError, "adequacy"):
            fit_selector(data[1:], OPTIONS, allow_fit=True)
        for row in data:
            row["outcome"]["equivalent"] = False
        self.assertEqual(check_adequacy(data, OPTIONS)["reasons"],
            ["too_few_verified_train_groups_per_action", "no_positive_train_signal"])

    def test_bad_provenance_and_label_rejected(self):
        data = sample()[:8]
        bad = deepcopy(data)
        bad[1]["group_id"] = "different"
        with self.assertRaisesRegex(ValueError, "drifted"):
            check_adequacy(bad, OPTIONS)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            check_adequacy(data + data[:1], OPTIONS)
        bad = deepcopy(data)
        bad[0]["outcome"]["and_reduction"] = 17
        with self.assertRaisesRegex(ValueError, "label"):
            check_adequacy(bad, OPTIONS)


if __name__ == "__main__":
    unittest.main()
