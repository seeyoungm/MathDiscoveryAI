from unittest.mock import patch
import unittest
from mdai.abc_runner import ABCResult
from mdai.aig import AIG
from mdai.w3runner import run_match
from mdai.w3verify import verify_z3

def stub(labels=("a","b")):
    c=AIG(labels)
    c.outputs=(c.and_(*c.input_literals),)
    return c

class AuditTests(unittest.TestCase):
    def test_proved_abc_duplicate_cannot_win_but_keeps_zero_gain(self):
        g=stub()
        same=ABCResult("VERIFIED",g,g.metrics(),g.metrics(),True,True,0.0,"valid",None)
        with patch("mdai.w3runner.run_abc",return_value=same):
            result=run_match(g,task_id="A-smoke",arm="abc",repeat=0,seed=0,abc_path="/abc")
        self.assertEqual(len(result["attempts"]),3)
        self.assertFalse(result["verified_improvement"])
        for row in result["attempts"]:
            self.assertEqual(row["status"],"DUPLICATE")
            self.assertTrue(row["label"]["equivalent"])
            self.assertTrue(row["label"]["depth_ok"])
            self.assertEqual(row["label"]["and_reduction"],0)
            self.assertFalse(row["eligible"])

    def test_z3_comparison_rejects_reordered_declared_names(self):
        self.assertIs(verify_z3(stub(),stub(("b","a")),2.0),False)
