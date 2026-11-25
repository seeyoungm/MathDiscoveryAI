import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mdai.abc_runner import RECIPES, encode_binary
from mdai.candidates import seed_graph
from mdai.devgen import generate_development
from mdai.w4review import (
    ACTIONS, REPEATS, cohort_records, pinned, strict_json, validate_rows, verify_recipe_artifacts,
)


class Week4Review(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = generate_development(1, 0)[0]
        g = seed_graph(cls.record)
        m = g.metrics()
        cls.rows = [dict(task_id=cls.record["task_id"], group_id=cls.record["canonical_id"],
                         family="A", partition="development", action=a, repeat=r,
                         features=dict(input_bits=4, output_bits=1, source_and_count=m.and_count,
                                       source_depth=m.depth),
                         outcome=dict(equivalent=True, depth_ok=True, and_reduction=0))
                    for a in ACTIONS for r in REPEATS]

    def test_full_cartesian_and_mutations(self):
        self.assertEqual(len(validate_rows(self.rows, [self.record])), 9)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            validate_rows(self.rows[:-1], [self.record])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_rows(self.rows + self.rows[:1], [self.record])
        for field, invalid, error in (("repeat", False, "invalid"), ("group_id", "other", "provenance"),
                                      ("partition", "evaluation", "provenance"),
                                      ("features", {**self.rows[0]["features"], "output_bits": True}, "provenance"),
                                      ("outcome", dict(equivalent=False, depth_ok=False, and_reduction=0), "verified")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = invalid
                validate_rows(rows, [self.record])

    def test_reject_cohort_overlap(self):
        task = self.record["task_id"]
        with self.assertRaisesRegex(ValueError, "overlap"):
            cohort_records([self.record], {"task_ids": [task]}, {"task_ids": [task]})

    def test_predicted_proof_is_not_a_verified_receipt(self):
        row = self.rows[0]
        graph = seed_graph(self.record)
        raw = encode_binary(graph)
        metrics = vars(graph.metrics())
        cmd = "; ".join(("read_aiger input.aig", *RECIPES[row["action"]], "write_aiger -s output.aig"))
        receipt = dict(recipe=row["action"], literal_commands=cmd, status="VERIFIED", equivalent=True,
                       input_metrics=metrics, output_metrics=metrics, elapsed_s=1.0, budget_s=5,
                       eligible=True)
        verify_recipe_artifacts(row, self.record, receipt, raw, raw)
        with self.assertRaises(ValueError):
            verify_recipe_artifacts(row, self.record, {**receipt, "equivalent": None}, raw, raw)
        with self.assertRaises(ValueError):
            verify_recipe_artifacts(row, self.record, {**receipt, "elapsed_s": 5}, raw, raw)
        with self.assertRaises(ValueError):
            verify_recipe_artifacts(row, self.record, receipt, b"wrong", raw)
        graph.outputs = tuple(x ^ 1 for x in graph.outputs)
        with self.assertRaisesRegex(ValueError, "not prove equivalence"):
            verify_recipe_artifacts(row, self.record, receipt, raw, encode_binary(graph))

    def test_explicit_pins_and_ambiguous_json(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "file"
            path.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                pinned(path, "0" * 64)
        for source in ('{"a":1,"a":1}', '{"a":NaN}'):
            with self.assertRaises(ValueError):
                strict_json(source)


if __name__ == "__main__":
    unittest.main()
