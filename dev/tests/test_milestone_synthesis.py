import unittest

from mdai import milestone_synthesis as m
from mdai.abc_runner import NEXTSTAGE_RECIPES, RECIPES
from mdai.aig import AIG
from mdai.milestone_diagnostic import structure_signature


def group(partition, family, gains):
    return {"partition":partition,"family":family,"actions":{
        a:{"strict_gain":v} for a,v in zip(m.MENU,gains)
    }}


class MilestoneSynthesis(unittest.TestCase):
    def test_stop_checks_fixed_policy_nonregression_even_when_oracle_improves(self):
        data = [group("train","A",[0,0,5,0,6]),
                group("eval","A",[0,0,0,0,1]),
                group("eval","B",[0,0,0,0,1]),
                group("eval","B",[0,0,10,0,1])]
        expanded, old, _, ev, _ = m.compare(data)
        self.assertEqual((expanded,old),("lut_mfs","rewrite_refactor"))
        self.assertEqual((ev["groups_new_oracle_beats_old"],ev["oracle_gain"],ev["prior_menu_oracle_gain"]),(2,12,10))
        self.assertFalse(m.positive_predefined_contrast(ev))

    def test_positive_gate_needs_two_groups_even_at_large_gain(self):
        train = group("train","A",[0,0,5,0,5])
        evaluation = [group("eval","A",[0,0,0,0,7]),group("eval","B",[0,0,2,0,2])]
        new, old, _, ev, _ = m.compare([train,*evaluation])
        self.assertEqual((new,old),("rewrite_refactor","rewrite_refactor"))
        self.assertFalse(m.positive_predefined_contrast(ev))
        evaluation[1]["actions"]["lut_mfs"]["strict_gain"] = 3
        self.assertTrue(m.positive_predefined_contrast(m.compare([train,*evaluation])[3]))

    @unittest.skipUnless((m.FREEZE / "cohort.json").is_file(), "historical cohort is not in the public package")
    def test_frozen_menu_changes_are_opt_in(self):
        self.assertNotIn("lut_mfs", RECIPES)
        self.assertNotIn("lut_mfs", NEXTSTAGE_RECIPES)
        self.assertEqual(m.MILESTONE_RECIPES["lut_mfs"],("strash","dch","if -K 4","mfs2","strash","balance"))
        with self.assertRaises(ValueError):
            m.read_inputs("0"*64,"0"*64)

    def test_signature_ignores_unreachable_nodes_but_keeps_ordered_outputs(self):
        a = AIG.from_aag("aag 4 2 0 2 2\n2\n4\n6\n7\n6 4 2\n8 4 3\n")
        b = AIG.from_aag("aag 4 2 0 2 2\n2\n4\n8\n9\n6 4 3\n8 4 2\n")
        c = AIG.from_aag("aag 4 2 0 2 2\n2\n4\n9\n8\n6 4 3\n8 4 2\n")
        self.assertEqual(structure_signature(a),structure_signature(b))
        self.assertNotEqual(structure_signature(a),structure_signature(c))


if __name__ == "__main__":
    unittest.main()
