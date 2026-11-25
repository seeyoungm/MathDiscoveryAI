import unittest

from mdai import nextstage_synthesis as n
from mdai.abc_runner import RECIPES


def group(part, family, gains):
    return {"partition":part,"family":family,"actions":{
        a:{"strict_gain":v} for a,v in zip(n.MENU,gains)
    }}


class Nextstage(unittest.TestCase):
    def test_eval_cannot_select_policy_and_oracle_and_prior_are_distinct(self):
        data = [
            group("train","A",[1,0,2,2]),
            group("train","B",[0,0,2,2]),
            group("eval","A",[0,4,1,8]),
            group("eval","B",[5,0,2,0]),
        ]
        chosen, _, ev, families = n.compare(data)
        self.assertEqual(chosen,"rewrite_refactor")
        self.assertEqual((ev["prior_fixed_gain"],ev["train_selected_gain"],
                          ev["prior_menu_oracle_gain"],ev["oracle_gain"]),(3,3,9,13))
        self.assertEqual(families["B"]["oracle_gain"],5)
        with self.assertRaises(ValueError):
            n.train_selection(data)

    def test_new_recipe_not_in_default_menu(self):
        self.assertNotIn("resub_balance", RECIPES)
        self.assertEqual(n.NEXTSTAGE_RECIPES["resub_balance"][2],"resub -K 10 -N 1")

    @unittest.skipUnless((n.FREEZE / "cohort.json").is_file(), "historical cohort is not in the public package")
    def test_wrong_cohort_pin_rejected_at_entry(self):
        with self.assertRaises(ValueError):
            n.read_inputs("0"*64,"0"*64)


if __name__ == "__main__":
    unittest.main()
