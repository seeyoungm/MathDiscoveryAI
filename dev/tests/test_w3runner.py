import tempfile
import time
import unittest
from pathlib import Path

from mdai.aig import AIG
from mdai.w3runner import Proposal, run_match


def source(redundant=False, counter=False):
    g=AIG(("a","b"))
    a,b=g.input_literals
    g.outputs=(a^int(counter),)
    if redundant:
        g.outputs=(g.or_(g.and_(a,b),g.and_(a,b^1)),)
    return g


class HarnessTests(unittest.TestCase):
    def test_ranked_replay_labels_and_duplicate(self):
        options=[Proposal("same",lambda _:source(redundant=True),3),
                 Proposal("overwrite",lambda _:source(counter=True),2),
                 Proposal("simple",lambda _:source(),1),
                 Proposal("again",lambda _:source(),4)]
        identity=dict(source="recorded",model="m",request_sha256="x",response_sha256="y")
        g=source(redundant=True)
        result=run_match(g,task_id="A-TEST",arm="replay",repeat=0,seed=99,proposals=options,
                         replay_identity=identity)
        self.assertEqual([x["status"] for x in result["attempts"]],
                         ["VERIFIED","NOT_EQUIVALENT","DUPLICATE","DUPLICATE"])
        self.assertEqual(result["selected_id"], "simple")
        self.assertTrue(result["excludes_live_generation_cost"])
        self.assertNotIn("equivalent",result["attempts"][0]["pre_action"])
        self.assertTrue(result["attempts"][0]["label"]["equivalent"])
        reverse=run_match(g,task_id="A-TEST",arm="replay",repeat=0,seed=99,proposals=options,
                         replay_identity=identity)
        self.assertEqual(reverse["outcome_sha256"],result["outcome_sha256"])

    def test_repeated_rank_annotations_do_not_affect_seeded_baseline(self):
        opts=[Proposal(str(i),lambda _:source(),i+1) for i in range(8)]
        observed=[]
        for us in (opts,[Proposal(x.id,x.build,42) for x in reversed(opts)]):
            r=run_match(source(redundant=True),task_id="A",arm="baseline",repeat=1,seed=8,proposals=us)
            observed.append([x["id"] for x in r["attempts"]])
            self.assertTrue(all(x["rank"] is None for x in r["attempts"]))
        self.assertEqual(*observed)

    def test_duplicate_id_depth_and_wrong_I_O(self):
        options=[Proposal("deep",lambda _:source(redundant=True),1),
                 Proposal("deep",lambda _:self.fail("duplicate was executed"),2),
                 Proposal("bad",lambda _:AIG(),3)]
        r=run_match(source(),task_id="A",arm="replay",repeat=0,seed=6,proposals=options,
                    replay_identity=dict(source="recorded",model="m",request_sha256="x",response_sha256="y"))
        self.assertEqual([x["status"] for x in r["attempts"]],["DEPTH_CAP","DUPLICATE","INVALID_INTERFACE"])
        self.assertFalse(r["verified_improvement"])

    def test_max_twenty_no_budget_reset(self):
        options=[Proposal(str(i),lambda _:source()) for i in range(30)]
        with tempfile.TemporaryDirectory() as tmp:
            evidence=Path(tmp)/"receipt"
            kw=dict(task_id="A",arm="baseline",repeat=0,seed=1,proposals=options,evidence_dir=evidence)
            a=run_match(source(redundant=True),**kw)
            self.assertEqual(a["terminal_status"],"CANDIDATE_LIMIT")
            self.assertEqual(a["attempted_candidates"],20)
            self.assertEqual(a,run_match(source(redundant=True),**kw))
            (evidence/"receipt.json").unlink()
            stale=run_match(source(redundant=True),**kw)
            self.assertEqual(stale["terminal_status"],"INTERRUPTED_NO_RETRY")
            with self.assertRaisesRegex(ValueError,"resume identity"):
                run_match(source(redundant=True),**dict(kw,seed=2))

    def test_late_never_promoted_and_charged_read(self):
        def late(_):
            time.sleep(.04)
            return source()
        r=run_match(source(redundant=True),task_id="A",arm="baseline",repeat=0,seed=4,
                    proposals=[Proposal("late",late)],timeout_s=.02)
        self.assertEqual(r["attempts"][0]["status"],"TIMEOUT")
        self.assertFalse(r["verified_improvement"])
        r=run_match(source(redundant=True),task_id="A",arm="baseline",repeat=0,seed=4,
                    proposals=[Proposal("late",late)],timeout_s=.01,launch_charge_s=.02)
        self.assertEqual(r["attempted_candidates"],0)

    def test_live_requires_in_budget_source(self):
        with self.assertRaisesRegex(ValueError,"live arm"):
            run_match(source(),task_id="A",arm="live",repeat=0,seed=4)
        r=run_match(source(redundant=True),task_id="A",arm="live",repeat=0,seed=4,
                    proposal_source=lambda budget:[Proposal("small",lambda _:source(),1)])
        self.assertEqual(r["mode"],"live_callback_in_budget")
        self.assertFalse(r["excludes_live_generation_cost"])
        self.assertTrue(r["verified_improvement"])


if __name__ == "__main__":
    unittest.main()
