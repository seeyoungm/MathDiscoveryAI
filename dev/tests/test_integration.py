from pathlib import Path
import subprocess
import unittest
from mdai.aig import AIG
from mdai.integration import prove_word_to_aig

class CrosscheckTests(unittest.TestCase):
    def test_output_uses_serialized_ids_when_dead_gates_are_removed(self):
        z3=str(Path(__file__).resolve().parents[1]/"tools"/"z3_lib_cli.py")
        try:
            subprocess.run([z3,"-version"],check=True,capture_output=True,timeout=5)
        except (OSError,subprocess.SubprocessError):
            self.skipTest("official Z3 library bridge unavailable in this environment")
        g=AIG(("x","y"))
        x,y=g.input_literals
        a=g.and_(x,y)
        g.and_(x^1,y^1)  # dead and serialized away
        b=g.and_(x^1,a^1)
        g.outputs=(b,)
        prove_word_to_aig({"op":"not","width":1,"args":[{"op":"input","name":"x","width":1}]},{"x":1,"y":1},g,z3)
