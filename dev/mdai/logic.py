"""Strict, non-executable expression grammar and exact equivalence checks."""
import itertools
import json
import math
import os
import re
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

MAX_WIDTH = 256
MAX_NODES = 2048
MAX_DEPTH = 64
MAX_INPUT_BITS = 4096
MAX_EXHAUSTIVE_BITS = 16
MAX_JSON_BYTES = 262144
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_BINARY = {"and", "or", "xor"}
_SHIFT = {"shl", "lshr", "rotl", "rotr"}


class InvalidExpression(ValueError):
    pass


@dataclass(frozen=True)
class Result:
    status: str
    backend: str
    counterexample: dict | None = None
    detail: str = ""


def _require(ok, detail):
    if not ok:
        raise InvalidExpression(detail)


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _signature(inputs):
    _require(isinstance(inputs, Mapping), "input signature must be a mapping")
    _require(len(inputs) <= MAX_INPUT_BITS, "too many input names")
    result = {}
    for name, width in inputs.items():
        _require(type(name) is str and _NAME.fullmatch(name), "invalid input identifier")
        _require(_integer(width, 1, MAX_WIDTH), "invalid input width")
        result[name] = width
    _require(sum(result.values()) <= MAX_INPUT_BITS, "input bit limit exceeded")
    return result


def _compile(expr, inputs):
    """Validate and make an immutable post-order representation."""
    program = []

    def walk(node, depth):
        _require(depth <= MAX_DEPTH and len(program) < MAX_NODES, "expression complexity limit")
        _require(type(node) is dict and all(type(key) is str for key in node), "each node must be an object")
        op, width = node.get("op"), node.get("width")
        _require(type(op) is str, "op must be a string")
        _require(_integer(width, 1, MAX_WIDTH), "invalid width")
        base = {"op", "width"}
        keys = set(node)
        if op in ("input", "const"):
            field = "name" if op == "input" else "value"
            _require(keys in (base | {field}, base | {field, "args"}), f"invalid fields for {op}")
            _require("args" not in node or (type(node["args"]) is list and not node["args"]), "leaf args must be empty")
            param = node[field]
            if op == "input":
                _require(type(param) is str and param in inputs and inputs[param] == width, "undeclared or incorrectly sized input")
            else:
                _require(_integer(param, 0, (1 << width) - 1), "constant outside width")
            child = ()
        else:
            param = None
            extra = {"amount"} if op in _SHIFT else {"hi", "lo"} if op == "extract" else set()
            _require(op in (_BINARY | _SHIFT | {"not", "extract", "concat"}), "unknown operation")
            _require(keys == base | {"args"} | extra, f"invalid fields for {op}")
            args = node["args"]
            _require(type(args) is list and (2 <= len(args) <= MAX_WIDTH if op == "concat" else len(args) == (2 if op in _BINARY else 1)), f"invalid arity for {op}")
            child = tuple(walk(part, depth + 1) for part in args)
            child_widths = [program[i][1] for i in child]
            if op in _BINARY or op == "not" or op in _SHIFT:
                _require(all(w == width for w in child_widths), f"width mismatch for {op}")
                if op in _SHIFT:
                    param = node["amount"]
                    _require(_integer(param, 0, (1 << 31)-1), "invalid fixed amount")
            elif op == "extract":
                hi, lo = node["hi"], node["lo"]
                _require(_integer(hi, 0, child_widths[0]-1) and _integer(lo, 0, hi) and width == hi-lo+1, "invalid extract range or output width")
                param = hi, lo
            else:
                _require(width == sum(child_widths), "invalid concat output width")
        _require(len(program) < MAX_NODES, "expression node limit")
        program.append((op, width, param, child))
        return len(program)-1

    walk(expr, 0)
    return tuple(program)


def validate(expr, inputs):
    """Return the output width; invalid input raises InvalidExpression."""
    return _compile(expr, _signature(inputs))[-1][1]


def parse_expression(payload, inputs):
    """Parse a bounded JSON string/UTF-8 bytes; reject duplicate keys and non-finite numbers."""
    _require(type(payload) in (str, bytes) and len(payload) <= MAX_JSON_BYTES, "invalid JSON payload size or type")

    def pairs(entries):
        result = {}
        for key, value in entries:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def bad_constant(_):
        raise InvalidExpression("non-finite JSON number")

    try:
        if type(payload) is bytes:
            payload = payload.decode("utf-8", "strict")
        else:
            _require(len(payload.encode("utf-8", "strict")) <= MAX_JSON_BYTES, "JSON payload exceeds byte limit")
        expr = json.loads(payload, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (UnicodeError, RecursionError, ValueError) as exc:
        raise InvalidExpression("invalid JSON expression: " + str(exc)) from exc
    validate(expr, inputs)
    return expr


def _eval(program, args):
    vals = []
    for op, w, param, child in program:
        mask = (1 << w)-1
        if op == "input": v = args[param]
        elif op == "const": v = param
        elif op == "and": v = vals[child[0]] & vals[child[1]]
        elif op == "or": v = vals[child[0]] | vals[child[1]]
        elif op == "xor": v = vals[child[0]] ^ vals[child[1]]
        elif op == "not": v = ~vals[child[0]]
        elif op == "shl": v = 0 if param >= w else vals[child[0]] << param
        elif op == "lshr": v = 0 if param >= w else vals[child[0]] >> param
        elif op in ("rotl", "rotr"):
            k = (param if op == "rotl" else -param) % w
            x = vals[child[0]]
            v = (x << k) | (x >> (w-k))
        elif op == "extract": v = vals[child[0]] >> param[1]
        elif op == "concat":
            v = 0
            for c in child:
                v = (v << program[c][1]) | vals[c]
        vals.append(v & mask)
    return vals[-1]


def evaluate(expr, assignment, inputs=None):
    """Evaluate a valid expression exactly for one assignment."""
    if inputs is None:
        # Infer widths from the leaves, with the same bounds as final validation.
        found = {}
        stack = [(expr, 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            _require(count <= MAX_NODES and depth <= MAX_DEPTH and type(node) is dict, "invalid expression structure")
            if node.get("op") == "input":
                name, width = node.get("name"), node.get("width")
                _require(type(name) is str and _integer(width, 1, MAX_WIDTH), "invalid input leaf")
                _require(found.setdefault(name, width) == width, "inconsistent input width")
            children = node.get("args", [])
            _require(type(children) is list and len(children) <= MAX_WIDTH, "invalid args")
            stack.extend((part, depth + 1) for part in children)
        inputs = found
    inputs = _signature(inputs)
    _require(isinstance(assignment, Mapping) and set(assignment) == set(inputs), "assignment must cover declared inputs exactly")
    _require(all(_integer(assignment[k], 0, (1 << w)-1) for k, w in inputs.items()), "assignment outside width")
    return _eval(_compile(expr, inputs), assignment)


def _smt_program(prg, names, prefix):
    """SMT-LIB with private generated symbols, fixed grammar, and linear-size output."""
    lines = []
    for i, (op, w, param, kids) in enumerate(prg):
        symbols = [f"{prefix}{j}" for j in kids]
        lit = lambda value: f"(_ bv{value} {w})"
        if op == "input": exp = names[param]
        elif op == "const": exp = lit(param)
        elif op in _BINARY | {"not"}: exp = f"(bv{op} {' '.join(symbols)})"
        elif op in {"shl", "lshr"}: exp = lit(0) if param >= w else f"(bv{op} {symbols[0]} {lit(param)})"
        elif op in {"rotl","rotr"}: exp = f"((_ rotate_{'left' if op=='rotl' else 'right'} {param%w}) {symbols[0]})"
        elif op == "extract": exp = f"((_ extract {param[0]} {param[1]}) {symbols[0]})"
        elif op == "concat":
            exp = symbols[-1]
            for sym in reversed(symbols[:-1]):
                exp = f"(concat {sym} {exp})"
        lines.append(f"(define-fun {prefix}{i} () (_ BitVec {w}) {exp})")
    return lines


def _witness(text, inputs):
    """Parse only the flat (get-value ...) BV output grammar and check all widths."""
    pair = re.compile(r"\(\s*v(\d+)\s+(?:(#x[0-9a-fA-F]+|#b[01]+)|\(_\s+bv(\d+)\s+(\d+)\s*\))\s*\)")
    _require(len(text) <= 100000 and text.startswith("(") and text.endswith(")"), "invalid solver witness form")
    pairs = list(pair.finditer(text[1:-1]))
    _require(not pair.sub("", text[1:-1]).strip() and len(pairs)==len(inputs), "incomplete or malformed solver witness")
    values = {}
    declared = list(inputs.items())
    for m in pairs:
        idx, based, num, width = m.groups()
        i = int(idx)
        _require(i < len(declared), "unknown solver witness symbol")
        name, w = declared[i]
        _require(name not in values, "duplicate solver witness symbol")
        if based:
            bits = (len(based)-2) * (4 if based.startswith("#x") else 1)
            val = int(based[2:], 16 if based.startswith("#x") else 2)
        else:
            bits, val = int(width), int(num)
        _require(bits == w and _integer(val, 0, (1<<w)-1), "solver witness width mismatch")
        values[name] = val
    return values


def _solver(left, right, signature, path, deadline):
    backend = "z3"
    path = os.fspath(path)
    if not os.path.isabs(path) or not os.path.isfile(path):
        return Result("UNKNOWN", backend, detail="explicit absolute solver executable unavailable")
    names = {name: f"v{i}" for i,name in enumerate(signature)}
    declarations = [f"(declare-fun {names[k]} () (_ BitVec {w}))" for k,w in signature.items()]
    base = ["(set-logic QF_BV)", "(set-option :produce-models true)"] + declarations + _smt_program(left,names,"r") + _smt_program(right,names,"c")
    base += [f"(assert (distinct r{len(left)-1} c{len(right)-1}))", "(check-sat)"]
    def run(model):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("z3", 0)
        # In-process timeout and separate wall clock guard both cap the attempt.
        body = [f"(set-option :timeout {max(1, math.ceil(remaining*1000))})"] + base
        if model:
            body.append(f"(get-value ({' '.join(names.values())}))")
        body.append("(exit)")
        proc = subprocess.run([path, "-in", "-smt2"], input="\n".join(body)+"\n", text=True,
            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=remaining, check=False)
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("z3", remaining)
        if proc.returncode or proc.stderr.strip():
            raise RuntimeError("solver process reported failure")
        return proc.stdout.strip()
    try:
        initial = run(False)
        if initial == "unsat": return Result("EQUIVALENT", backend)
        if initial == "unknown": return Result("UNKNOWN", backend, detail="solver returned unknown")
        if initial != "sat": return Result("ERROR", backend, detail="unexpected solver response")
        if not signature:
            values = {}
        else:
            status, sep, body = run(True).partition("\n")
            if status != "sat" or not sep:
                return Result("ERROR", backend, detail="solver did not reproduce sat witness")
            values = _witness(body.strip(), signature)
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("z3", 0)
        if _eval(left, values) == _eval(right, values):
            return Result("ERROR", backend, detail="solver witness did not reproduce")
        return Result("COUNTEREXAMPLE", backend, values)
    except subprocess.TimeoutExpired:
        return Result("TIMEOUT",backend)
    except (OSError,RuntimeError,ValueError) as exc:
        return Result("ERROR",backend,detail=str(exc))


def verify(reference, candidate, inputs, *, max_exhaustive_bits=12, timeout_s=5.0, z3_path=None):
    """Prove exact equality or return an explicit unverified/error outcome."""
    try:
        signature = _signature(inputs)
        _require(_integer(max_exhaustive_bits,0,MAX_EXHAUSTIVE_BITS), "invalid bit budget")
        _require(type(timeout_s) in (int,float) and 0 < timeout_s <= 3600, "invalid time budget")
        deadline = time.monotonic()+timeout_s
        left, right = _compile(reference,signature),_compile(candidate,signature)
        _require(left[-1][1]==right[-1][1], "output widths do not match")
    except InvalidExpression as exc:
        return Result("INVALID", "schema", detail=str(exc))
    if sum(signature.values()) > max_exhaustive_bits:
        if z3_path is None:
            return Result("UNKNOWN", "none", detail="exhaustive bit cap exceeded and no solver configured")
        if not isinstance(z3_path, (str,os.PathLike)):
            return Result("INVALID","schema",detail="invalid solver path")
        return _solver(left,right,signature,z3_path,deadline)
    for args in itertools.product(*(range(1<<w) for w in signature.values())):
        if time.monotonic() >= deadline:
            return Result("TIMEOUT","exhaustive")
        values = dict(zip(signature,args))
        if _eval(left,values)!=_eval(right,values):
            return Result("COUNTEREXAMPLE","exhaustive",values)
    if time.monotonic() >= deadline:
        return Result("TIMEOUT","exhaustive")
    return Result("EQUIVALENT","exhaustive")


def verify_truth_table(table_bits, candidate, input_order, *, timeout_s=5.0):
    """Exact one-bit truth table check, bit i = row i, first declared input MSB."""
    try:
        _require(type(input_order) in (list,tuple) and len(input_order)<=6, "invalid truth table input count")
        _require(all(type(k) is str for k in input_order) and len(set(input_order))==len(input_order), "invalid or duplicated truth table input")
        signature=_signature(dict.fromkeys(input_order,1))
        _require(type(timeout_s) in (int,float) and 0<timeout_s<=3600,"invalid time budget")
        deadline=time.monotonic()+timeout_s
        n=len(signature)
        _require(_integer(table_bits,0,(1<<(1<<n))-1),"truth table outside declared width")
        program=_compile(candidate,signature)
        _require(program[-1][1]==1,"truth table candidate must output one bit")
    except InvalidExpression as exc:
        return Result("INVALID","schema",detail=str(exc))
    for row,bits in enumerate(itertools.product((0,1),repeat=n)):
        if time.monotonic()>=deadline:
            return Result("TIMEOUT","truth_table")
        values=dict(zip(signature,bits))
        if ((table_bits>>row)&1)!=_eval(program,values):
            return Result("COUNTEREXAMPLE","truth_table",values)
    if time.monotonic()>=deadline:
        return Result("TIMEOUT","truth_table")
    return Result("EQUIVALENT","truth_table")
