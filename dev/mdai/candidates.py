"""Small deterministic proposal vocabulary for A/B development expressions.

Menus contain edit descriptions only. Structural costs and proof outcomes
are not features; compute them after a proposal is selected. No file I/O.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import permutations
import random

from .aig import AIG, AIGEquivalence, compile_truth_table, compile_words, verify_aig_exact
from .logic import validate

MAX_CANDIDATES = 20


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    edit_kind: str
    path: tuple[int, ...] = ()
    parameter: tuple[int, ...] = ()


def _task(record: Mapping):
    if not isinstance(record, Mapping) or record.get("split") != "development":
        raise ValueError("only development records are accepted")
    task_id = record.get("task_id")
    if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
        raise ValueError("bad task ID")
    family = record.get("family")
    if family not in ("A", "B"):
        raise ValueError("only A/B are accepted")
    inp, order = record.get("inputs"), record.get("input_order")
    if (not isinstance(inp, dict) or not isinstance(order, list) or not order
            or not all(isinstance(k, str) for k in order)
            or len(order) != len(inp) or set(order) != set(inp)
            or not all(type(w) is int and w > 0 for w in inp.values())):
        raise ValueError("invalid signature")
    inputs = {k: inp[k] for k in order}
    n = sum(inputs.values())
    if record.get("total_input_bits") != n:
        raise ValueError("bad total input bit count")
    if family == "A":
        if n != 4 or len(inputs) != n or record.get("representation") != "truth_table_lsb0":
            raise ValueError("only exact 4-input A development records are accepted")
        table = record.get("truth_table")
        if type(table) is not int or not 0 <= table < (1 << (1 << n)):
            raise ValueError("bad task truth table")
        param = tuple((table >> row) & 1 for row in range(1 << n))
        width = 1
    else:
        if n > 16 or any(w not in (4,8) for w in inputs.values()) or record.get("representation") != "bit_ast":
            raise ValueError("only 4/8-bit B development records are accepted")
        param = record.get("ast")
        width = validate(param, inputs)
        if width not in (4, 8):
            raise ValueError("only 4/8-bit B outputs are accepted")
    outputs = record.get("outputs")
    if (not isinstance(outputs, list) or len(outputs) != 1
            or not isinstance(outputs[0], dict) or outputs[0].get("width") != width):
        raise ValueError("invalid output signature")
    return family, task_id, inputs, param


def _word(op, w, *args, **kw):
    return {"op": op, "width": w, "args": list(args), **kw}


def _not(x):
    return _word("not", x["width"], x)


def _const(value, width):
    return {"op": "const", "width": width, "value": value}


def _edits(node):
    """Yield replacement for each applicable local rule in stable order."""
    op, w, args = node["op"], node["width"], node.get("args", ())
    if op == "not" and args[0]["op"] == "not":
        yield "remove_double_not", args[0]["args"][0]
    if op in ("and", "or", "xor"):
        x, y = args
        if x == y:
            yield "idempotent", _const(0, w) if op == "xor" else x
        for side, c, other in (("left",x,y),("right",y,x)):
            if c["op"] != "const":
                continue
            v = c["value"]
            if v == 0:
                yield f"remove_zero_{side}", c if op == "and" else other
            if v == (1 << w)-1:
                yield f"remove_ones_{side}", other if op == "and" else c if op == "or" else _not(other)
        if x["op"] == op:
            a,b=x["args"]
            yield "associate_right", _word(op, w, a, _word(op, w, b, y))
        if y["op"] == op:
            a,b=y["args"]
            yield "associate_left", _word(op, w, _word(op, w, x, a), b)
        if op in ("and", "or"):
            dual = "or" if op == "and" else "and"
            if (y["op"] == dual and x in y["args"]) or (x["op"] == dual and y in x["args"]):
                yield "absorption", x if y["op"] == dual and x in y["args"] else y
            if x["op"] == dual and y["op"] == dual:
                for i,a in enumerate(x["args"]):
                    for j,b in enumerate(y["args"]):
                        if a == b:
                            yield f"factor_{i}{j}", _word(dual, w, a, _word(op, w, x["args"][1-i], y["args"][1-j]))
            if x["op"] == "not" and y["op"] == "not":
                yield "de_morgan", _not(_word(dual, w, x["args"][0], y["args"][0]))
        if op == "xor":
            if x["op"] == "xor" and y in x["args"]:
                i = x["args"].index(y)
                yield "xor_cancel_left", x["args"][1-i]
            if y["op"] == "xor" and x in y["args"]:
                i = y["args"].index(x)
                yield "xor_cancel_right", y["args"][1-i]
        # Alternative normal forms expose structurally different approaches to
        # the downstream search without leaking cost or proof observations.
        yield "commute", _word(op, w, y, x)
        if op == "xor":
            yield "xor_union_intersection", _word("and", w, _word("or", w, x, y), _not(_word("and", w, x, y)))
            yield "xor_disjoint_terms", _word("or", w, _word("and", w, x, _not(y)), _word("and", w, _not(x), y))
        else:
            dual = "or" if op == "and" else "and"
            yield "demorgan_expansion", _not(_word(dual, w, _not(x), _not(y)))
    if op in ("shl", "lshr", "rotl", "rotr"):
        amount = node["amount"]
        if op in ("rotl", "rotr"):
            if amount % w == 0:
                yield "remove_full_rotation", args[0]
            else:
                if amount >= w:
                    yield "normalize_rotation", _word(op,w,args[0],amount=amount % w)
                yield "reverse_rotation_direction", _word("rotr" if op == "rotl" else "rotl",w,args[0],amount=w-(amount % w))
        elif amount == 0:
            yield "remove_zero_shift", args[0]
        elif amount >= w:
            yield "remove_overshift", _const(0,w)
    if op == "extract" and node["lo"] == 0 and w == args[0]["width"]:
        yield "remove_full_extract", args[0]


def _walk(node, path=()):
    yield path,node
    for i,arg in enumerate(node.get("args", ())):
        yield from _walk(arg,path+(i,))


def _id(task_id, kind, path, parameter):
    return ":".join((task_id, kind, ".".join(map(str,path)) or "root", "".join(map(str,parameter)) or "-"))


def _candidate_list(family, task_id, param, limit):
    if limit == 0:
        return ()
    if family == "A":
        variants = (("shannon_order", (), p) for p in permutations(range(4)) if p != (0,1,2,3))
    else:
        def descriptors():
            found = False
            for path,node in _walk(param):
                for kind,replacement in _edits(node):
                    if replacement != node:
                        yield kind, path, ()
                        found = True
            if not found:
                yield "neutral_double_not", (), ()
        variants = descriptors()
    output = []
    for kind,path,parm in variants:
        output.append(Candidate(_id(task_id,kind,path,parm), kind, path, parm))
        if len(output) == limit:
            break
    return tuple(output)


def candidate_menu(record: Mapping, *, limit: int = MAX_CANDIDATES) -> tuple[Candidate, ...]:
    """Return at most 20 predefined single edits with task-local stable IDs."""
    if type(limit) is not int or not 0 <= limit <= MAX_CANDIDATES:
        raise ValueError("invalid candidate limit")
    family,task_id,_inputs,param=_task(record)
    return _candidate_list(family, task_id, param, limit)


def ordered_menu(record: Mapping, *, order: str = "canonical", seed: int = 0,
                 limit: int = MAX_CANDIDATES) -> tuple[Candidate, ...]:
    """Reorder the same bounded candidate universe (no scoring at menu time)."""
    menu = candidate_menu(record, limit=limit)
    if order == "canonical":
        return menu
    if order != "random" or type(seed) is not int:
        raise ValueError("invalid ordering strategy or seed")
    ordered = list(menu)
    random.Random(seed).shuffle(ordered)
    return tuple(ordered)


def seed_graph(record: Mapping) -> AIG:
    family, _task_id, inputs, param = _task(record)
    return compile_truth_table(param, tuple(inputs)) if family == "A" else compile_words(inputs, [param])


def _alternate_aig(table, inputs, ordering):
    graph = AIG(tuple(inputs))
    literals = graph.input_literals
    def emit(level, row):
        if level == len(ordering):
            return table[row]
        index = ordering[level]
        mask = 1 << (len(ordering)-1-index)
        lo,hi = emit(level+1,row),emit(level+1,row | mask)
        if lo == hi:
            return lo
        x = literals[index]
        return graph.or_(graph.and_(x ^ 1, lo), graph.and_(x, hi))
    graph.outputs = (emit(0,0),)
    return graph


def _replace(node, path, edit_kind):
    if not path:
        if edit_kind == "neutral_double_not":
            return _not(_not(node))
        return next(replacement for rule,replacement in _edits(node) if rule == edit_kind)
    i,*rest=path
    args=list(node["args"])
    args[i] = _replace(args[i], rest, edit_kind)
    return {**node,"args":args}


def build_candidate(record: Mapping, proposal: Candidate) -> AIG:
    """Build exactly one known menu edit; never execute arbitrary proposal text."""
    family,task_id,inputs,param = _task(record)
    if not isinstance(proposal, Candidate) or proposal not in _candidate_list(family,task_id,param,MAX_CANDIDATES):
        raise ValueError("proposal is not in the task menu")
    if family == "A":
        return _alternate_aig(param,inputs,proposal.parameter)
    return compile_words(inputs, [_replace(param,proposal.path,proposal.edit_kind)])


def verify_candidate(seed: AIG, candidate: AIG) -> AIGEquivalence:
    """Independent, fail-closed check of the complete candidate graph."""
    return verify_aig_exact(seed, candidate)
