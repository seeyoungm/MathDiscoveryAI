import unittest
from dataclasses import fields, replace

from mdai.candidates import (Candidate, build_candidate, candidate_menu,
                             ordered_menu, seed_graph, verify_candidate)
from mdai.devgen import generate_development


class CandidateTests(unittest.TestCase):
    def test_exact_for_generated_records_and_limit(self):
        for task in generate_development(8, 12):
            with self.subTest(task=task["task_id"]):
                menu = candidate_menu(task)
                self.assertTrue(0 < len(menu) <= 20)
                self.assertEqual(menu, candidate_menu(task))
                self.assertEqual(len(set(x.candidate_id for x in menu)), len(menu))
                initial = seed_graph(task)
                for proposal in menu:
                    self.assertEqual(verify_candidate(initial, build_candidate(task, proposal)).status, "EQUIVALENT")

    def test_random_and_canonical_have_same_universe(self):
        task = generate_development(1,0)[0]
        canonical = ordered_menu(task, limit=7)
        random1 = ordered_menu(task, order="random", limit=7, seed=1)
        self.assertEqual(set(canonical), set(random1))
        self.assertEqual(random1, ordered_menu(task, order="random", limit=7, seed=1))
        self.assertNotEqual(random1, canonical)
        self.assertEqual(candidate_menu(task, limit=0), ())
        self.assertEqual([f.name for f in fields(Candidate)], ["candidate_id", "edit_kind", "path", "parameter"])

    def test_reject_holdout_forged_candidate_and_oversize(self):
        task = generate_development(1,0)[0]
        valid = candidate_menu(task)[0]
        for invalid in ({**task, "family":"C"}, {**task, "split":"week_5"}, {**task, "total_input_bits":6}):
            with self.assertRaises(ValueError):
                candidate_menu(invalid)
        with self.assertRaises(ValueError):
            candidate_menu(task, limit=21)
        with self.assertRaises(ValueError):
            build_candidate(task, replace(valid, parameter=(0,0,1,2)))

    def test_b_common_term_factoring_uses_fewer_ands(self):
        def leaf(name):
            return dict(op="input", width=4, name=name)
        def binary(op,left,right):
            return dict(op=op,width=4,args=[left,right])
        x,y,z=map(leaf,"xyz")
        ast=binary("or", binary("and",x,y), binary("and",x,z))
        record={"task_id":"toy", "family":"B", "split":"development", "representation":"bit_ast",
                "inputs":dict(x=4,y=4,z=4), "input_order":list("xyz"), "total_input_bits":12,
                "outputs":[dict(id="out",width=4)], "ast":ast}
        baseline=seed_graph(record)
        chosen=next(q for q in candidate_menu(record) if q.edit_kind.startswith("factor_"))
        graph=build_candidate(record,chosen)
        self.assertEqual(verify_candidate(baseline,graph).status,"EQUIVALENT")
        self.assertLess(graph.metrics().and_count, baseline.metrics().and_count)


if __name__ == "__main__":
    unittest.main()
