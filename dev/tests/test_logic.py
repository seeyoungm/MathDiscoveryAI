import itertools
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from mdai.logic import (InvalidExpression, evaluate, parse_expression, validate,
                        verify, verify_truth_table)


def inp(name="x", width=4):
    return {"op":"input","width":width,"name":name}


def c(value, width=4):
    return {"op":"const","width":width,"value":value}


def op(operation, width, *args, **attrs):
    return {"op":operation,"width":width,"args":list(args),**attrs}


class GrammarTests(unittest.TestCase):
    def test_parse(self):
        ast=op("xor",4,inp(),c(3))
        self.assertEqual(parse_expression(json.dumps(ast).encode(),{"x":4}),ast)
        self.assertEqual(evaluate(ast,{"x":7}),4)
        self.assertEqual(validate({"op":"input","name":"x","width":4,"args":[]},{"x":4}),4)

    def test_duplicate_and_malformed_json(self):
        for s in (b'{"op":"hey","op":"const","width":1,"value":0}', b'{"op":"const","width":1,"value":NaN}', b'{\xff', b'['*1000, b' ' * 262145):
            with self.subTest(s=s[:40]), self.assertRaises(InvalidExpression): parse_expression(s, {})

    def test_invalid_fails_closed(self):
        malformed=[inp("missing"),inp("x",3), c(True),c(16),c(-1),op("xor",4,inp()),op("nand",4,inp(),inp()),
                   op("not",4,inp(),amount=0),op("lshr",4,inp(),amount=-1),
                   op("lshr",4,inp(),amount=True),op("lshr",4,inp(),amount=1<<31),
                   op("extract",2,inp(),hi=4,lo=2),op("extract",2,inp(),hi=2,lo=0),
                   op("concat",4,inp(),inp()),{"op":"input","width":4,"name":"x","args":False},
                   {"op":[],"width":4,"args":[]}, {"op":"const","value":0,"width":True}]
        for node in malformed:
            with self.subTest(node=node):
                self.assertEqual(verify(inp(),node,{"x":4}).status,"INVALID")
        with self.assertRaises(InvalidExpression): evaluate(inp(),{"x":16})
        with self.assertRaises(InvalidExpression): evaluate(inp(),{"x":True})
        with self.assertRaises(InvalidExpression): evaluate(inp(),{"x":1,"extra":7})

    def test_complexity_and_cycle(self):
        node=inp()
        for _ in range(65): node=op("not",4,node)
        self.assertEqual(verify(inp(),node,{"x":4}).status,"INVALID")
        node=op("not",4,inp()); node["args"][0]=node
        self.assertEqual(verify(inp(),node,{"x":4}).status,"INVALID")

    def test_unsupported_width_or_order(self):
        self.assertEqual(verify(inp(),inp(),{"x":4,"#":1}).status,"INVALID")
        self.assertEqual(verify(inp(),inp(),{"x":4},max_exhaustive_bits=17).status,"INVALID")
        self.assertEqual(verify(inp(),c(1,1),{"x":4}).status,"INVALID")
        for timeout in (float("nan"), float("inf"), True, 0):
            self.assertEqual(verify(inp(),inp(),{"x":4},timeout_s=timeout).status,"INVALID")


class EvaluatorTests(unittest.TestCase):
    def test_bitwise(self):
        for x,y in itertools.product(range(16),repeat=2):
            a,b=inp(),inp("y")
            for name,expected in (("and",x&y),("or",x|y),("xor",x^y)):
                self.assertEqual(evaluate(op(name,4,a,b),{"x":x,"y":y}),expected)
            self.assertEqual(evaluate(op("not",4,a),{"x":x}),x^15)

    def test_fixed_bit_operations(self):
        a=inp()
        for value in range(16):
            for amount in (0,1,3,4,5,7,8,32,1<<30):
                args={"x":value}
                got=[evaluate(op(n,4,a,amount=amount),args) for n in ("shl","lshr","rotl","rotr")]
                bits=f"{value:04b}"
                k=amount%4
                expected=[(value<<amount)&15 if amount<4 else 0,
                    value>>amount,int(bits[k:]+bits[:k],2),int(bits[-k:]+bits[:-k],2)]
                self.assertEqual(got,expected)
            self.assertEqual(evaluate(op("extract",2,a,lo=1,hi=2),{"x":value}),(value>>1)&3)
            self.assertEqual(evaluate(op("concat",8,a,c(6),),{"x":value}),(value<<4)|6)
        self.assertEqual(evaluate(op("concat",9,a,c(6),c(1,1)),{"x":10}),333)

    def test_exact_equivalence_witness_and_timeout(self):
        a=inp()
        ref=op("not",4,op("not",4,a))
        self.assertEqual(verify(ref,a,{"x":4}).status,"EQUIVALENT")
        got=verify(ref,c(0),{"x":4})
        self.assertEqual(got.status,"COUNTEREXAMPLE")
        self.assertNotEqual(evaluate(ref,got.counterexample,{"x":4}),evaluate(c(0),got.counterexample,{"x":4}))
        self.assertEqual(verify(ref,a,{"x":4},timeout_s=1e-12).status,"TIMEOUT")
        self.assertEqual(verify(ref,a,{"x":4},max_exhaustive_bits=3).status,"UNKNOWN")

    def test_table_msb_order_and_bad_inputs(self):
        a,b=inp("a",1),inp("b",1)
        self.assertEqual(verify_truth_table(0b1100,a,["a","b"]).status,"EQUIVALENT")
        got=verify_truth_table(0b1100,b,["a","b"])
        self.assertEqual((got.status,got.counterexample),("COUNTEREXAMPLE",{"a":0,"b":1}))
        self.assertEqual(verify_truth_table(0b1000,op("and",1,a,b),["a","b"]).status,"EQUIVALENT")
        self.assertEqual(verify_truth_table(16,b,["a","b"]).status,"INVALID")
        self.assertEqual(verify_truth_table(1,b,["b","b"]).status,"INVALID")
        self.assertEqual(verify_truth_table(1,c(1,1),[]).status,"EQUIVALENT")


class SolverProtocolTests(unittest.TestCase):
    @staticmethod
    def complete(stdout, stderr="", returncode=0):
        return subprocess.CompletedProcess([],returncode,stdout,stderr)

    def solver_case(self, responses):
        with patch("mdai.logic.subprocess.run", side_effect=responses) as sub:
            value=verify(inp(),c(0),{"x":4},max_exhaustive_bits=0,z3_path=sys.executable)
            return value, sub.call_args_list

    def test_sat_model_replayed_and_no_get_before_sat(self):
        for value in ("#x1","#b0001","(_ bv1 4)"):
            got,calls=self.solver_case([self.complete("sat\n"), self.complete("sat\n((v0 "+value+"))\n")])
            self.assertEqual((got.status,got.counterexample),("COUNTEREXAMPLE",{"x":1}))
            self.assertNotIn("get-value",calls[0].kwargs["input"])
            self.assertIn("(get-value (v0))",calls[1].kwargs["input"])
            self.assertIn("(assert (distinct r0 c0))",calls[0].kwargs["input"])

    def test_unknown_timeout_error_and_invalid_witness(self):
        cases=[([self.complete("unknown\n")],"UNKNOWN"),
               ([subprocess.TimeoutExpired("z3",1)],"TIMEOUT"),
               ([self.complete("unsat\n(error \"oops\")\n")],"ERROR"),
               ([self.complete("unsat\n","warning")],"ERROR"),
               ([self.complete("unsat",returncode=1)],"ERROR")]
        for reply in ("((v0 #x0))","((v0 #b01))","((v1 #x1))","((v0 #x1) (v0 #x1))",
                      "((v0 #x1))\n(error \"oops\")","()", "((v0 (_ bv44 4)))"):
            cases.append(([self.complete("sat"),self.complete("sat\n"+reply)],"ERROR"))
        for responses,status in cases:
            with self.subTest(status=status,responses=responses):
                self.assertEqual(self.solver_case(responses)[0].status,status)

    def test_unsat_with_clean_exit_only(self):
        with patch("mdai.logic.subprocess.run",return_value=self.complete("unsat\n")) as sub:
            got=verify(inp(),op("or",4,inp(),c(0)),{"x":4},max_exhaustive_bits=0,z3_path=sys.executable)
            self.assertEqual(got.status,"EQUIVALENT")
            sub.assert_called_once()
        self.assertEqual(verify(inp(),inp(),{"x":4},max_exhaustive_bits=0,z3_path="/nonexistent/z3").status,"UNKNOWN")


if __name__=="__main__":
    unittest.main()
