"""Replayable fixed-menu timing rehearsal. No model, external data, or network."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic_ns
from .aig import verify_aig_exact
from .candidates import candidate_menu, seed_graph, build_candidate
from .w3verify import verify_z3

ROOT = Path(__file__).resolve().parents[2]
BUDGET = 800_000_000
ATTEMPT = 120_000_000
LIMIT = 8
REPEATS = 3
ARMS = ("canonical", "structure")
SCHEMA = "mdai-anytime-local-rehearsal-v1"


def digest(raw):
    return sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fixtures():
    def A(i, table):
        return dict(task_id=f"toy-A{i}",family="A",split="development",inputs=dict.fromkeys("abcd",1),
            input_order=list("abcd"),total_input_bits=4,representation="truth_table_lsb0",
            outputs=[{"width":1}],truth_table=table)
    def v(name):
        return dict(op="input",width=4,name=name)
    def op(name,*args,**kw):
        return dict(op=name,width=4,args=list(args),**kw)
    def B(i, ast):
        return dict(task_id=f"toy-B{i}",family="B",split="development",inputs={"x":4,"y":4},
            input_order=["x","y"],total_input_bits=8,representation="bit_ast",outputs=[{"width":4}],ast=ast)
    x,y=v("x"),v("y")
    rot=op("rotl",x,amount=1)
    return (A(1,0x36ab),A(2,0x12ef),A(3,0x5a34),
            B(1,op("and",x,op("or",x,y))),
            B(2,op("xor",op("xor",x,y),x)),
            B(3,op("or",op("and",x,y),op("and",x,rot))))


def menu_order(menu, arm):
    if arm not in ARMS:
        raise ValueError("unknown arm")
    if arm == "canonical":
        return menu
    prefixes = ("remove_", "absorption", "factor_", "xor_cancel_", "idempotent")
    def priority(p):
        if p.edit_kind == "shannon_order":
            return tuple(-v for v in p.parameter)
        return (not p.edit_kind.startswith(prefixes), len(p.path))
    return tuple(sorted(menu,key=priority))


def disposition(initial, ms, exact, smt, emitted_ns, proof_ns):
    if emitted_ns > BUDGET:
        return "after_deadline"
    if proof_ns > ATTEMPT:
        return "proof_timeout"
    if exact == "DIFFERENT" or smt is False:
        return "not_equivalent"
    if exact != "EQUIVALENT" or smt is not True:
        return "unverified"
    if ms["depth"] > initial["depth"]:
        return "depth_rejected"
    return "feasible"


def summarize(initial, events, cutoff=BUDGET):
    """Offline projection; no proposal changes a result before proved in budget."""
    target=initial-1
    best=initial
    reached=None
    points=[[0,initial]]
    for e in events:
        if e["end_ns"] > cutoff or e["status"] != "feasible":
            continue
        n=e["metrics"]["and_count"]
        if n < best:
            best=n
            points.append([e["end_ns"],n])
            if reached is None and n <= target:
                reached=e["end_ns"]
    return dict(best=best,target_ns=reached,points=points)


def run_arm(case, seed, arm, repeat, *, clock=monotonic_ns, check_smt=verify_z3):
    initial=asdict(seed.metrics())
    start=clock()
    menu=candidate_menu(case)
    ordered=menu_order(menu,arm)
    setup=clock()-start
    events=[]
    for p in ordered[:LIMIT]:
        begin=clock()
        if begin-start >= BUDGET:
            break
        g=build_candidate(case,p)
        ms=asdict(g.metrics())
        fingerprint=digest(g.to_aag().encode())
        proof=clock()
        cap=min(begin+ATTEMPT,start+BUDGET)
        exact=verify_aig_exact(seed,g).status if proof<cap else None
        exact_done=clock()
        smt=check_smt(seed,g,(cap-exact_done)/1e9) if exact_done<cap and exact=="EQUIVALENT" else None
        end=clock()
        status=disposition(initial,ms,exact,smt,end-start,end-begin)
        events.append(dict(candidate=p.candidate_id,kind=p.edit_kind,path=list(p.path),parameter=list(p.parameter),
            fingerprint=fingerprint,metrics=ms,exact=exact,smt=smt,status=status,
            start_ns=begin-start,proof_ns=proof-start,exact_ns=exact_done-start,end_ns=end-start))
    return dict(arm=arm,repeat=repeat,setup_ns=setup,menu_sha256=digest(canonical([p.candidate_id for p in menu])),
        elapsed_ns=clock()-start,events=events)


def replay(payload):
    """Rebuild every reported candidate and exact check independently of timing;
    verify receipt schema/order; never judge speed beyond this host's rehearsal."""
    fixed=dict(schema=SCHEMA,model_calls=0,holdout=False,budget_ns=BUDGET,attempt_ns=ATTEMPT,limit=LIMIT,repeats=REPEATS)
    for k,v in fixed.items():
        if type(payload.get(k)) is not type(v) or payload[k]!=v:
            raise ValueError(f"unexpected contract: {k}")
    rs=payload["cases"]
    if not isinstance(rs,list) or len(rs)!=len(fixtures()):
        raise ValueError("wrong fixtures")
    result=[]
    for idx,(case,row) in enumerate(zip(fixtures(),rs)):
        seed=seed_graph(case)
        base=asdict(seed.metrics())
        expected=dict(id=case["task_id"],family=case["family"],initial=base,case_sha256=digest(canonical(case)))
        for k,v in expected.items():
            if row[k]!=v:
                raise ValueError("case mismatch")
        menu=candidate_menu(case)
        mh=digest(canonical([p.candidate_id for p in menu]))
        keys=[(repeat,arm) for repeat in range(REPEATS)
                for arm in (ARMS if (idx+repeat)%2==0 else ARMS[::-1])]
        runs=row["runs"]
        if [(r["repeat"],r["arm"]) for r in runs]!=keys:
            raise ValueError("run pairing/order mismatch")
        new_runs=[]
        for run in runs:
            arm=run["arm"]
            events=run["events"]
            planned=menu_order(menu,arm)[:LIMIT]
            if run["menu_sha256"]!=mh or len(events)>len(planned):
                raise ValueError("menu mismatch")
            prev=run["setup_ns"]
            if type(prev) is not int or prev<0 or type(run["elapsed_ns"]) is not int:
                raise ValueError("invalid setup time")
            for e,p in zip(events,planned):
                t=[e[k] for k in ("start_ns","proof_ns","exact_ns","end_ns")]
                if any(type(x) is not int for x in t) or not prev<=t[0]<BUDGET or t!=sorted(t):
                    raise ValueError("invalid event clock")
                prev=t[-1]
                g=build_candidate(case,p)
                enacted=(p.candidate_id,p.edit_kind,list(p.path),list(p.parameter),digest(g.to_aag().encode()),asdict(g.metrics()))
                if enacted!=tuple(e[k] for k in ("candidate","kind","path","parameter","fingerprint","metrics")):
                    raise ValueError("candidate receipt mismatch")
                proven=verify_aig_exact(seed,g).status
                if (e["exact"] not in (None,proven) or (e["smt"] is not None and type(e["smt"]) is not bool)
                    or (e["smt"] is not None and (e["exact"]!="EQUIVALENT" or e["smt"] is not True))):
                    raise ValueError("proof receipt mismatch")
                label=disposition(base,e["metrics"],e["exact"],e["smt"],t[-1],t[-1]-t[0])
                if e["status"]!=label:
                    raise ValueError("disposition mismatch")
            if run["elapsed_ns"]<prev or (len(events)<len(planned) and run["elapsed_ns"]<BUDGET):
                raise ValueError("incomplete run")
            new_runs.append({**run,**summarize(base["and_count"],events)})
        result.append({**expected,"runs":new_runs})
    return {**fixed,"cases":result}


def main():
    import argparse
    import platform
    import subprocess
    p=argparse.ArgumentParser()
    p.add_argument("output",type=Path)
    args=p.parse_args()
    protocol=ROOT/"docs/ANYTIME_PROTOCOL.md"
    if digest(protocol.read_bytes())!="a9f63ccdcd9c96c245d6982e68851ded729a2b3463d98e01841acb963b4cf91f":
        raise SystemExit("protocol checksum mismatch")
    tool=ROOT/"dev/tools/z3_lib_cli.py"
    version=subprocess.check_output([str(tool),"-version"],text=True,timeout=5).strip()
    payload=dict(schema=SCHEMA,model_calls=0,holdout=False,budget_ns=BUDGET,attempt_ns=ATTEMPT,limit=LIMIT,repeats=REPEATS,
        python=platform.python_version(),z3=version,pins={f:digest((ROOT/f).read_bytes()) for f in
        ("docs/ANYTIME_PROTOCOL.md","dev/mdai/anytime.py","dev/mdai/candidates.py","dev/mdai/aig.py",
         "dev/mdai/logic.py","dev/mdai/w3verify.py","dev/tools/z3_lib_cli.py")},cases=[])
    for idx,case in enumerate(fixtures()):
        seed=seed_graph(case)
        runs=[]
        for repeat in range(REPEATS):
            for arm in (ARMS if (idx+repeat)%2==0 else ARMS[::-1]):
                runs.append(run_arm(case,seed,arm,repeat))
        payload["cases"].append(dict(id=case["task_id"],family=case["family"],initial=asdict(seed.metrics()),
                                     case_sha256=digest(canonical(case)),runs=runs))
    result=replay(payload)
    with args.output.open("xb") as f:
        f.write(canonical(payload)+b"\n")
    from collections import Counter
    for arm in ARMS:
        runs=[r for c in result["cases"] for r in c["runs"] if r["arm"]==arm]
        print(arm, len(runs),sum(r["target_ns"] is not None for r in runs),Counter(e["status"] for r in runs for e in r["events"]))


if __name__ == "__main__":
    main()
