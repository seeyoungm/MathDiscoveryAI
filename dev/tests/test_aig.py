import itertools
import unittest

from mdai.aig import AIG, Metrics, compile_truth_table, compile_words


class TestAIG(unittest.TestCase):
    def test_structural_hash_metrics_dead(self):
        g = AIG(['x', 'y', 'z'])
        x, y, z = g.input_literals
        p = g.and_(x, y)
        self.assertEqual(p, g.and_(y, x))
        self.assertEqual(g.and_(x, x ^ 1), 0)
        bottom = g.and_(p ^ 1, z)
        self.assertEqual(g.and_(1, bottom), bottom)
        g.and_(x, z)  # dead: not charged
        g.outputs = (bottom ^ 1, p)
        self.assertEqual(g.metrics(), Metrics(2, 2))
        h = AIG.from_aag(g.to_aag())
        self.assertEqual(h.metrics(), g.metrics())
        self.assertEqual(h.input_names, ('x', 'y', 'z'))
        for a,b,c in itertools.product((0,1), repeat=3):
            self.assertEqual(h.evaluate((a,b,c)), (1 ^ ((1 ^ (a&b))&c), a&b))

    def test_input_literals_constants_and_empty_metrics(self):
        g = AIG(['x'])
        self.assertEqual(g.metrics(), Metrics(0, 0))
        g.outputs=(1,0,3)
        for x in (0,1):
            self.assertEqual(AIG.from_aag(g.to_aag()).evaluate((x,)),(1,0,1^x))

    def test_untrusted_aag_rejects_bad_references_and_sequential_forms(self):
        for malformed in [
            "aag 1 1 1 0 0\n2\n3\n",
            "aag 2 1 0 1 1\n2\n4\n4 4 2\n",
            "aag 3 1 0 1 1\n2\n6\n6 4 2\n",
            "aag 2 1 0 1 1\n2\n9\n4 0 2\n",
            "aag 1 1 0 1 0\n2\n3\ni1 missing\n",
            "aag 1 1 0 1 0\n2\n3\ni0 first\ni0 dupe\n",
        ]:
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    AIG.from_aag(malformed)

    def test_aag_sparse_ids_and_imported_cost_not_changed(self):
        g = AIG.from_aag("aag 4 1 0 1 2\n2\n9\n8 6 2\n6 2 2\n")
        self.assertEqual(g.metrics(), Metrics(2,2))
        self.assertEqual(g.evaluate([0]), (1,))
        self.assertEqual(g.evaluate([1]), (0,))
        self.assertEqual(AIG.from_aag(g.to_aag()).metrics(),Metrics(2,2))

    def test_shannon_order(self):
        bits = [int((x and not y) or (z and y)) for x,y,z in itertools.product((0,1), repeat=3)]
        g = compile_truth_table(bits,('x','y','z'))
        self.assertEqual([g.evaluate(p)[0] for p in itertools.product((0,1), repeat=3)],bits)

    def test_bitvector_semantics_and_multioutput_order(self):
        def ast(op,w,*args,**params):
            return dict(op=op,width=w,args=list(args),**params)
        x=ast("input",4,name="x")
        y=ast("input",4,name="y")
        cat=ast("concat",4,ast("extract",2,x,hi=3,lo=2),ast("extract",2,y,hi=1,lo=0))
        sub=ast("xor",4,ast("rotl",4,x,amount=5),ast("lshr",4,y,amount=1))
        zero=ast("shl",4,x,amount=4)
        complement=ast("not",4,y)
        ror=ast("rotr",4,x,amount=3)
        g=compile_words({'y':4,'x':4},[cat,sub,zero,complement,ror])
        self.assertEqual(g.input_names,tuple(f'{n}[{j}]' for n in ('y','x') for j in range(4)))
        def bits(n):
            return tuple((n >> j) & 1 for j in range(4))
        h=AIG.from_aag(g.to_aag(symbols=False))
        for i,j in itertools.product(range(16),repeat=2):
            rot=((i<<1)|(i>>3))&15
            self.assertEqual(h.evaluate(bits(j)+bits(i)),bits((i&12)|(j&3))+bits(rot^(j>>1))+bits(0)+bits(15^j)+bits(rot))


class TestAIGProof(unittest.TestCase):
    def test_proof_checks_order_and_produces_real_witness(self):
        from mdai.aig import verify_aig_exact
        g = compile_truth_table([0, 0, 1, 0], ["first", "second"])
        h = AIG.from_aag(g.to_aag(symbols=False))
        self.assertEqual(verify_aig_exact(g,h).status, "EQUIVALENT")
        h.outputs = (h.outputs[0] ^ 1,)
        result = verify_aig_exact(g,h)
        self.assertEqual(result.status,"DIFFERENT")
        self.assertNotEqual(g.evaluate(result.counterexample),h.evaluate(result.counterexample))
        self.assertEqual(verify_aig_exact(g,h,max_input_bits=1).status,"UNKNOWN")
        self.assertEqual(verify_aig_exact(g,h,max_bit_cells=1).status,"UNKNOWN")
        self.assertEqual(verify_aig_exact(g,AIG()).status,"INVALID_INTERFACE")

    def test_full_width_exhaustive_bit_parallel(self):
        from mdai.aig import verify_aig_exact
        g=AIG([f"x{i}" for i in range(16)])
        x,y=g.input_literals[0],g.input_literals[-1]
        g.outputs=(g.xor(x,y),)
        h=AIG.from_aag(g.to_aag(symbols=False))
        self.assertEqual(verify_aig_exact(g,h).status,"EQUIVALENT")
        h.outputs=(h.outputs[0]^1,)
        self.assertEqual(verify_aig_exact(g,h).status,"DIFFERENT")

    def test_larger_than_16_is_unknown_without_false_pass(self):
        from mdai.aig import verify_aig_exact
        g = AIG([f"x{i}" for i in range(17)])
        g.outputs = (g.input_literals[-1],)
        self.assertEqual(verify_aig_exact(g, g, max_input_bits=32).status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
