from contextlib import redirect_stdout
from io import StringIO
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from mdai import w4expand as exp


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.source = exp.ROOT.parent/"records"/"week2"/"week2_smoke"/"dataset_manifest.jsonl"
        if not self.source.exists():
            self.skipTest("source fixture not mounted in this unit-test wrapper")

    def test_pins_and_wrong_digest(self):
        spec, rows = exp.read_cohort(self.source)
        self.assertEqual([r["task_id"] for r in rows], spec["task_ids"])
        self.assertEqual(len({r["canonical_id"] for r in rows}),11)
        with tempfile.TemporaryDirectory() as t:
            edited = Path(t)/"bad.jsonl"
            edited.write_bytes(self.source.read_bytes()+b"\n")
            with self.assertRaisesRegex(ValueError,"digest"):
                exp.read_cohort(edited)

    def test_proof_labels(self):
        with tempfile.TemporaryDirectory() as t:
            work = Path(t)
            mock_abc = work/"abc"
            mock_abc.write_bytes(b"fixture")
            _, rows = exp.read_cohort(self.source)
            frozen = ({"source_manifest_sha256": exp.pin(self.source)}, rows[:1])
            def match(base, **kw):
                folder=kw["evidence_dir"]
                folder.mkdir()
                attempts=[]
                for j,action in enumerate(exp.ACTIONS,1):
                    action_folder=folder/f"abc-{j:02d}"
                    action_folder.mkdir()
                    result={"status":"VERIFIED","equivalent":j!=3,"recipe":action,
                            "input_metrics":vars(base.metrics()),"output_metrics":vars(base.metrics())}
                    for name in ("input.aig","output.aig"):
                        (action_folder/name).write_text("fixture")
                    exp.put(action_folder/"receipt.json",result)
                    attempts.append({"action":action,"status":"DUPLICATE"})
                obj={"attempts":attempts}
                exp.put(folder/"receipt.json",obj)
                return obj
            with patch.object(exp,"DEST",work/"exp"), patch.object(exp,"read_cohort",return_value=frozen), \
                 patch.object(exp,"run_match",side_effect=match), \
                 patch.object(exp.subprocess,"run",return_value=subprocess.CompletedProcess([],0, exp.REFERENCE_ABC["expected_version_line"],"")), \
                 patch.object(exp.subprocess,"check_output",return_value="Z3 4.13.3.0"):
                with redirect_stdout(StringIO()):
                    self.assertFalse(exp.run(self.source,mock_abc,exp.pin(mock_abc)))
                files=work/"exp"
                label_rows=[json.loads(s) for s in (files/"abc_action_observations.jsonl").read_text().splitlines()]
                self.assertEqual(len(label_rows),9)
                for row_idx,row in enumerate(label_rows):
                    with self.subTest(row=row_idx):
                        self.assertEqual(row["outcome"]["equivalent"],row_idx % 3 != 2)
                        self.assertEqual(row["outcome"]["and_reduction"],None if row_idx%3==2 else 0)
                summary=json.loads((files/"summary.json").read_text())
                self.assertEqual(summary["statuses"],{"VERIFIED":6,"PROOF_INCONSISTENT":3})
                self.assertEqual(summary["terminal_status"],"PARTIAL_FAIL_CLOSED")
                index=json.loads((files/"receipt_index.json").read_text())
                for r in index["rows"]:
                    self.assertEqual(exp.pin(files/r["receipt_path"]),r["receipt_sha256"])
                with self.assertRaises(FileExistsError):
                    exp.run(self.source,mock_abc,exp.pin(mock_abc))

if __name__=="__main__":
    unittest.main()
