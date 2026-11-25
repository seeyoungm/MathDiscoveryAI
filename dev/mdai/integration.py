"""Development A/B smoke only; no discovery or loading of evaluation artifacts."""
import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from itertools import product
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic

from .abc_runner import RECIPES, run_abc
from .aig import AIG, compile_truth_table, compile_words, verify_aig_exact
from .devgen import generate_development, partition_groups
from .logic import _compile, _smt_program, evaluate, parse_expression, verify

def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"))

def dump(dest, name, obj):
    (dest/name).write_text(json.dumps(obj, sort_keys=True, indent=2)+"\n")

def prove_word_to_aig(expr, inputs, g, z3):
    p = _compile(expr, inputs)
    names = {k:f"v{i}" for i,k in enumerate(inputs)}
    script = [f"(declare-const {names[k]} (_ BitVec {w}))" for k,w in inputs.items()]
    n = 1
    for name,w in inputs.items():
        for bit in range(w):
            script.append(f"(define-fun g{n} () (_ BitVec 1) ((_ extract {bit} {bit}) {names[name]}))")
            n += 1
    def lit(x):
        if x < 2: return f"#b{x}"
        return f"(bvnot g{x//2})" if x%2 else f"g{x//2}"
    # Serialization prunes unreachable nodes and compacts variable IDs; both
    # definitions and outputs must therefore use that serialized numbering.
    aag=g.to_aag(symbols=False).splitlines()
    output_start=1+len(g.input_literals)
    gate_start=output_start+len(g.outputs)
    for row in aag[gate_start:]:
        lhs,a,b = map(int,row.split())
        script.append(f"(define-fun g{lhs//2} () (_ BitVec 1) (bvand {lit(a)} {lit(b)}))")
    script += _smt_program(p,names,"w")
    bits = [lit(int(row)) for row in aag[output_start:gate_start]]
    value = bits[0]
    for x in bits[1:]: value = f"(concat {x} {value})"
    script += [f"(assert (distinct w{len(p)-1} {value}))", "(check-sat)"]
    r = subprocess.run([z3,"-in","-smt2"],input="\n".join(script),text=True,capture_output=True,timeout=5)
    assert r.returncode == 0 and r.stdout.strip() == "unsat", (r.returncode,r.stdout,r.stderr)

def exact_cb(a,b,remaining):
    if remaining <= 0: return None
    verdict = verify_aig_exact(a,b).status
    return {"EQUIVALENT":True,"DIFFERENT":False}.get(verdict)

def compile_record(rec,z3):
    names=rec["input_order"]
    if rec["family"]=="A":
        bits=[(rec["truth_table"]>>i)&1 for i in range(16)]
        g=compile_truth_table(bits,names)
        assert [g.evaluate(row)[0] for row in product((0,1),repeat=4)]==bits
        # An explicit sum-of-products expression derived from the original rows is
        # independent of the AIG truth-table compiler's Shannon construction.
        leaves=[{"op":"input","name":n,"width":1} for n in names]
        def fold(op, nodes):
            first,*rest=nodes
            for child in rest:
                first={"op":op,"width":1,"args":[first,child]}
            return first
        terms=[fold("and",[x if b else {"op":"not","width":1,"args":[x]}
                          for x,b in zip(leaves,row)])
               for row,result in zip(product((0,1),repeat=4),bits) if result]
        expr=fold("or",terms) if terms else {"op":"const","width":1,"value":0}
        prove_word_to_aig(expr,{n:1 for n in names},g,z3)
    else:
        ins={k:rec["inputs"][k] for k in names}
        ast=parse_expression(canonical(rec["ast"]),ins)
        g=compile_words(ins,[ast])
        for strategy in (lambda w:0,lambda w:1,lambda w:(1<<w)-1,lambda w:1<<(w-1)):
            bound={k:strategy(w) for k,w in ins.items()}
            data=tuple((bound[k]>>j)&1 for k,w in ins.items() for j in range(w))
            assert sum(b<<j for j,b in enumerate(g.evaluate(data)))==evaluate(ast,bound,ins)
        prove_word_to_aig(ast,ins,g,z3)
    return g

def main():
    cli=argparse.ArgumentParser()
    cli.add_argument("--abc")
    opts=cli.parse_args()
    root=Path(__file__).resolve().parents[1]
    z3=str(root/"tools/z3_lib_cli.py")
    dest=root/"evidence"/"week2_smoke"
    dest.mkdir(exist_ok=False)
    rows=generate_development()
    payload="".join(canonical(x)+"\n" for x in rows).encode()
    assert payload=="".join(canonical(x)+"\n" for x in generate_development()).encode()
    (dest/"dataset_manifest.jsonl").write_bytes(payload)
    groups_by_split=partition_groups(rows)
    groups=[{"split":split,"members":[member["task_id"] for member in component]} for split,components in groups_by_split.items() for component in components]
    assert len(groups)==64
    dump(dest,"split_manifest.json",{"status":"dev_smoke_only","groups":groups,"training_release":False,
      "excluded":["A_final","B_final","B_16_32","C","D","E"],"untested":["A_5_6_exact","B_cross_signature","holdout_equivalence"]})
    ins={"x":8,"y":8}
    x={"op":"input","name":"x","width":8}; y={"op":"input","name":"y","width":8}
    e1={"op":"xor","width":8,"args":[x,y]}; e2={"op":"xor","width":8,"args":[y,x]}
    verdicts=[verify(e1,right,ins,max_exhaustive_bits=0,z3_path=z3) for right in (e2,x,{"op":"invalid","width":8})]
    assert [v.status for v in verdicts]==["EQUIVALENT","COUNTEREXAMPLE","INVALID"]
    witness=verdicts[1].counterexample
    assert evaluate(e1,witness,ins)!=evaluate(x,witness,ins)
    selected=[rows[0],rows[16]]
    selected += [next(r for r in rows if r["family"]=="B" and r["outputs"][0]["width"]==w) for w in (4,8)]
    verified_graphs={r["task_id"]:compile_record(r,z3) for r in rows}
    trials=[]
    for i,r in enumerate(selected):
        original=verified_graphs[r["task_id"]]
        aag=original.to_aag(symbols=False)
        (dest/f"seed_{i}.aag").write_text(aag)
        for rep in range(3):
            t=monotonic()
            baseline=AIG.from_aag(aag)
            assert verify_aig_exact(original,baseline).status=="EQUIVALENT"
            assert baseline.metrics()==original.metrics()
            trial={"task":r["task_id"],"repeat":rep,"seed_metrics":asdict(baseline.metrics()),"abc":[]}
            if opts.abc:
                for recipe in RECIPES:
                    remaining=60-(monotonic()-t)
                    if remaining<=0: break
                    a=run_abc(baseline,abc_path=opts.abc,recipe=recipe,timeout_s=min(5,remaining),verify=exact_cb,evidence_dir=dest/f"abc_{i}_{rep}_{recipe}")
                    trial["abc"].append({"recipe":recipe,"status":a.status,"detail":a.detail,"eligible":a.eligible,"elapsed_s":a.elapsed_s,
                        "metrics":asdict(a.output_metrics) if a.output_metrics else None})
            trial["wall_s"]=monotonic()-t
            assert trial["wall_s"]<=60
            trials.append(trial)
    def command(argv):
        return subprocess.run(argv,capture_output=True,text=True,check=True,timeout=5).stdout.strip()
    versions={"python":sys.version.split()[0],"python_path":sys.executable,
      "z3":command([z3,"-version"]),"z3_path":z3,"z3_mode":"official installed C library via local bridge"}
    if opts.abc: versions.update(abc=command([opts.abc,"-s","-c","version"]),abc_path=opts.abc)
    dump(dest,"environment.json",versions)
    states=Counter(q["status"] for row in trials for q in row["abc"])
    receipt={"counts":dict(Counter(r["family"] for r in rows)),"groups":len(groups),"dataset_sha256":sha256(payload).hexdigest(),
      "source_aig_z3_verified":len(verified_graphs),"abc_dev_fixtures":len(selected),"repeats_total":len(trials),"ABC":dict(states),"ABC_recipes":RECIPES,
      "Z3":list(map(asdict,verdicts)),"trials":trials,"heldout_access":False,"model_calls":0,"performance_claim":False}
    dump(dest,"integration_receipt.json",receipt)
    print(canonical({k:v for k,v in receipt.items() if k!="trials"}))
    if opts.abc and states!={"VERIFIED":len(selected)*3*len(RECIPES)}:
        raise SystemExit("ABC non-verified results; do not pass integration gate")
if __name__=="__main__": main()
