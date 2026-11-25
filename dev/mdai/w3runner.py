"""Bounded development-only runner, with explicitly distinct live/replay arms."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from time import monotonic
from typing import Callable, Iterable, Mapping

from .abc_runner import RECIPES, run_abc
from .aig import AIG, verify_aig_exact

ABC_ORDER = tuple(RECIPES)
ARMS = {"abc", "baseline", "replay", "live"}


@dataclass(frozen=True)
class Proposal:
    id: str
    build: Callable[[float], AIG]
    rank: int | None = None


def _json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _fp(graph):
    """Structural fingerprint keyed by declared port and ordered output positions."""
    codes = {0: _digest(b"0")}
    # AIG serialisation has gone through reachability cleanup and topological renumbering.
    rows = graph.to_aag(symbols=False).splitlines()
    _, m, i, l, o, a = rows[0].split()
    i, o = int(i), int(o)
    for n, row in enumerate(rows[1 : i + 1]):
        codes[int(row) // 2] = _digest(f"input:{n}".encode())
    def literal(x):
        return str(x & 1) + codes[x // 2]
    for row in rows[1 + i + o :]:
        g, x, y = map(int, row.split())
        codes[g // 2] = _digest(("and:" + "".join(sorted((literal(x), literal(y))))).encode())
    metrics = graph.metrics()
    return _digest(_json([i, metrics.and_count, metrics.depth,
                          [literal(int(row)) for row in rows[1 + i : 1 + i + o]]]).encode())


def _write_new(path, row):
    with path.open("x", encoding="utf-8") as f:
        f.write(_json(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _reason(base, outcome, detail=None, *, equivalent=None, metrics=None, fp=None):
    base.update(status=outcome, equivalent=equivalent, detail=detail)
    if metrics is not None:
        base.update(candidate_and_count=metrics.and_count,
                    candidate_depth=metrics.depth)
    if fp is not None:
        base["candidate_fingerprint"] = fp
    return base


def run_match(
    graph: AIG, *, task_id: str, arm: str, repeat: int, seed: int,
    proposals: Iterable[Proposal] = (), abc_path=None, timeout_s: float = 60.0,
    candidate_limit: int = 20,
    verify: Callable[[AIG, AIG, float], bool | None] | None = None,
    evidence_dir=None, replay_identity: Mapping | None = None,
    launch_charge_s: float | None = None, started_monotonic: float | None = None,
    proposal_source: Callable[[float], Iterable[Proposal]] | None = None,
) -> dict:
    """Run one fixed task/arm/repeat. Never re-open an incomplete receipt.

    For an already loaded common seed, pass measured loading time as launch_charge_s,
    or pass the real monotonic origin before reading it as started_monotonic, not both.
    Callbacks must honor provided timeouts; late returns are rejected. Replay costs
    exclude live generation by definition. Live ordering requires an in-budget callback.
    """
    arrival = monotonic()
    if arm not in ARMS or not isinstance(task_id, str) or not task_id:
        raise ValueError("invalid task or arm")
    if type(repeat) is not int or repeat < 0 or type(seed) is not int:
        raise ValueError("invalid repeat/seed")
    if (type(candidate_limit) is not int or not 1 <= candidate_limit <= 20 or
        type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 60):
        raise ValueError("invalid fixed limits")
    if launch_charge_s is not None and started_monotonic is not None:
        raise ValueError("specify either measured charge or start, not both")
    if launch_charge_s is not None:
        if type(launch_charge_s) not in (int, float) or not math.isfinite(launch_charge_s) or launch_charge_s < 0:
            raise ValueError("invalid seed-read charge")
    if started_monotonic is not None:
        if type(started_monotonic) not in (int, float) or not math.isfinite(started_monotonic) or not 0 <= started_monotonic <= arrival:
            raise ValueError("invalid monotonic origin")
    if arm == "abc" and abc_path is None:
        raise ValueError("ABC arm needs an explicit executable")
    if arm == "replay" and (not replay_identity or not all(k in replay_identity for k in
             ("source", "model", "request_sha256", "response_sha256"))):
        raise ValueError("replay requires model and request/response identity")
    if arm != "replay" and replay_identity is not None:
        raise ValueError("replay identity applies only to replay")
    if (arm == "live") != (proposal_source is not None):
        raise ValueError("live arm requires an in-budget proposal callback")
    start = started_monotonic if started_monotonic is not None else arrival - (launch_charge_s or 0.0)
    deadline = start + timeout_s
    base_metrics = graph.metrics()
    initial_fp = _fp(graph)
    dirpath = Path(evidence_dir) if evidence_dir is not None else None
    portfolio = []
    # Collect bounded, prebuilt named candidates once. Count all attempted entries, including duplicates.
    if arm not in ("abc", "live"):
        for p in proposals:
            if len(portfolio) >= 1000:
                raise ValueError("oversized proposal menu")
            if not isinstance(p, Proposal) or not isinstance(p.id, str) or not p.id or not callable(p.build):
                raise ValueError("invalid proposal")
            if arm == "replay" and (type(p.rank) is not int or p.rank < 1):
                raise ValueError("replay requires positive rank")
            portfolio.append(p)
    key = dict(task_id=task_id, arm=arm, repeat=repeat, seed=seed, max_candidates=candidate_limit,
               budget_seconds=timeout_s, initial_fingerprint=initial_fp,
               abc_path=str(Path(abc_path)) if abc_path is not None else None,
               abc_recipes=list(ABC_ORDER) if arm=="abc" else None,
               portfolio_metadata=[[p.id, p.rank if arm=="replay" else None] for p in portfolio],
               replay_identity=dict(replay_identity) if replay_identity else None)
    mode = {
        "abc":"abc_native", "baseline":"seeded_no_ai", "replay":"recorded_model_rank_replay",
        "live":"live_callback_in_budget",
    }[arm]
    if dirpath is not None:
        try:
            dirpath.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            stored = json.loads((dirpath/"started.json").read_text(encoding="utf-8"))
            if stored["key"] != key:
                raise ValueError("resume identity mismatch")
            finalpath=dirpath/"receipt.json"
            if finalpath.exists():
                recorded = json.loads(finalpath.read_text(encoding="utf-8"))
                if recorded["key"] != key:
                    raise ValueError("receipt identity mismatch")
                return recorded
            # A monotonic deadline cannot be imported across processes reliably.
            # Preserve original attempt log; no retries/top-up after an interruption.
            return {"key": key, "mode": mode, "terminal_status":"INTERRUPTED_NO_RETRY",
                    "budget_preserved_no_restart": True}
        _write_new(dirpath/"started.json", {"key":key,"mode":mode,"started_monotonic":start,
                                          "budget_deadline_monotonic":deadline})

    def remaining():
        return max(0.0, deadline-monotonic())

    def record(kind, payload):
        if dirpath is not None:
            with (dirpath/"attempts.jsonl").open("a", encoding="utf-8") as f:
                f.write(_json({"kind":kind, "data":payload})+"\n")
                f.flush()
                os.fsync(f.fileno())

    results = []
    selected = None
    seen_ids = set()
    seen_fp = {initial_fp}
    end_reason = None
    setup_elapsed = max(0.0, arrival-start)
    proposal_latency_s = 0.0
    if arm == "live" and remaining() > 0:
        call_started = monotonic()
        try:
            for p in proposal_source(remaining()):
                if remaining() <= 0:
                    end_reason="TIMEOUT"
                    break
                if len(portfolio) >= 1000 or not isinstance(p, Proposal) or not isinstance(p.id,str) or not p.id or not callable(p.build) or type(p.rank) is not int or p.rank < 1:
                    raise ValueError("invalid live proposal portfolio")
                portfolio.append(p)
        except Exception as exc:
            end_reason = "PROPOSAL_SOURCE_ERROR"
            record("proposal_source_error",{"type":type(exc).__name__})
        proposal_latency_s = monotonic()-call_started

    if arm == "abc":
        portfolio = [Proposal(recipe, lambda _: None) for recipe in ABC_ORDER]
    elif arm == "baseline":
        portfolio.sort(key=lambda p: (_digest(_json([task_id, seed, p.id]).encode()),p.id))
    else:
        portfolio.sort(key=lambda p: (p.rank, p.id))

    def prove(left,right,allowance):
        if verify is not None:
            return verify(left,right,allowance)
        verdict = verify_aig_exact(left,right).status
        return {"EQUIVALENT": True, "DIFFERENT": False}.get(verdict)

    for number, p in enumerate(portfolio[:candidate_limit], start=1):
        if end_reason is not None or remaining() <= 0:
            break
        attempt_start = monotonic()
        row = dict(number=number, id=p.id, action=p.id, rank=p.rank if arm in ("replay","live") else None,
                   pre_action={"input_bits":len(graph.input_literals),"output_bits":len(graph.outputs),
                               "source_and_count":base_metrics.and_count,"source_depth":base_metrics.depth})
        record("attempt_started", row)
        if remaining() <= 0:
            row = _reason(row,"TIMEOUT","budget expired before candidate preparation")
        elif p.id in seen_ids:
            row = _reason(row, "DUPLICATE", "repeated proposal id")
        else:
            seen_ids.add(p.id)
            try:
                if arm == "abc":
                    abc_limit = min(remaining(), timeout_s / len(ABC_ORDER))
                    rr = run_abc(graph, abc_path=abc_path, recipe=p.id, timeout_s=abc_limit,
                                 evidence_dir=(dirpath/f"abc-{number:02}") if dirpath is not None else None,
                                 verify=prove)
                    candidate = rr.graph
                    checked = rr.equivalent
                    if rr.status != "VERIFIED":
                        row = _reason(row,rr.status,rr.detail,equivalent=rr.equivalent,metrics=rr.output_metrics)
                else:
                    candidate = p.build(remaining())
                    checked = None
                if "status" not in row:
                    if remaining() <= 0:
                        row = _reason(row,"TIMEOUT","late preparation")
                    elif not isinstance(candidate,AIG):
                        row = _reason(row,"INVALID","proposal did not produce an AIG")
                    elif candidate.input_names != graph.input_names or len(candidate.outputs)!=len(graph.outputs):
                        row = _reason(row,"INVALID_INTERFACE","wrong declared inputs/output count")
                    else:
                        fingerprint= _fp(candidate)
                        metrics=candidate.metrics()
                        if fingerprint in seen_fp:
                            row = _reason(row,"DUPLICATE","repeated reachable structure",equivalent=True if checked is True else None,metrics=metrics,fp=fingerprint)
                        else:
                            seen_fp.add(fingerprint)
                            allowed = min(5.0, remaining())
                            vt = monotonic()
                            if allowed <= 0:
                                row = _reason(row,"TIMEOUT","budget expired before verification",metrics=metrics,fp=fingerprint)
                            elif checked is not True:
                                checked = prove(graph,candidate,allowed)
                            if "status" not in row:
                                if remaining() <= 0 or monotonic()-vt>=allowed:
                                    row = _reason(row,"TIMEOUT","late verification",metrics=metrics,fp=fingerprint)
                                elif checked is False:
                                    row = _reason(row,"NOT_EQUIVALENT",equivalent=False,metrics=metrics,fp=fingerprint)
                                elif checked is not True:
                                    row = _reason(row,"UNVERIFIED",metrics=metrics,fp=fingerprint)
                                elif metrics.depth>base_metrics.depth:
                                    row = _reason(row,"DEPTH_CAP",equivalent=True,metrics=metrics,fp=fingerprint)
                                else:
                                    row = _reason(row,"VERIFIED",equivalent=True,metrics=metrics,fp=fingerprint)
            except Exception as exc:
                row = _reason(row,"TOOL_ERROR" if arm=="abc" else "ERROR",type(exc).__name__)
        if remaining() <= 0:
            row["status"]="TIMEOUT"
            row["equivalent"]=None
            row["detail"]="late result rejected"
        row["elapsed_s"] = monotonic()-attempt_start
        metrics = row.get("candidate_and_count")
        row["label"] = {
            "equivalent": row["equivalent"],
            "depth_ok": row.get("candidate_depth",float("inf")) <= base_metrics.depth if row["equivalent"] is True else None,
            "and_reduction": base_metrics.and_count-metrics if row["equivalent"] is True else None,
        }
        row["eligible"] = row["status"]=="VERIFIED"
        row["strict_improvement"] = row["eligible"] and metrics is not None and metrics<base_metrics.and_count
        if row["strict_improvement"] and (selected is None or
                (metrics,row["candidate_depth"],row["id"]) <
                (selected["candidate_and_count"],selected["candidate_depth"],selected["id"])):
            selected = row
        results.append(row)
        record("attempt_finished",row)
    finish_time=monotonic()
    terminal = end_reason or ("TIMEOUT" if finish_time>=deadline else
                "CANDIDATE_LIMIT" if len(portfolio)>candidate_limit else "PORTFOLIO_EXHAUSTED")
    identity = {"key":key,"mode":mode,"terminal_status":terminal,
                "attempts":[{k:v for k,v in x.items() if k!="elapsed_s"} for x in results],
                "selected_id":selected["id"] if selected else None}
    receipt= dict(identity, attempts=results, started_monotonic=start,
                  audit_complete_monotonic=finish_time, elapsed_s=max(0.0,finish_time-start),
                  charged_seed_read_s=setup_elapsed, budget_deadline_monotonic=deadline,
                  proposal_callback_elapsed_s=proposal_latency_s,
                  attempted_candidates=len(results), original_metrics=vars(base_metrics),
                  outcome_sha256=_digest(_json(identity).encode()),
                  verified_improvement=selected is not None,
                  best_and_count=selected["candidate_and_count"] if selected else base_metrics.and_count,
                  excludes_live_generation_cost=arm=="replay")
    if dirpath is not None:
        temporary = dirpath/"receipt.partial"
        _write_new(temporary, receipt)
        os.replace(temporary,dirpath/"receipt.json")
    return receipt
