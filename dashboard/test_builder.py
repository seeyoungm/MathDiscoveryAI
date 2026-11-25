"""Unit-level validation of independent receipt gates and display projections."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import build


def write(path, payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(payload))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DashboardBuilder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.synthesis = self.root/"milestone_synthesis"
        self.receipt = self.root/build.LATEST_RECEIPT
        self.menu=list(build.MILESTONE_MENU_OLD)+["added"]

    def fixture(self, *, wrong_gain=False, unexpected_family=False):
        def action(and_count):
            return dict(status="VERIFIED",repeat_stable=True,depth_ok=True,
                        strict_gain=6-and_count,trials=[dict(output_metrics=dict(depth=1,and_count=and_count))]*2)
        def group(family):
            actions = {a:action(5) for a in self.menu[:-1]}
            actions["added"]=action(2 if family=="A" else 5)
            return dict(group_id=family,family="C" if unexpected_family else family,
                        partition="eval",source=dict(depth=2,and_count=6),actions=actions)
        group_hash=write(self.synthesis/"group_metrics.json",{"groups":[group("A"),group("B")]})
        summary=dict(state="VERIFIED_DEVELOPMENT_ONLY",model_calls=0,ranker_fit=False,
                     holdout_accessed=False,runtime_claim=False,menu=self.menu,group_metrics_sha256=group_hash,
                     counts={"groups":2,"trials":20,"statuses":{"VERIFIED":20}},
                     eval_totals=dict(groups=2,prior_menu_oracle_gain=2,oracle_gain=5+wrong_gain,groups_new_oracle_beats_old=1),
                     eval_by_family={
                         "A":dict(groups=1,prior_menu_oracle_gain=1,oracle_gain=4,groups_new_oracle_beats_old=1),
                         "B":dict(groups=1,prior_menu_oracle_gain=1,oracle_gain=1,groups_new_oracle_beats_old=0),
                     })
        summary_hash=write(self.synthesis/"summary.json",summary)
        return dict(status="PASS",summary_sha256=summary_hash,group_metrics_sha256=group_hash)

    def test_missing_and_unverified_never_display(self):
        review = self.fixture()
        self.assertIsNone(build.milestone_data(self.root))
        write(self.receipt,{"schema":1,"synthesis":{**review,"status":"BLOCKED"}})
        self.assertIsNone(build.milestone_data(self.root))

    def test_verified_recomputes_families_and_tamper_fails_closed(self):
        write(self.receipt,{"schema":1,"synthesis":self.fixture()})
        s=build.milestone_data(self.root)["synthesis"]
        self.assertEqual((s["oldOracle"],s["newOracle"],s["better"],s["trials"]),(2,5,1,20))
        self.assertEqual([r["newOracle"] for r in s["families"]],[4,1])
        with (self.synthesis/"summary.json").open("a") as handle: handle.write(" ")
        with self.assertRaisesRegex(ValueError,"digest mismatch"):
            build.milestone_data(self.root)

    def test_consistent_hash_does_not_bypass_semantic_validation(self):
        write(self.receipt,{"schema":1,"synthesis":self.fixture(wrong_gain=True)})
        with self.assertRaisesRegex(ValueError,"oracle_gain"):
            build.milestone_data(self.root)
        write(self.receipt,{"schema":1,"synthesis":self.fixture(unexpected_family=True)})
        with self.assertRaisesRegex(ValueError,"development scope"):
            build.milestone_data(self.root)

    def rank_fixture(self, *, live=False):
        fixtures = ["A0","A1","B0","B1"]
        summary = dict(schema="mdai-ab-ordered-pilot-repair-result-v1",state="OFFLINE_CONTRACT_VERIFIED",
            model_calls=0,ranker_fit=False,holdout_accessed=False,formal_gate3_pass=False,
            real_live_model_comparison=live,actual_cost_or_latency_claimed=False,
            historical_b0_b1_action="neutral_double_not",historical_bound_fixtures=fixtures,
            fixtures=4,no_model_runs=12,offline_synthetic_runs=12,attempt_statuses={"DUPLICATE":24},
            trials=[dict(key=key,repeat=r,arm=arm,verified_statuses={"DUPLICATE":1},attempts=1)
                    for key in fixtures for r in range(3) for arm in ("no-model","offline-synthetic")])
        return dict(status="PASS",summary_sha256=write(self.root/"pilot_contract_repair"/"summary.json",summary))

    def test_rank_can_be_reviewed_without_synthesis(self):
        write(self.receipt,{"schema":1,"rankpilot":self.rank_fixture()})
        values=build.milestone_data(self.root)
        self.assertEqual(set(values),{"rankpilot"})
        self.assertEqual(values["rankpilot"]["rows"][3][1],24)
        write(self.receipt,{"schema":1,"rankpilot":self.rank_fixture(live=True)})
        with self.assertRaisesRegex(ValueError,"real_live_model_comparison"):
            build.milestone_data(self.root)


    def test_subprocess_policy_separate_receipt_and_only_canonical_commit(self):
        cases = {"canonical":"COMMITTED_LOCAL_FIXTURE","sleeping":"DEADLINE_EXCEEDED",
                 "malformed":"INVALID_RESPONSE","oversized_response":"RESPONSE_TOO_LARGE",
                 "oversized_request":"REQUEST_TOO_LARGE"}
        summary = dict(schema="mdai-pilot-local-transport-v1",state="LOCAL_SUBPROCESS_CONTRACT_VERIFIED",
                       model_calls=0,formal_gate3_pass=False,
                       cases={n:dict(status=s,committed=n=="canonical",killed=n=="sleeping",
                                     spawned=n!="oversized_request",reaped=n!="oversized_request")
                              for n,s in cases.items()})
        source = self.root/build.SUBPROCESS_SUMMARY
        review = self.root/build.SUBPROCESS_RECEIPT

        def publish():
            hash_ = write(source,summary)
            write(review,{"schema":1,"subprocess_contract":dict(status="PASS",
                         summary_path=str(build.SUBPROCESS_SUMMARY),summary_sha256=hash_)})
        write(source,summary)
        self.assertIsNone(build.milestone_data(self.root))
        publish()
        result=build.milestone_data(self.root)["subprocess_contract"]
        self.assertEqual([row[1] for row in result["rows"][:2]],[5,1])
        summary["cases"]["sleeping"]["reaped"]=False
        publish()
        with self.assertRaisesRegex(ValueError,"sleeping reap"):
            build.milestone_data(self.root)
        summary["cases"]["sleeping"]["reaped"]=True
        summary["cases"]["malformed"]["committed"]=True
        publish()
        with self.assertRaisesRegex(ValueError,"malformed commit"):
            build.milestone_data(self.root)


if __name__ == "__main__":
    unittest.main()
