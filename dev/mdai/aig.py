"""Deterministic AND-inverter graphs for small combinational expressions.

Literals are AIGER literals: 0/1 are constants, and the low bit inverts.
Word I/O uses LSB-first bit vectors. AIGER symbols are labels, not proof
of I/O equivalence: an external transformation must still be verified.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Metrics:
    and_count: int
    depth: int


class AIG:
    def __init__(self, input_names: Sequence[str] = ()):
        self.input_names = tuple(input_names)
        if not all(isinstance(n, str) and n and all(32 <= ord(ch) <= 126 for ch in n)
                   for n in self.input_names):
            raise ValueError("invalid input label")
        if len(set(self.input_names)) != len(self.input_names):
            raise ValueError("duplicate input label")
        self._gates: list[tuple[int, int]] = []
        self._intern: dict[tuple[int, int], int] = {}
        self.outputs: tuple[int, ...] = ()

    @property
    def input_literals(self) -> tuple[int, ...]:
        return tuple(2 * j for j in range(1, len(self.input_names) + 1))

    def _literal(self, n: int) -> int:
        if type(n) is not int or not 0 <= n < 2 * (1 + len(self.input_names) + len(self._gates)):
            raise ValueError("undefined literal")
        return n

    def and_(self, a: int, b: int) -> int:
        self._literal(a)
        self._literal(b)
        if not a or not b or a == (b ^ 1):
            return 0
        if a == 1:
            return b
        if b == 1 or a == b:
            return a
        key = (min(a, b), max(a, b))
        if key not in self._intern:
            self._gates.append(key)
            self._intern[key] = 2 * (len(self.input_names) + len(self._gates))
        return self._intern[key]

    def or_(self, a: int, b: int) -> int:
        return self.and_(a ^ 1, b ^ 1) ^ 1

    def xor(self, a: int, b: int) -> int:
        return self.or_(self.and_(a, b ^ 1), self.and_(a ^ 1, b))

    def _retained(self) -> list[int]:
        pending = [self._literal(l) // 2 for l in self.outputs]
        used = set()
        n = len(self.input_names)
        while pending:
            var = pending.pop()
            if var <= n or var in used:
                continue
            used.add(var)
            a, b = self._gates[var - n - 1]
            pending.extend((a // 2, b // 2))
        return sorted(used)

    def metrics(self) -> Metrics:
        n = len(self.input_names)
        used = self._retained()
        depths = {j: 0 for j in range(n + 1)}
        for var in used:
            a, b = self._gates[var - n - 1]
            depths[var] = 1 + max(depths[a // 2], depths[b // 2])
        return Metrics(len(used), max((depths[l // 2] for l in self.outputs), default=0))

    def evaluate(self, bits: Sequence[int]) -> tuple[int, ...]:
        if len(bits) != len(self.input_names) or any(type(b) not in (int, bool) or b not in (0, 1) for b in bits):
            raise ValueError("wrong input bits")
        vals = [0, *map(int, bits)]
        def val(l: int) -> int:
            return vals[l // 2] ^ (l & 1)
        for a, b in self._gates:
            vals.append(val(a) & val(b))
        return tuple(val(self._literal(l)) for l in self.outputs)

    def to_aag(self, *, symbols: bool = True) -> str:
        n = len(self.input_names)
        used = self._retained()
        remap = {j: j for j in range(n + 1)}
        remap.update((v, i) for i, v in enumerate(used, n + 1))
        def lit(l: int) -> int:
            return 2 * remap[l // 2] + (l & 1)
        lines = [f"aag {n + len(used)} {n} 0 {len(self.outputs)} {len(used)}"]
        lines.extend(str(i) for i in self.input_literals)
        lines.extend(str(lit(o)) for o in self.outputs)
        for var in used:
            a, b = sorted((lit(l) for l in self._gates[var - n - 1]), reverse=True)
            lines.append(f"{2 * remap[var]} {a} {b}")
        if symbols:
            lines.extend(f"i{j} {name}" for j, name in enumerate(self.input_names))
        return "\n".join(lines) + "\n"

    @classmethod
    def from_aag(cls, text: str, *, max_bytes: int = 2_000_000,
                 max_items: int = 65_536) -> AIG:
        """Read a combinational ASCII AIGER file; preserve gate costs as read.

        The importer deliberately keeps raw gates, including dead/redundant gates.
        Metrics count the reachable structure that actually came out of the tool;
        parsing should not silently improve the reported cost.
        """
        if not isinstance(text, str) or len(text) > max_bytes or not text.isascii():
            raise ValueError("invalid or oversized AAG")
        lines = text.splitlines()
        if not lines or not re.fullmatch(r"aag(?: [0-9]{1,9}){5}", lines[0]):
            raise ValueError("unsupported AAG header")
        m, i, l, o, a = map(int, lines[0].split()[1:])
        if l or m > max_items or i + o + a > max_items:
            raise ValueError("unsupported AAG size or latches")
        end = 1 + i + o + a
        if len(lines) < end:
            raise ValueError("truncated AAG")
        def nums(line: str, k: int) -> tuple[int, ...]:
            if not re.fullmatch(r"[0-9]{1,9}(?: [0-9]{1,9}){" + str(k-1) + "}", line):
                raise ValueError("invalid AAG row")
            return tuple(map(int, line.split()))
        entries = [nums(line, 1)[0] for line in lines[1:1+i]]
        if any(v < 2 or v & 1 or v//2 > m for v in entries) or len(set(entries)) != i:
            raise ValueError("invalid AAG input")
        outs = [nums(line, 1)[0] for line in lines[1+i:1+i+o]]
        gates: dict[int, tuple[int, int]] = {}
        seen_inputs = set(entries)
        for line in lines[1+i+o:end]:
            out, x, y = nums(line, 3)
            if out < 2 or out & 1 or out//2 > m or out in seen_inputs or out in gates:
                raise ValueError("invalid or repeated AAG AND output")
            if max(x, y) >= out:
                raise ValueError("AAG gate operands must precede their output")
            gates[out] = (x, y)
        if max([0, *entries, *gates]) // 2 != m:
            raise ValueError("inconsistent AAG max variable")
        names = [f"i{j}" for j in range(i)]
        seen_sym = set()
        for line in lines[end:]:
            if line == "c":
                break
            mat = re.fullmatch(r"([io])([0-9]{1,9}) ([ -~]+)", line)
            if not mat:
                raise ValueError("unsupported AAG symbol")
            kind, nraw, name = mat.groups()
            idx = int(nraw)
            if idx >= (i if kind == "i" else o) or (kind, idx) in seen_sym:
                raise ValueError("invalid AAG symbol index")
            seen_sym.add((kind, idx))
            if kind == "i":
                names[idx] = name
        graph = cls(names)
        mapping = {0: 0, **dict(zip(entries, graph.input_literals))}
        def mapped(lit: int) -> int:
            key = lit & ~1
            if key not in mapping:
                raise ValueError("dangling AAG literal")
            return mapping[key] ^ (lit & 1)
        for old, (x, y) in sorted(gates.items()):
            a1, a2 = sorted((mapped(x), mapped(y)))
            graph._gates.append((a1, a2))
            new = 2 * (i + len(graph._gates))
            graph._intern.setdefault((a1, a2), new)
            mapping[old] = new
        graph.outputs = tuple(mapped(lit) for lit in outs)
        return graph


def compile_words(inputs: Mapping[str, int], outputs: Sequence[dict]) -> AIG:
    """Compile ordered, validated safe word expressions into a shared AIG."""
    from .logic import validate

    if not isinstance(inputs, Mapping) or not outputs:
        raise ValueError("input mapping and at least one output required")
    for expression in outputs:
        validate(expression, inputs)
    labels = [f"{name}[{b}]" for name, w in inputs.items() for b in range(w)]
    graph = AIG(labels)
    words = {}
    offset = 0
    for name, w in inputs.items():
        words[name] = graph.input_literals[offset:offset + w]
        offset += w

    def convert(node: dict) -> tuple[int, ...]:
        op, w = node["op"], node["width"]
        if op == "input":
            return words[node["name"]]
        if op == "const":
            return tuple((node["value"] >> b) & 1 for b in range(w))
        args = [convert(arg) for arg in node["args"]]
        x = args[0]
        if op == "not":
            return tuple(b ^ 1 for b in x)
        if op in ("and", "or", "xor"):
            fn = {"and": graph.and_, "or": graph.or_, "xor": graph.xor}[op]
            return tuple(fn(a, b) for a, b in zip(x, args[1]))
        if op in ("shl", "lshr"):
            n = min(node["amount"], w)
            return (0,) * n + x[:w - n] if op == "shl" else x[n:] + (0,) * n
        if op in ("rotl", "rotr"):
            n = node["amount"] % w
            if op == "rotl":
                return x[w - n:] + x[:w - n]
            return x[n:] + x[:n]
        if op == "extract":
            return x[node["lo"]:node["hi"]+1]
        if op == "concat":
            return tuple(bit for arg in reversed(args) for bit in arg)
        raise ValueError("unsupported operation")

    graph.outputs = tuple(bit for o in outputs for bit in convert(o))
    return graph


def compile_truth_table(bits: Sequence[int], input_names: Sequence[str]) -> AIG:
    """Compile a small single-output truth table using ordered Shannon splits.

    Table rows follow lexical assignment order, first declared input being
    the most significant bit of the row index.
    """
    if len(input_names) > 6 or len(bits) != 1 << len(input_names):
        raise ValueError("invalid or out-of-scope truth table size")
    table = tuple(bits)
    if any(type(bit) not in (int, bool) or bit not in (0, 1) for bit in table):
        raise ValueError("invalid truth table bit")
    graph = AIG(input_names)
    literals = graph.input_literals

    def shannon(i: int, values: tuple[int, ...]) -> int:
        if len(set(values)) == 1:
            return int(values[0])
        mid = len(values) // 2
        lo = shannon(i + 1, values[:mid])
        hi = shannon(i + 1, values[mid:])
        if lo == hi:
            return lo
        x = literals[i]
        return graph.or_(graph.and_(x ^ 1, lo), graph.and_(x, hi))

    graph.outputs = (shannon(0, table),)
    return graph


@dataclass(frozen=True)
class AIGEquivalence:
    status: str
    counterexample: tuple[int, ...] | None = None


def verify_aig_exact(reference: AIG, candidate: AIG, *, max_input_bits: int = 16,
                     max_bit_cells: int = 100_000_000) -> AIGEquivalence:
    """Exhaustive, bounded proof for two combinational graphs' ordered I/O.

    Pair interfaces by position, not labels (external tools may drop symbols).
    A numerical work cap bounds the bit-vector arrays. If the proof would
    exceed the cap, return UNKNOWN, never PASS.
    """
    n = len(reference.input_names)
    if n != len(candidate.input_names) or len(reference.outputs) != len(candidate.outputs):
        return AIGEquivalence("INVALID_INTERFACE")
    if type(max_input_bits) is not int or type(max_bit_cells) is not int or max_input_bits < 0 or max_bit_cells <= 0:
        raise ValueError("invalid exact proof limits")
    if n > min(16, max_input_bits):
        return AIGEquivalence("UNKNOWN")
    rows = 1 << n
    keep = reference._retained(), candidate._retained()
    cells = 1 + n + len(reference.outputs) + sum(map(len, keep))
    if rows * cells > max_bit_cells:
        return AIGEquivalence("UNKNOWN")
    mask = (1 << rows) - 1

    # Use repeated blocks instead of a row-by-row loop or an unbounded shift.
    def pattern(width: int) -> int:
        block = (1 << width) - 1
        repeat = mask // ((1 << (2 * width)) - 1)
        return (repeat * block) << width

    inputs = {0: 0, **{j+1: pattern(1 << (n-j-1)) for j in range(n)}}

    def run(graph: AIG, retained: list[int]) -> tuple[int, ...]:
        values = inputs.copy()
        def get(lit: int) -> int:
            return values[lit // 2] ^ (mask if lit & 1 else 0)
        for var in retained:
            a, b = graph._gates[var - n - 1]
            values[var] = get(a) & get(b)
        return tuple(get(lit) for lit in graph.outputs)

    for left, right in zip(run(reference, keep[0]), run(candidate, keep[1])):
        delta = left ^ right
        if delta:
            row = (delta & -delta).bit_length() - 1
            bits = tuple((row >> (n-j-1)) & 1 for j in range(n))
            assert reference.evaluate(bits) != candidate.evaluate(bits)
            return AIGEquivalence("DIFFERENT", bits)
    return AIGEquivalence("EQUIVALENT")
