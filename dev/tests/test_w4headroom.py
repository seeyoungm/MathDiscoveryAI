from unittest import TestCase

from mdai.selector import split_for_group
from mdai.w4headroom import describe
from mdai.w4review import ACTIONS


class HeadroomTests(TestCase):
    def test_train_baseline_and_group_weighted_oracle(self):
        groups = {s: [f"g{i}" for i in range(30) if split_for_group(f"g{i}") == s]
                  for s in ("train","dev")}
        def rows(*fixtures):
            return {(g,a,r): dict(group_id=g,task_id=g,repeat=r,action=a,
                                  outcome=dict(and_reduction=v,equivalent=True,depth_ok=True))
                    for g,r,vs in fixtures for a,v in zip(ACTIONS,vs)}
        train = (groups["train"][0],0,(0,0,4))
        prior = (groups["dev"][0],0,(0,0,4))
        new = (groups["dev"][1],0,(0,2,0))
        out = describe(rows(train,prior),rows(new))
        self.assertEqual(out["baseline_chosen_from_original_train_only"],"rewrite_refactor")
        self.assertEqual(out["partitions"]["combined_dev"]["mean_maximum_top1_gain"],"1")
        self.assertEqual(out["partitions"]["prior_dev"]["trials_oracle_strictly_beats_static"],0)

    def test_incomplete_menu_fails(self):
        row = {"group_id":"g0","task_id":"g0","repeat":0,"action":ACTIONS[0],
               "outcome":{"and_reduction":0,"depth_ok":True,"equivalent":True}}
        with self.assertRaisesRegex(ValueError,"incomplete action"):
            describe({0:row},{})
