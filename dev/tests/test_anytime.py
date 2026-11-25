import copy
import json
from pathlib import Path
import unittest
from mdai.anytime import (BUDGET,ATTEMPT,ARMS,fixtures,menu_order,run_arm,replay,summarize,disposition)
from mdai.candidates import candidate_menu,seed_graph

class AnytimeTest(unittest.TestCase):
    def test_target_time_and_late_rejection(self):
        def event(ms,n,status):
            return dict(end_ns=ms*1_000_000,metrics={"and_count":n},status=status)
        ev=[event(100,1,"depth_rejected"),event(210,7,"feasible"),
            event(240,3,"unverified"),event(800,5,"feasible"),event(801,0,"after_deadline")]
        self.assertEqual(summarize(8,ev,200*1_000_000),dict(best=8,target_ns=None,points=[[0,8]]))
        self.assertEqual(summarize(8,ev),dict(best=5,target_ns=210_000_000,points=[[0,8],[210_000_000,7],[BUDGET,5]]))
        self.assertEqual(ev[-1]["metrics"]["and_count"],0)

    def test_fail_closed_at_both_deadlines(self):
        origin=dict(and_count=9,depth=3)
        good=dict(and_count=8,depth=2)
        self.assertEqual(disposition(origin,good,"EQUIVALENT",True,BUDGET,ATTEMPT),"feasible")
        self.assertEqual(disposition(origin,good,"EQUIVALENT",True,BUDGET+1,ATTEMPT),"after_deadline")
        self.assertEqual(disposition(origin,good,"EQUIVALENT",True,BUDGET,ATTEMPT+1),"proof_timeout")
        self.assertEqual(disposition(origin,good,"EQUIVALENT",None,BUDGET,ATTEMPT),"unverified")
        self.assertEqual(disposition(origin,{"depth":4},"EQUIVALENT",True,BUDGET,ATTEMPT),"depth_rejected")

    def test_late_smt_return_cannot_improve(self):
        case=fixtures()[0]
        fake_clock=iter([0,1,BUDGET-6,BUDGET-5,BUDGET-4,BUDGET+1,BUDGET+2,BUDGET+3])
        run=run_arm(case,seed_graph(case),ARMS[0],0,clock=lambda:next(fake_clock),check_smt=lambda *a:True)
        self.assertEqual([e["status"] for e in run["events"]],["after_deadline"])
        self.assertIsNone(summarize(99,run["events"])["target_ns"])

    def test_same_candidate_universe(self):
        reordered=0
        for case in fixtures():
            menu=candidate_menu(case)
            a,b=(menu_order(menu,arm) for arm in ARMS)
            self.assertEqual(set(a),set(b))
            reordered += a != b
        self.assertGreaterEqual(reordered,3)

    def test_replay_rejects_tampering(self):
        path=Path(__file__).resolve().parents[1]/"evidence/anytime/trace.json"
        if not path.exists(): self.skipTest("run the local rehearsal first")
        trace=json.loads(path.read_text())
        self.assertEqual(len(replay(trace)["cases"]),6)
        e=trace["cases"][0]["runs"][0]["events"][0]
        for key,bad,error in (("end_ns",-1,"clock"),("status","tampered","disposition"),("fingerprint","bad","candidate")):
            with self.subTest(key=key):
                tmp=copy.deepcopy(trace)
                tmp["cases"][0]["runs"][0]["events"][0][key]=bad
                self.assertNotEqual(e[key],bad)
                with self.assertRaisesRegex(ValueError,error):
                    replay(tmp)

if __name__=="__main__":
    unittest.main()
