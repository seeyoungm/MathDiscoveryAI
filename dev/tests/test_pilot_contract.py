"""Boundary cases for ordered prompt index binding and callback accounting."""
import json
from pathlib import Path
from time import sleep
import unittest

from mdai.aig import verify_aig_exact
from mdai.candidates import candidate_menu, seed_graph
from mdai.pilot_contract import (bind_response, build_contract_candidate, captured_contract,
                                 indexed_request, local_provider, timed_trial)

ROOT = Path(__file__).resolve().parents[1]
CAP = ROOT/"evidence/week3/model_pilot"


class OrderedPilot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        req = (CAP/"request.txt").read_bytes()
        manifest=(ROOT.parent/"records/week2/week2_smoke/dataset_manifest.jsonl").read_bytes()
        cls.req,cls.manifest=req,manifest
        cls.contract=captured_contract(req,manifest)
        cls.menus={k:m for k,(_,m) in cls.contract.items()}
        cls.captured=(CAP/"response.txt").read_bytes()

    def test_hash_boundary_rejects_even_a_whitespace_change(self):
        for req, manifest in ((self.req+b" ",self.manifest),(self.req,self.manifest+b"\n")):
            with self.assertRaisesRegex(ValueError,"unrecognized"):
                captured_contract(req,manifest)

    def test_a_and_b_historical_exact_index_semantics(self):
        selected=bind_response(self.captured,self.menus,tail=False)
        self.assertEqual([a.parameter for a in selected["A0"]],[(1,3,0,2),(1,3,2,0),(3,1,0,2)])
        for k in ("B0","B1"):
            row,menu=self.contract[k]
            self.assertGreater(len(candidate_menu(row)),1)
            self.assertNotEqual(menu[0],candidate_menu(row)[0])
            with self.assertRaisesRegex(ValueError,"outside"):
                build_contract_candidate(row,menu,candidate_menu(row)[0])
            a=selected[k][0]
            self.assertEqual(a.edit_kind,"neutral_double_not")
            g=seed_graph(row)
            c=build_contract_candidate(row,menu,a)
            self.assertEqual(verify_aig_exact(g,c).status,"EQUIVALENT")
            self.assertEqual(g.to_aag(),c.to_aag())

    def test_one_bad_case_rejects_the_whole_response(self):
        original=json.loads(self.captured)
        for field, incorrect in (("B0",[1]),("A0",[0,0]),("A0",[True]),("B1",[0.0]),("B1",[]),
                                 ("A0",[0,1,2,3]),("A0",[-1]),("A0",[20]),("UNKNOWN",[0])):
            with self.subTest(field=field,value=incorrect),self.assertRaises(ValueError):
                bind_response(json.dumps({**original,field:incorrect}).encode(),self.menus)
        for wrong in (self.captured+b" or here",self.captured.replace(b'"B0":[0]',b'"B0":[0],"B0":[0]'),
                      self.captured.replace(b'"B0":[0]',b'"B0":[NaN]'), b"\xff", b"x"*8193):
            with self.assertRaises(ValueError):
                bind_response(wrong,self.menus)

    def test_rank_prefix_and_tail_are_exactly_the_printed_menu(self):
        row,menu=self.contract["A0"]
        prompt=indexed_request("A0",row,menu)
        response=local_provider(prompt,1)
        printed=json.loads(prompt)["ordered_menu"]
        applied=bind_response(response,{"A0":menu})["A0"]
        expected=[2,1,0,*range(3,20)]
        self.assertEqual([a.candidate_id for a in applied],[printed[i]["candidate_id"] for i in expected])
        for arm in ("live","baseline"):
            first,ctx=timed_trial("A0",row,menu,0,arm,None)
            second,_=timed_trial("A0",row,menu,0,arm,None)
            with self.subTest(arm=arm):
                self.assertEqual(first["outcome_sha256"],second["outcome_sha256"])
                ids=[a["id"] for a in first["attempts"]]
                self.assertEqual(set(ids),{p["candidate_id"] for p in printed})
                self.assertLess(first["elapsed_s"],60)
                if arm=="live":
                    self.assertEqual(ids,ctx["bound_action_ids"])
                    self.assertGreater(first["proposal_callback_elapsed_s"],0)
                else:
                    self.assertEqual(first["proposal_callback_elapsed_s"],0)

    def test_reject_invalid_or_late_provider_without_candidate_execution(self):
        row,menu=self.contract["B0"]
        def late(raw,remaining):
            sleep(remaining+0.005)
            return b'{"B0":[0]}'
        def invalid(raw,remaining):
            return b'{"B0":[1]}'
        for provider,budget in ((late,0.03),(invalid,1)):
            with self.subTest(provider=provider.__name__):
                r,ctx=timed_trial("B0",row,menu,0,"live",None,provider,timeout_s=budget)
                self.assertEqual(r["attempts"],[])
                self.assertEqual(r["terminal_status"],"PROPOSAL_SOURCE_ERROR")
                self.assertNotIn("bound_action_ids",ctx)
                self.assertGreater(r["proposal_callback_elapsed_s"],0)


if __name__=="__main__":
    unittest.main()
