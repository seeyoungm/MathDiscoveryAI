import unittest

from mdai import devgen


class GeneratorTests(unittest.TestCase):
    def test_exact_npn_and_remove_noise(self):
        and_low = sum(int((r & 3) == 3) << r for r in range(16))
        nor_high = sum(int((r & 12) == 0) << r for r in range(16))
        self.assertEqual(devgen.reduce_truth_table(4, and_low), (2, 0b1000))
        self.assertEqual(devgen.canonical_npn(4, and_low), devgen.canonical_npn(4, nor_high))
        self.assertNotEqual(devgen.canonical_npn(2, 0b1000), devgen.canonical_npn(2, 0b0110))
        self.assertEqual(devgen.canonical_npn(6, 0), (0, 0))
        self.assertIsNone(devgen.canonical_npn(5, 1 << 31))
        self.assertIsNone(devgen.canonical_npn(6, 1 << 63))

    def test_truth_columns_row_convention(self):
        _, cols = devgen._truth_columns(8)
        for row in range(256):
            for p in range(8):
                self.assertEqual((cols[p] >> row) & 1, (row >> p) & 1)

    def test_unsigned_width_conventions(self):
        x = {"op": "input", "name": "x", "width": 4}
        y = {"op": "input", "name": "y", "width": 4}
        w = {"x": 4, "y": 4}
        node = devgen._node
        def key(a):
            return devgen.bit_function_key(a, w)
        self.assertEqual(key(node("rotl", 4, x, amount=1)), key(node("rotr", 4, x, amount=3)))
        self.assertEqual(key(node("concat", 4, node("extract", 2, x, lo=2, hi=3),
                                  node("extract", 2, x, lo=0, hi=1))), key(x))
        self.assertEqual(key(node("shl", 4, x, amount=9)), key(node("lshr", 4, y, amount=4)))
        self.assertNotEqual(key(node("and", 4, x, y)), key(node("xor", 4, x, y)))
        self.assertEqual(key(node("and", 4, x, y)), key(node("and", 4, y, x)))
        with self.assertRaises(ValueError):
            key(node("extract", 2, x, lo=2, hi=2))
        with self.assertRaises(ValueError):
            key(node("shl", 4, x, amount=-1))
        with self.assertRaises(ValueError):
            key(node("shl", 4, x, y, amount=1))

    def test_deterministic_development_only_and_all_unique(self):
        results = devgen.generate_development()
        self.assertEqual(results, devgen.generate_development())
        self.assertNotEqual(results, devgen.generate_development(a_seed=27, b_seed=41))
        self.assertEqual([r["family"] for r in results].count("A"), 32)
        self.assertEqual([r["family"] for r in results].count("B"), 32)
        self.assertEqual({r["outputs"][0]["width"] for r in results if r["family"] == "B"}, {4, 8})
        self.assertEqual({r["split"] for r in results}, {"development"})
        self.assertEqual(len({r["canonical_id"] for r in results}), 64)
        self.assertEqual({r["release_status"] for r in results}, {"dev_smoke_only_no_training_release"})
        self.assertEqual(len(devgen.partition_groups(results)["development"]), 64)
        self.assertEqual(devgen.generate_development(0, 0), [])

    def test_components_transitively_fail_closed(self):
        r = lambda k,root,c,s,p=None: dict(task_id=k, root_id=root, canonical_id=c, split=s, parent_id=p)
        rows = [r("a", "a", "u", "development"),
                r("b", "a", "v", "development", "a"),
                r("c", "c", "v", "final")]
        with self.assertRaisesRegex(ValueError,"crosses"):
            devgen.partition_groups(rows)
        rows[-1]["split"] = "development"
        self.assertEqual(len(devgen.partition_groups(rows)["development"][0]), 3)
        with self.assertRaisesRegex(ValueError,"unknown parent"):
            devgen.partition_groups([rows[1]])
        cycle = [r("a","a","u","development","b"), r("b","a","v","development","a")]
        with self.assertRaisesRegex(ValueError,"cyclic"):
            devgen.partition_groups(cycle)


if __name__ == "__main__":
    unittest.main()
