from pathlib import Path
import sys
import tempfile
import unittest

from mdai.aig import AIG
from mdai.abc_runner import decode_binary, encode_binary, run_abc


def fixture():
    graph = AIG(["a", "b"])
    a, b = graph.input_literals
    graph.outputs = (graph.and_(a ^ 1, b), a ^ 1)
    return graph


def fake_tool(directory: Path, body: str) -> Path:
    script = directory / "abc-fixture"
    script.write_text("#!" + sys.executable + "\n"
                      "from pathlib import Path\n"
                      "import sys, time\n"
                      "p=Path('input.aig'); q=Path('output.aig')\n"
                      "assert sys.argv[1:3]==['-s','-c']\n"
                      + body + "\n", encoding="utf-8")
    script.chmod(0o700)
    return script


class ABCAdapterTests(unittest.TestCase):
    def test_binary_roundtrip(self):
        for graph in (fixture(), AIG(["x"])):
            if not graph.outputs:
                graph.outputs = (0, 1, graph.input_literals[0] ^ 1)
            decoded = decode_binary(encode_binary(graph), require_labels=True)
            self.assertEqual(graph.metrics(), decoded.metrics())
            for a,b in ((0,0),(0,1),(1,0),(1,1)):
                bits = (a,b)[:len(graph.input_literals)]
                self.assertEqual(graph.evaluate(bits), decoded.evaluate(bits))

    def test_roundtrip_large_delta(self):
        graph = AIG([f"x{i}" for i in range(140)])
        graph.outputs = (graph.and_(2, 4),)
        other = decode_binary(encode_binary(graph), require_labels=True)
        self.assertEqual(graph.metrics(),other.metrics())
        self.assertEqual(other.evaluate([1]*140),(1,))

    def test_reject_malformed(self):
        for value in (b"", b"aig 1 1 0 1 0\n9\n", b"aig 1 0 1 1 0\n0\n",
                      b"aig 1 1 0 1 0 1\n0\n", b"aig 1 0 0 1 1\n2\n\x00\x01",
                      b"aig 1 0 0 1 1\n2\n\x81\x00\x00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_binary(value)
        with self.assertRaisesRegex(ValueError,"ordered"):
            decode_binary(encode_binary(fixture(), include_labels=False), require_labels=True)

    def test_record_evidence_and_refuse_overwrite(self):
        graph = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = fake_tool(root, "q.write_bytes(p.read_bytes()); print('fixture ABC')")
            evidence = root/"audit"
            result = run_abc(graph, abc_path=tool, evidence_dir=evidence)
            self.assertEqual(result.status, "VERIFIED")
            self.assertTrue(result.eligible)
            self.assertEqual(result.output_metrics, graph.metrics())
            self.assertEqual(result.graph.input_names, graph.input_names)
            self.assertEqual(set(p.name for p in evidence.iterdir()),
                             {"input.aig","output.aig","receipt.json","stderr.txt","stdout.txt"})
            with self.assertRaises(FileExistsError):
                run_abc(graph, abc_path=tool, evidence_dir=evidence)

    def test_reject_reordered_port_label_and_wrong_function(self):
        graph = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = fake_tool(root, "q.write_bytes(p.read_bytes().replace(b'MDI0',b'MDI2'))")
            self.assertEqual(run_abc(graph,abc_path=tool).status, "INVALID_OUTPUT")
            tool = fake_tool(root, "h,a,b,rest=p.read_bytes().split(b'\\n',3); q.write_bytes(b'\\n'.join((h,b,a,rest)))")
            result = run_abc(graph,abc_path=tool)
            self.assertEqual(result.status, "NOT_EQUIVALENT")
            self.assertIsNone(result.graph)
            self.assertFalse(result.eligible)

    def test_timeout_error_and_no_unknown_admission(self):
        graph = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = fake_tool(root, "time.sleep(2)")
            self.assertEqual(run_abc(graph,abc_path=tool,timeout_s=.1).status,"TIMEOUT")
            tool = fake_tool(root, "q.write_bytes(p.read_bytes()); print('Error: unexpected')")
            self.assertEqual(run_abc(graph,abc_path=tool).status,"TOOL_ERROR")
            tool = fake_tool(root, "q.write_bytes(p.read_bytes())")
            result = run_abc(graph,abc_path=tool,verify=lambda *_: None)
            self.assertEqual(result.status,"UNVERIFIED")
            self.assertIsNone(result.graph)

    def test_recipes_are_closed(self):
        with self.assertRaises(ValueError):
            run_abc(fixture(), abc_path="/unused",recipe="strash; shell")
        with self.assertRaises(ValueError):
            run_abc(fixture(), abc_path="abc")


if __name__ == "__main__":
    unittest.main()
