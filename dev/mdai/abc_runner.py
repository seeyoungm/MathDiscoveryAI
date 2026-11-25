"""Fixed, bounded Berkeley ABC adapter for combinational development fixtures.

ABC is an untrusted producer of *nominations*, not an equivalence oracle.
No shell, user-specified ABC commands, startup rc, or alternate input formats.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from time import monotonic
from typing import Callable, Mapping

from .aig import AIG, Metrics

RECIPES: Mapping[str, tuple[str, ...]] = {
    "strash_only": ("strash",),
    "balance_rewrite": ("strash", "balance", "rewrite", "balance"),
    "rewrite_refactor": ("strash", "balance", "rewrite", "refactor", "balance"),
}
NEXTSTAGE_RECIPES = {**RECIPES, "resub_balance": ("strash", "balance", "resub -K 10 -N 1", "balance")}
MILESTONE_RECIPES = {**NEXTSTAGE_RECIPES, "lut_mfs": ("strash", "dch", "if -K 4", "mfs2", "strash", "balance")}
MAX_BYTES = 2_000_000
MAX_ITEMS = 65_536
# The child enforces an upper bound before exec, avoiding preexec_fn.
_CHILD = ("import os,resource,sys;"
          "n=int(sys.argv[1]);resource.setrlimit(resource.RLIMIT_FSIZE,(n,n));"
          "os.execv(sys.argv[2],sys.argv[2:])")
_ERROR = re.compile(r"(?im)^\s*(?:error\b|unknown command\b|cannot\b|can't\b|assertion\b|warning:.*(?:failed|cannot))")

@dataclass(frozen=True)
class ABCResult:
    status: str
    graph: AIG | None
    input_metrics: Metrics
    output_metrics: Metrics | None
    equivalent: bool | None
    eligible: bool
    elapsed_s: float
    detail: str
    evidence_dir: str | None

def _labels(i: int, o: int) -> list[str]:
    return [f"i{j} MDI{j}" for j in range(i)] + [f"o{j} MDO{j}" for j in range(o)]

def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("negative AIGER delta")
    out = bytearray()
    while n >= 128:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)

def encode_binary(graph: AIG, *, include_labels: bool = True) -> bytes:
    lines = graph.to_aag(symbols=False).splitlines()
    m, i, l, o, a = map(int, lines[0].split()[1:])
    if l or m != i + a or i + o + a > MAX_ITEMS or o < 1:
        raise ValueError("unsupported input AIG structure")
    outs = lines[1+i:1+i+o]
    result = bytearray(("\n".join([f"aig {m} {i} 0 {o} {a}", *outs]) + "\n").encode("ascii"))
    for j, line in enumerate(lines[1+i+o:], i + 1):
        lhs, rhs0, rhs1 = map(int, line.split())
        if lhs != 2*j or not 0 <= rhs1 <= rhs0 < lhs:
            raise ValueError("unsorted AIGER operands")
        result.extend(_varint(lhs - rhs0))
        result.extend(_varint(rhs0 - rhs1))
    if include_labels:
        result.extend(("\n".join(_labels(i, o)) + "\n").encode("ascii"))
    if len(result) > MAX_BYTES:
        raise ValueError("input AIG too large")
    return bytes(result)

def decode_binary(data: bytes, *, require_labels: bool = False) -> AIG:
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ValueError("oversized or invalid binary AIGER")
    cursor = 0
    def line() -> bytes:
        nonlocal cursor
        end = data.find(b"\n", cursor)
        if end < 0 or end - cursor > 120:
            raise ValueError("invalid binary AIGER text row")
        start, cursor = cursor, end + 1
        return data[start:end]
    header = line()
    if not re.fullmatch(rb"aig(?: [0-9]{1,6}){5}(?: 0){0,4}", header):
        raise ValueError("unsupported binary AIGER header or properties")
    m, i, l, o, a, *_ = map(int, header.split()[1:])
    if l or m != i + a or i + o + a > MAX_ITEMS or o < 1:
        raise ValueError("unsupported binary AIGER structure")
    def delta() -> int:
        nonlocal cursor
        n = 0
        for shift in (0, 7, 14):
            if cursor >= len(data):
                raise ValueError("truncated AIGER delta")
            b = data[cursor]
            cursor += 1
            n |= (b & 127) << shift
            if not b & 128:
                if shift and b == 0:
                    raise ValueError("noncanonical AIGER delta")
                return n
        raise ValueError("oversized AIGER delta")
    outs = []
    for _ in range(o):
        raw = line()
        if not re.fullmatch(rb"[0-9]{1,6}", raw) or int(raw) > 2*m+1:
            raise ValueError("invalid AIGER output")
        outs.append(raw.decode("ascii"))
    gates = []
    for v in range(i + 1, m + 1):
        lhs = 2*v
        first_delta = delta()
        rhs0 = lhs - first_delta
        rhs1 = rhs0 - delta()
        if not 0 <= rhs1 <= rhs0 < lhs:
            raise ValueError("invalid or non-topological AIGER gate")
        gates.append(f"{lhs} {rhs0} {rhs1}")
    seen: dict[str, str] = {}
    while cursor < len(data):
        # AIGER comment bytes are opaque and cannot affect parsing after c.
        if data[cursor:cursor+2] == b"c\n":
            break
        raw = line()
        match = re.fullmatch(rb"([io])([0-9]{1,5}) ([ -~]+)", raw)
        if not match:
            raise ValueError("invalid AIGER symbol section")
        k, j, label = match.groups()
        index = int(j)
        kind = k.decode("ascii")
        key = kind + str(index)
        if key in seen or index >= (i if kind == "i" else o):
            raise ValueError("duplicate or out-of-range AIGER symbol")
        seen[key] = label.decode("ascii")
    expected = dict(x.split(" ", 1) for x in _labels(i, o))
    if require_labels and seen != expected:
        raise ValueError("ABC changed or dropped ordered I/O labels")
    rows = [f"aag {m} {i} 0 {o} {a}", *(str(2*x) for x in range(1, i+1)), *outs, *gates]
    return AIG.from_aag("\n".join(rows) + "\n", max_bytes=MAX_BYTES, max_items=MAX_ITEMS)

def run_abc(graph: AIG, *, abc_path: str | Path, recipe: str = "balance_rewrite",
            timeout_s: float = 5.0,
            verify: Callable[[AIG, AIG, float], bool | None] | None = None,
            evidence_dir: str | Path | None = None,
            nextstage_menu: bool = False, milestone_menu: bool = False) -> ABCResult:
    """A verifier callback returns True only if exact ordered-port equivalence is proved.

    Args are source AIG, nominated AIG, and the remaining seconds (at most 5).
    No evidence is overwritten. Only equivalent on-time graphs may be nominated;
    eligibility additionally requires the original reachable-depth cap.
    """
    start = monotonic()
    if milestone_menu and nextstage_menu:
        raise ValueError("choose exactly one extended fixed menu")
    recipes = MILESTONE_RECIPES if milestone_menu else NEXTSTAGE_RECIPES if nextstage_menu else RECIPES
    if recipe not in recipes:
        raise ValueError("unknown fixed ABC recipe")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (float, int)) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 60:
        raise ValueError("ABC timeout must be in (0, 60] seconds")
    path = Path(abc_path)
    if not path.is_absolute():
        raise ValueError("ABC executable path must be absolute")
    path = path.resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("ABC executable unavailable")
    cmd = "; ".join(("read_aiger input.aig", *recipes[recipe], "write_aiger -s output.aig"))
    deadline = start + timeout_s
    initial = graph.metrics()
    out_metrics: Metrics | None = None
    evidence = Path(evidence_dir).absolute() if evidence_dir is not None else None
    def remaining() -> float:
        return deadline - monotonic()
    def finish(status: str, detail: str, nominated: AIG | None = None,
               equivalent: bool | None = None) -> ABCResult:
        elapsed = monotonic() - start
        if elapsed >= timeout_s and status != "TIMEOUT":
            status, detail, nominated, equivalent = "TIMEOUT", "late result rejected", None, None
        eligible = bool(status == "VERIFIED" and out_metrics and out_metrics.depth <= initial.depth)
        if evidence is not None:
            receipt = {
                "recipe": recipe, "literal_commands": cmd, "abc_path": str(path),
                "status": status, "detail": detail, "eligible": eligible,
                "equivalent": equivalent, "elapsed_s": elapsed, "budget_s": timeout_s,
                "input_metrics": vars(initial),
                "output_metrics": vars(out_metrics) if out_metrics else None,
            }
            (evidence/"receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True)+"\n", encoding="utf-8")
        return ABCResult(status, nominated, initial, out_metrics, equivalent, eligible, elapsed, detail, str(evidence) if evidence is not None else None)
    def execute(base: Path) -> ABCResult:
        nonlocal out_metrics
        (base/"input.aig").write_bytes(encode_binary(graph))
        argv = [sys.executable, "-I", "-c", _CHILD, str(MAX_BYTES), str(path), "-s", "-c", cmd]
        env = {"PATH": os.defpath, "LC_ALL": "C", "HOME": str(base)}
        try:
            if remaining() <= 0:
                return finish("TIMEOUT", "budget exhausted before ABC invocation")
            with (base/"stdout.txt").open("wb") as out, (base/"stderr.txt").open("wb") as err:
                run = subprocess.run(argv, cwd=base, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, check=False, timeout=remaining())
        except subprocess.TimeoutExpired:
            return finish("TIMEOUT", "ABC exceeded the task budget")
        except OSError as exc:
            return finish("TOOL_ERROR", f"could not execute ABC ({type(exc).__name__})")
        logs = [(base/p).read_text(encoding="utf-8", errors="replace") for p in ("stdout.txt", "stderr.txt")]
        if run.returncode or any(_ERROR.search(log) for log in logs):
            return finish("TOOL_ERROR", f"ABC error or exit {run.returncode}")
        result = base/"output.aig"
        if not result.is_file() or result.is_symlink():
            return finish("TOOL_ERROR", "missing ABC output")
        try:
            with result.open("rb") as file:
                other = decode_binary(file.read(MAX_BYTES + 1), require_labels=True)
            if (len(other.input_literals), len(other.outputs)) != (len(graph.input_literals), len(graph.outputs)):
                raise ValueError("ABC changed I/O counts")
            # Return the original bit names while preserving the raw ABC gate structure.
            aag = other.to_aag(symbols=False)
            original_labels = "".join(f"i{j} {name}\n" for j, name in enumerate(graph.input_names))
            other = AIG.from_aag(aag + original_labels)
            out_metrics = other.metrics()
        except (ValueError, OSError) as exc:
            return finish("INVALID_OUTPUT", str(exc))
        try:
            validate_deadline = monotonic() + min(5.0, max(0.0, remaining()))
            if verify is not None and remaining() > 0:
                verdict = verify(graph, other, max(0.0, validate_deadline - monotonic()))
            elif len(graph.input_literals) <= 12:
                verdict = True
                for bits in product((0, 1), repeat=len(graph.input_literals)):
                    if monotonic() >= validate_deadline:
                        return finish("TIMEOUT", "equivalence check exceeded its allowance")
                    if graph.evaluate(bits) != other.evaluate(bits):
                        verdict = False
                        break
            else:
                verdict = None
            if monotonic() >= validate_deadline:
                return finish("TIMEOUT", "late equivalence verdict rejected")
        except Exception as exc:
            return finish("VERIFY_ERROR", f"verifier raised {type(exc).__name__}")
        if verdict is False:
            return finish("NOT_EQUIVALENT", "output differs for ordered inputs", equivalent=False)
        if verdict is not True:
            return finish("UNVERIFIED", "no exact ordered-port equivalence verdict")
        return finish("VERIFIED", "depth cap respected" if out_metrics.depth <= initial.depth else "depth cap exceeded", other, True)
    if evidence is not None:
        evidence.mkdir(parents=True, exist_ok=False)
        return execute(evidence)
    with tempfile.TemporaryDirectory(prefix="mdai-abc-") as temporary:
        return execute(Path(temporary))
