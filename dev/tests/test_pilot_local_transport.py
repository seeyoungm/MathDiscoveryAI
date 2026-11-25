"""Exercise the actual sealed fixture through the bwrap/seccomp test entrypoint."""
import json
import os
from pathlib import Path
import unittest

from mdai.pilot_contract import captured_contract, indexed_request, json_bytes
from mdai.pilot_local_transport import (REQUEST_LIMIT, RESPONSE_LIMIT, require_socket_denial,
                                         run_local_transport)

ROOT=Path(__file__).resolve().parents[1]


class LocalTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            require_socket_denial()
        except RuntimeError as exc:
            raise unittest.SkipTest("requires tools/pilot_local_transport_sandbox.sh --test") from exc
        req=(ROOT/"evidence/week3/model_pilot/request.txt").read_bytes()
        manifest=(ROOT.parent/"records/week2/week2_smoke/dataset_manifest.jsonl").read_bytes()
        row,menu=captured_contract(req,manifest)["A0"]
        cls.prompt=json.loads(indexed_request("A0",row,menu))
        cls.menus={"A0":menu}

    def launch(self,case,deadline=2.0):
        return run_local_transport(json_bytes(dict(case=case,prompt=self.prompt)),self.menus,deadline_s=deadline)

    def reaped(self,receipt):
        self.assertTrue(receipt["spawned"])
        self.assertTrue(receipt["reaped"])
        with self.assertRaises(ChildProcessError):
            os.waitpid(receipt["child_pid"],os.WNOHANG)

    def test_canonical_pinned_child(self):
        result,r=self.launch("canonical")
        self.assertEqual(result,b'{"A0":[2,1,0]}')
        self.assertEqual(r["status"],"COMMITTED_LOCAL_FIXTURE")
        self.assertTrue(r["committed"])
        self.reaped(r)
        self.assertLess(r["elapsed_s"],r["deadline_s"])

    def test_real_sleeping_child_preempted_without_partial_commit(self):
        result,r=self.launch("sleeping",0.35)
        self.assertIsNone(result)
        self.assertEqual(r["status"],"DEADLINE_EXCEEDED")
        self.assertEqual(r["stdout_bytes"],6)
        self.assertEqual(r["returncode"],-9)
        self.assertTrue(r["killed"])
        self.assertLess(r["elapsed_s"],2.0)
        self.assertFalse(r["committed"])
        self.reaped(r)

    def test_duplicate_key_payload_and_nonzero_exit_not_committed(self):
        for case,status in (("malformed","INVALID_RESPONSE"),("nonzero","NONZERO_EXIT")):
            with self.subTest(case=case):
                result,r=self.launch(case)
                self.assertIsNone(result)
                self.assertFalse(r["committed"])
                self.assertEqual(r["status"],status)
                self.reaped(r)

    def test_limits_reject_large_input_before_spawn_and_kill_large_output(self):
        result,r=run_local_transport(b"x"*(REQUEST_LIMIT+1),self.menus,deadline_s=2)
        self.assertIsNone(result)
        self.assertEqual(r["status"],"REQUEST_TOO_LARGE")
        self.assertFalse(r["spawned"])
        result,r=self.launch("oversized_response")
        self.assertIsNone(result)
        self.assertEqual(r["status"],"RESPONSE_TOO_LARGE")
        self.assertEqual(r["stdout_bytes"],RESPONSE_LIMIT+1)
        self.assertTrue(r["killed"])
        self.reaped(r)


if __name__=="__main__":
    unittest.main()
