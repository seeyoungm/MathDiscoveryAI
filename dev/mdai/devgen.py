"""Deterministic toy A/B development tasks. No evaluation data or paths."""
from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from itertools import permutations
import json
import re

GENERATOR = "mdai-ab-v1"
A_SEED = 2026112201
B_SEED = 2026112202
A_SPLIT = "development"
_COMMUTATIVE = {"and", "or", "xor"}
_NODE_TYPES = {"input", "const", "and", "or", "xor", "not", "shl", "lshr", "rotl", "rotr", "extract", "concat"}


def _digest(obj):
    return sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _draw(seed: int, pos: int, salt: str) -> int:
    return int(_digest([GENERATOR, seed, pos, salt]), 16)


def _expand_index(row: int, active: tuple[int, ...]) -> int:
    return sum(((row >> (len(active) - 1 - i)) & 1) << bit for i, bit in enumerate(active))


def reduce_truth_table(n: int, table: int) -> tuple[int, int]:
    """Return effective input count and packed truth table, row 0 in bit 0."""
    if type(n) is not int or not 0 <= n <= 6 or type(table) is not int or not 0 <= table < 1 << (1 << n):
        raise ValueError("invalid input width or packed truth table")
    active = tuple(k for k in range(n - 1, -1, -1)
                   if any(((table >> r) ^ (table >> (r ^ (1 << k)))) & 1
                          for r in range(1 << n) if not r & (1 << k)))
    reduced = sum(((table >> _expand_index(r, active)) & 1) << r for r in range(1 << len(active)))
    return len(active), reduced


@lru_cache(maxsize=5)
def _npn_maps(n: int):
    return tuple(tuple(_expand_index(r, ordering) ^ neg for r in range(1 << n))
                 for ordering in permutations(range(n)) for neg in range(1 << n))


def canonical_npn(n: int, table: int) -> tuple[int, int] | None:
    """Exact <=4 effective inputs; return None, never a coarse key, otherwise."""
    n, table = reduce_truth_table(n, table)
    if n > 4:
        return None
    mask = (1 << (1 << n)) - 1
    best = mask
    for mapping in _npn_maps(n):
        value = sum(((table >> src) & 1) << dst for dst, src in enumerate(mapping))
        best = min(best, value, value ^ mask)
    return n, best


def _validate_ast(ast, widths):
    if not isinstance(ast, dict) or not isinstance(ast.get("op"), str) or ast["op"] not in _NODE_TYPES:
        raise ValueError("invalid AST node")
    op, width = ast["op"], ast.get("width")
    if type(width) is not int or not 1 <= width <= 8:
        raise ValueError("development width must be 1..8")
    if op == "input":
        if set(ast) != {"op", "width", "name"} or not isinstance(ast["name"], str) or widths.get(ast["name"]) != width:
            raise ValueError("invalid input")
        return width
    if op == "const":
        if set(ast) != {"op", "width", "value"} or type(ast["value"]) is not int or not 0 <= ast["value"] < 1 << width:
            raise ValueError("invalid constant")
        return width
    extra = "lo" if op == "extract" else "amount" if op in {"shl", "lshr", "rotl", "rotr"} else None
    keys = {"op", "args", "width"} | ({"lo", "hi"} if op == "extract" else {extra} if extra else set())
    if set(ast) != keys or not isinstance(ast["args"], list):
        raise ValueError("invalid AST fields")
    sizes = tuple(_validate_ast(x, widths) for x in ast["args"])
    value = ast.get(extra) if extra else None
    if op in _COMMUTATIVE:
        valid = sizes == (width, width)
    elif op == "not":
        valid = sizes == (width,)
    elif op in {"shl", "lshr", "rotl", "rotr"}:
        valid = sizes == (width,) and type(value) is int and value >= 0
    elif op == "extract":
        valid = (len(sizes) == 1 and type(value) is int and type(ast["hi"]) is int and
                 0 <= value <= ast["hi"] < sizes[0] and ast["hi"] - value + 1 == width)
    else:
        valid = len(sizes) == 2 and sum(sizes) == width
    if not valid:
        raise ValueError("invalid arity, width, or fixed operand")
    return width


@lru_cache(maxsize=17)
def _truth_columns(n: int):
    """One Python bigint per independent bit; row 0 is the least significant bit."""
    rows = 1 << n
    mask = (1 << rows) - 1
    # Column j is the periodic bit pattern 0^(2^j)1^(2^j).
    columns = tuple(mask ^ (mask // ((1 << (1 << j)) + 1)) for j in range(n))
    return mask, columns


def _bit_eval(ast, env, mask):
    op = ast["op"]
    w = ast["width"]
    if op == "input":
        return env[ast["name"]]
    if op == "const":
        return tuple(mask if ast["value"] & (1 << i) else 0 for i in range(w))
    args = tuple(_bit_eval(x, env, mask) for x in ast["args"])
    if op in _COMMUTATIVE:
        a, b = args
        if op == "and":
            return tuple(i & j for i, j in zip(a, b))
        if op == "or":
            return tuple(i | j for i, j in zip(a, b))
        return tuple(i ^ j for i, j in zip(a, b))
    a = args[0]
    if op == "not":
        return tuple(x ^ mask for x in a)
    if op == "concat":
        return args[1] + a
    if op == "extract":
        return a[ast["lo"]:ast["lo"] + w]
    k = ast["amount"]
    if op == "shl":
        k = min(k, w)
        return (0,) * k + a[:w - k]
    if op == "lshr":
        k = min(k, w)
        return a[k:] + (0,) * k
    k %= w
    if op == "rotl":
        return a[-k:] + a[:-k] if k else a
    return a[k:] + a[:k]


def bit_function_key(ast: dict, widths: dict[str, int]) -> str:
    """Exact fixed named signature, not NPN or cross-signature equivalence."""
    if not isinstance(widths, dict) or not widths or not all(
        isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_]*", k) and type(w) is int and 1 <= w <= 8
        for k, w in widths.items()
    ) or sum(widths.values()) > 16:
        raise ValueError("unsupported input signature")
    _validate_ast(ast, widths)
    signature = tuple(sorted(widths.items()))
    n = sum(widths.values())
    mask, columns = _truth_columns(n)
    env, offset = {}, n
    for name, w in signature:
        offset -= w
        env[name] = columns[offset:offset + w]
    out = _bit_eval(ast, env, mask)
    return "B-fixed-function:" + _digest([signature, ast["width"], [hex(x) for x in out]])


def structural_key(ast: dict) -> str:
    """Canonicalize commutative ordering; not an equivalence prover."""
    def normalize(node):
        normalized = {**node}
        if "args" in node:
            args = [normalize(arg) for arg in node["args"]]
            if node["op"] in _COMMUTATIVE:
                args.sort(key=lambda a: json.dumps(a, sort_keys=True))
            normalized["args"] = args
        return normalized
    return "B-structure:" + _digest(normalize(ast))


def _leaf(name, width):
    return {"op": "input", "name": name, "width": width}


def _node(op, width, *args, **parameters):
    return {"op": op, "width": width, "args": list(args), **parameters}


def _bit_ast(seed, pos, width):
    x, y = _leaf("x", width), _leaf("y", width)
    case = _draw(seed, pos, "op") % 10
    k = 1 + _draw(seed, pos, "fixed-position") % (width - 1)
    mix = tuple(sorted(_COMMUTATIVE))[_draw(seed, pos, "mix") % 3]
    c = {"op": "const", "width": width, "value": _draw(seed, pos, "constant") % (1 << width)}
    if case < 4:
        return _node(mix, width, _node(("shl", "lshr", "rotl", "rotr")[case], width, x, amount=k), y)
    if case == 4:
        return _node(mix, width, _node("not", width, x), y)
    if case == 5:
        return _node("concat", width, _node("extract", width - k, x, lo=k, hi=width - 1),
                     _node("extract", k, y, lo=0, hi=k - 1))
    if case == 6:
        return _node(mix, width, _node("xor", width, x, c), y)
    if case == 7:
        return _node(mix, width, _node("or", width, x, y), c)
    if case == 8:
        return _node(mix, width, x, y)
    return _node("concat", width, _node("extract", k, y, lo=width - k, hi=width - 1),
                 _node("extract", width - k, x, lo=0, hi=width - k - 1))


def _record(family, seed, index, signature, output_width, canonical_id, **payload):
    key = f"{family}-dev-{seed}-{index}"
    return {
        "family": family, "task_id": key, "root_id": key, "parent_id": None,
        "generator_version": GENERATOR, "seed": seed, "source_index": index,
        "inputs": signature, "input_order": list(signature),
        "total_input_bits": sum(signature.values()),
        "outputs": [{"id": "out", "width": output_width}],
        "canonical_id": canonical_id, "split": A_SPLIT,
        "release_status": "dev_smoke_only_no_training_release", **payload,
    }


def generate_development(a_count=32, b_count=32, *, a_seed=A_SEED, b_seed=B_SEED):
    """Generate only development roots; no generation/access of evaluation tasks."""
    if any(type(x) is not int or not 0 <= x <= 64 for x in (a_count, b_count)):
        raise ValueError("development quota must be an integer 0..64 per family")
    if any(type(s) is not int or s < 0 for s in (a_seed, b_seed)):
        raise ValueError("seed must be a nonnegative integer")
    output, seen = [], set()
    for family, seed, count in (("A", a_seed, a_count), ("B", b_seed, b_count)):
        made = 0
        for pos in range(max(256, count * 64)):
            if made == count:
                break
            if family == "A":
                n = 4  # Effective 5/6-input cases are quarantined outside the generator.
                table = _draw(seed, pos, "truth-table") & ((1 << (1 << n)) - 1)
                reduced = canonical_npn(n, table)
                assert reduced is not None
                effective, canonical = reduced
                if effective != n:
                    continue
                key = f"A-npn:{effective}:{canonical:x}"
                rec = _record(family, seed, pos, {f"x{i}": 1 for i in range(n)}, 1, key,
                              representation="truth_table_lsb0", truth_table=table,
                              equivalence_scope="exact_NPN_effective_inputs")
            else:
                w = (4, 8)[pos % 2]
                inputs = {"x": w, "y": w}
                ast = _bit_ast(seed, pos, w)
                key = bit_function_key(ast, inputs)
                rec = _record(family, seed, pos, inputs, w, key, representation="bit_ast",
                              ast=ast, structural_id=structural_key(ast),
                              equivalence_scope="exact_fixed_signature; wider/cross_signature/NPN_not_tested")
            if key not in seen:
                seen.add(key)
                output.append(rec)
                made += 1
        if made != count:
            raise RuntimeError(f"could not meet deduplicated development quota for {family}")
    return output


def partition_groups(records):
    """Audit connected components and fail closed if linked records cross partitions."""
    records = list(records)
    indexed = {}
    for r in records:
        task = r.get("task_id")
        if not isinstance(task, str) or not task or task in indexed:
            raise ValueError("missing or duplicate task ID")
        if not isinstance(r.get("root_id"), str) or not r["root_id"] or not isinstance(r.get("split"), str) or not r["split"]:
            raise ValueError("missing lineage or split")
        for k in ("parent_id", "canonical_id", "structural_id"):
            v = r.get(k)
            if v is not None and (not isinstance(v, str) or not v):
                raise ValueError(f"invalid {k}")
        indexed[task] = r
    link = {k: k for k in indexed}
    def find(k):
        while link[k] != k:
            link[k] = link[link[k]]
            k = link[k]
        return k
    def union(a, b):
        a, b = sorted((find(a), find(b)))
        link[b] = a
    owners = {}
    for task, record in indexed.items():
        root, parent = record["root_id"], record.get("parent_id")
        if root in indexed:
            if indexed[root]["root_id"] != root:
                raise ValueError("declared root must be its own parentless lineage root")
            union(task, root)
        if parent:
            if parent not in indexed:
                raise ValueError("unknown parent; audit complete manifest")
            if indexed[parent]["root_id"] != root:
                raise ValueError("conflicting lineage root")
            union(task, parent)
            trace = {task}
            cursor = parent
            while cursor is not None:
                if cursor in trace:
                    raise ValueError("cyclic lineage")
                if cursor not in indexed:
                    raise ValueError("unknown parent; audit complete manifest")
                trace.add(cursor)
                cursor = indexed[cursor].get("parent_id")
        for name in ("root_id", "canonical_id", "structural_id"):
            value = record.get(name)
            if value:
                union(task, owners.setdefault((name, value), task))
    components = {}
    for task in sorted(indexed):
        components.setdefault(find(task), []).append(indexed[task])
    by_split = {}
    for rows in components.values():
        splits = {r["split"] for r in rows}
        if len(splits) != 1:
            raise ValueError("lineage/equivalence component crosses split")
        by_split.setdefault(next(iter(splits)), []).append(rows)
    return by_split


if __name__ == "__main__":
    for _record_data in generate_development():
        print(json.dumps(_record_data, sort_keys=True, separators=(",", ":")))
