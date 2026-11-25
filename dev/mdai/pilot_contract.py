"""Recover the captured pilot's exact action binding and exercise an offline callback."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Callable
import argparse
import json
import re

from .candidates import Candidate, build_candidate, candidate_menu, seed_graph
from .aig import compile_words, verify_aig_exact
from .w3runner import Proposal, run_match

KEYS = ("A0", "A1", "B0", "B1")
REQUEST_SHA = "54acb5abc9584002541fa52796520ee6932ada59b82cabcdcde5522d8dcd8182"
RESPONSE_SHA = "f8a281d05ed2ce6a1416de4dd22dab2aef59aa72547fb5e636a5c2b53b9080a8"
MANIFEST_SHA = "34951a26fdf0a6067558931484915ea027a797cdc9f9d51f71eee01c0ece4077"
SEEDS = ((15, 6), (11, 6))
A_TABLES = (46177, 39931)
B_EXPRESSION = (("or", 4, 2), ("xor", 8, 6))
# Unicode free-prose requirements are not executable. Only the pinned capture's
# constrained menu section below is interpreted as a historical descriptor.
A_MENU = re.compile(r"For both A cases, indexed menus are: (.*?)\. A0 table=")
WALL_SECONDS, LIMIT, VERIFY_SECONDS = 60, 20, 5
REPEATS = (0, 1, 2)
CONFIG_SHA = "0ae0b89a14cc7dbff1fb750611f1f3bb91e7769e75fa738ed28503b738358230"


def digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def write(path: Path, obj) -> None:
    with path.open("xb") as f:
        f.write(json_bytes(obj) + b"\n")


def checked(path: Path, h: str) -> bytes:
    raw = path.read_bytes()
    if digest(raw) != h:
        raise ValueError(f"evidence hash mismatch: {path.name}")
    return raw


def strict_json(raw: bytes):
    if type(raw) is not bytes or len(raw) > 8192:
        raise ValueError("expected a bounded UTF-8 response")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def constant(x):
        raise ValueError("nonfinite JSON value")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid UTF-8 or excessively nested response") from exc


def bind_response(raw: bytes, menus: dict[str, tuple[Candidate, ...]], *, tail: bool = True):
    """One invalid field rejects the entire response; never rebase index to another menu."""
    body = strict_json(raw)
    if type(body) is not dict or set(body) != set(menus) or not menus:
        raise ValueError("incorrect response keys")
    result = {}
    for key, menu in menus.items():
        picks = body[key]
        if (type(picks) is not list or not 1 <= len(picks) <= 3 or
                any(type(i) is not int or not 0 <= i < len(menu) for i in picks) or
                len(set(picks)) != len(picks)):
            raise ValueError(f"invalid indices for {key}")
        ordered = tuple(menu[i] for i in picks)
        result[key] = ordered + (tuple(menu[i] for i in range(len(menu)) if i not in picks) if tail else ())
    return result


def captured_contract(request: bytes, manifest: bytes):
    """Translate the exact captured descriptors; never recompute B index 0 from live menu."""
    if digest(request) != REQUEST_SHA or digest(manifest) != MANIFEST_SHA:
        raise ValueError("unrecognized captured prompt or development manifest")
    rows = [json.loads(line) for line in manifest.splitlines()]
    all_a = [r for r in rows if r.get("family") == "A"]
    all_b = [r for r in rows if r.get("family") == "B"]
    schedule = all_a[:2] + [next(r for r in all_b if r["outputs"][0]["width"] == w) for w in (4, 8)]
    if any(r.get("split") != "development" for r in schedule):
        raise ValueError("pilot accepts only A/B development records")
    segment = A_MENU.search(request.decode("ascii")).group(1)
    indexed = [re.fullmatch(r"(\d+):([0123]{4})", part.strip()) for part in segment.split(",")]
    if (any(m is None for m in indexed) or [int(m[1]) for m in indexed] != list(range(20))):
        raise ValueError("invalid historical A descriptor indices")
    expected_a = [tuple(map(int,m[2])) for m in indexed]
    out = {}
    for i, (key,row) in enumerate(zip(KEYS,schedule)):
        t = row["task_id"]
        if i < 2:
            summary = seed_graph(row).metrics()
            menu = candidate_menu(row)
            if (row["truth_table"] != A_TABLES[i] or (summary.and_count,summary.depth) != SEEDS[i] or
                    [(a.edit_kind,a.path,a.parameter) for a in menu] != [("shannon_order",(),x) for x in expected_a]):
                raise ValueError("exact captured A menu no longer maps to executable descriptors")
        else:
            op,w,k = B_EXPRESSION[i-2]
            x,y = (dict(op="input",width=w,name=n) for n in ("x","y"))
            ast = dict(op=op,width=w,args=[dict(op="rotl",width=w,args=[x],amount=k),y])
            if row["ast"] != ast or row["input_order"] != ["x","y"] or row["inputs"] != {"x":w,"y":w}:
                raise ValueError("captured B operation does not match manifest fixture")
            menu = (Candidate(f"{t}:neutral_double_not:root:-","neutral_double_not"),)
        out[key]=(row,menu)
    return out


def build_contract_candidate(row, menu, action):
    if action not in menu:
        raise ValueError("candidate outside the immutable prompt menu")
    if action.edit_kind == "neutral_double_not":
        if (row["family"] != "B" or len(menu) != 1 or action.path or action.parameter or
                action.candidate_id != f'{row["task_id"]}:neutral_double_not:root:-'):
            raise ValueError("invalid historical neutral action")
        ast = row["ast"]
        def negate(node):
            return dict(op="not",width=node["width"],args=[node])
        inp={k:row["inputs"][k] for k in row["input_order"]}
        return compile_words(inp,[negate(negate(ast))])
    return build_candidate(row,action)


def proposals(row, menu, ordered, *, ranked):
    return tuple(Proposal(a.candidate_id,lambda _,a=a:build_contract_candidate(row, menu, a),
                          i+1 if ranked else None) for i,a in enumerate(ordered))


def indexed_request(key, row, menu):
    """The same descriptors order the response parser and build the execution menu."""
    action = [dict(index=i,**asdict(a)) for i,a in enumerate(menu)]
    fixture = {field:row[field] for field in ("family","inputs","input_order","outputs")}
    fixture.update(truth_table=row["truth_table"] if row["family"]=="A" else None,
                   ast=row["ast"] if row["family"]=="B" else None)
    return json_bytes(dict(schema="mdai-indexed-prompt-v1",fixture={key:fixture},
                           objective="reduce AND count without increasing depth",
                           instructions=f"Return only a JSON object with key {key} and 1..3 distinct legal integer indices, best first.",
                           ordered_menu=action))


def local_provider(raw, seconds):
    """Synthetic fixture only: no scoring, model, network, or arbitrary code."""
    if seconds <= 0:
        raise TimeoutError("no budget")
    request=json.loads(raw)
    key=next(iter(request["fixture"]))
    ids=[a["index"] for a in request["ordered_menu"]]
    return json_bytes({key:list(reversed(ids[:3]))})


def timed_trial(key, row, menu, repeat, arm, path, provider: Callable=local_provider, *, timeout_s=WALL_SECONDS,
                verify=None):
    start=monotonic()
    source=seed_graph(row)
    if verify is None:
        def verify(a,b,s):
            if s <= 0:
                return None
            return {"EQUIVALENT":True,"DIFFERENT":False}.get(verify_aig_exact(a,b).status)
    ctx = {}
    def callback(remaining):
        started=monotonic()
        deadline=started+remaining
        request=indexed_request(key,row,menu)
        ctx["request_sha256"]=digest(request)
        ctx["request_utf8"]=request.decode()
        remaining=deadline-monotonic()
        ctx["remaining_at_dispatch_s"]=remaining
        if remaining <= 0:
            raise TimeoutError("budget elapsed during request preparation")
        response=provider(request,remaining)
        if monotonic() >= deadline:
            # The runner independently rejects all proposals materialized after deadline.
            # This check keeps parse/build from running on a late provider response.
            raise TimeoutError("local provider returned late")
        approved=bind_response(response,{key:menu})[key]
        if monotonic() >= deadline:
            raise TimeoutError("budget elapsed while binding response")
        ctx["response_sha256"]=digest(response)
        ctx["response_utf8"]=response.decode("utf-8")
        ctx["bound_action_ids"]=[a.candidate_id for a in approved]
        return proposals(row,menu,approved,ranked=True)
    r=run_match(source,task_id=row["task_id"],arm=arm,repeat=repeat,seed=1000+repeat,
                proposals=proposals(row,menu,menu,ranked=False) if arm=="baseline" else (),
                timeout_s=timeout_s,candidate_limit=LIMIT,verify=verify,evidence_dir=path,started_monotonic=start,
                proposal_source=callback if arm=="live" else None)
    return r,ctx


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",type=Path,required=True)
    opts=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    evidence=root/"evidence/week3/model_pilot"
    cfg=json.loads(checked(root/"pilot_contract_config.json",CONFIG_SHA))
    contract=captured_contract(checked(evidence/"request.txt",REQUEST_SHA),
      checked(root.parent/"records/week2/week2_smoke/dataset_manifest.jsonl",MANIFEST_SHA))
    response=checked(evidence/"response.txt",RESPONSE_SHA)
    original_selected=bind_response(response,{k:m for k,(_,m) in contract.items()},tail=False)
    out=opts.out.resolve()
    if out != (root/"evidence/pilot_contract_repair").resolve():
        raise ValueError("use the dedicated non-overwriting pilot evidence location")
    out.mkdir(parents=True,exist_ok=False)
    write(out/"preregistered.json",dict(config=cfg,config_sha256=CONFIG_SHA,model_provider="NONE_OFFLINE_SYNTHETIC"))
    history={}
    for key,(row,menu) in contract.items():
        source=seed_graph(row)
        selected=[]
        for a in original_selected[key]:
            c=build_contract_candidate(row,menu,a)
            verdict=verify_aig_exact(source,c)
            if verdict.status!="EQUIVALENT":
                raise ValueError("unverified historical index translation")
            selected.append(dict(index=menu.index(a),action_id=a.candidate_id,
                                 ordered_port_proof=verdict.status,
                                 metrics=asdict(c.metrics()),neutral_metrics=(source.metrics()==c.metrics())))
        history[key]=dict(task_id=row["task_id"],prompt_menu_size=len(menu),current_unbound_menu_size=len(candidate_menu(row)),
                          current_unbound_index0=candidate_menu(row)[0].candidate_id,
                          ranked_actions=selected)
    write(out/"historical_binding.json",dict(captured_request_sha256=REQUEST_SHA,captured_response_sha256=RESPONSE_SHA,
          original_receipts_modified=False,actual_native_replay_scored=False,cases=history))
    statuses=Counter()
    trials=[]
    for k,(row,menu) in contract.items():
        for repeat in REPEATS:
            for arm in ("baseline","live"):
                label="offline-synthetic" if arm=="live" else "no-model"
                stem=f"{k}-{repeat}-{label}"
                r,ctx=timed_trial(k,row,menu,repeat,arm,out/stem)
                write(out/stem/"contract.json",dict(arm_label=label,model_calls=0,
                     ordered_prompt_menu=[asdict(a) for a in menu],contract_context=ctx))
                # Same universe and counts, but an explicitly different order.
                attempts=r["attempts"]
                applied=[a["id"] for a in attempts]
                if len(applied)!=len(menu) or set(applied)!={a.candidate_id for a in menu}:
                    raise ValueError("runner did not consume exactly the frozen menu")
                if arm=="live" and applied!=ctx["bound_action_ids"]:
                    raise ValueError("execution index order differs from bound prompt")
                if r["terminal_status"]!="PORTFOLIO_EXHAUSTED" or not 0<=r["elapsed_s"]<=WALL_SECONDS:
                    raise ValueError("offline trial exceeded preregistered bounds")
                st=Counter(a["status"] for a in attempts)
                if not set(st)<={"DUPLICATE","VERIFIED","DEPTH_CAP"}:
                    raise ValueError("unexpected offline result")
                statuses.update(st)
                trials.append(dict(key=k,arm=label,repeat=repeat,attempts=len(applied),verified_statuses=dict(st),
                    charged_seed_s=r["charged_seed_read_s"],charged_callback_s=r["proposal_callback_elapsed_s"],
                    elapsed_total_s=r["elapsed_s"],runner_outcome_sha256=r["outcome_sha256"],
                    receipt_sha256=digest((out/stem/"receipt.json").read_bytes()),
                    contract_receipt_sha256=digest((out/stem/"contract.json").read_bytes())))
    write(out/"summary.json",dict(schema="mdai-ab-ordered-pilot-repair-result-v1",
        state="OFFLINE_CONTRACT_VERIFIED", formal_gate3_pass=False, real_live_model_comparison=False,
        model_calls=0, holdout_accessed=False, ranker_fit=False, actual_cost_or_latency_claimed=False,
        fixtures=4,historical_bound_fixtures=list(history),historical_b0_b1_action="neutral_double_not",
        offline_synthetic_runs=sum(t["arm"]=="offline-synthetic" for t in trials),
        no_model_runs=sum(t["arm"]=="no-model" for t in trials), attempt_statuses=dict(statuses),
        trial_design="two-arm synthetic contract exercise; no matched ABC arm in this diagnostic",
        historical_binding_sha256=digest((out/"historical_binding.json").read_bytes()),
        preregistered_sha256=digest((out/"preregistered.json").read_bytes()), trials=trials))
    print(f"{len(trials)} offline/no-model trials, {sum(statuses.values())} attempts; no model calls or gate promotion.")


if __name__=="__main__":
    main()
