"""SMT cross-check of two already-parsed development AIGs. No arbitrary code."""
from pathlib import Path
import subprocess
from time import monotonic
from .aig import verify_aig_exact

def _encode(g, prefix):
    aag = g.to_aag(symbols=False).splitlines()
    _,i,l,o,a=map(int,aag[0].split()[1:])
    if l or i > 16 or a > 4096 or o < 1:
        raise ValueError("outside the bounded development profile")
    def lit(x):
        if x < 2: return f"#b{x}"
        base = f"v{x//2}" if x//2 <= i else f"{prefix}{x//2}"
        return f"(bvnot {base})" if x%2 else base
    outputs=[lit(int(v)) for v in aag[1+i:1+i+o]]
    defs=[]
    for line in aag[1+i+o:1+i+o+a]:
        out,x,y=map(int,line.split())
        defs.append(f"(define-fun {prefix}{out//2} () (_ BitVec 1) (bvand {lit(x)} {lit(y)}))")
    return defs,outputs

def verify_z3(a,b,remaining_s):
    if remaining_s<=0: return None
    deadline=monotonic()+min(5,remaining_s)
    n=len(a.input_literals)
    if a.input_names != b.input_names or n!=len(b.input_literals) or len(a.outputs)!=len(b.outputs): return False
    al,ao=_encode(a,"g")
    bl,bo=_encode(b,"h")
    decl=[f"(declare-const v{i} (_ BitVec 1))" for i in range(1,n+1)]
    disj=[f"(distinct {x} {y})" for x,y in zip(ao,bo)]
    condition=disj[0] if len(disj)==1 else f"(or {' '.join(disj)})"
    script="\n".join(decl+al+bl+[f"(assert {condition})","(check-sat)"])+"\n"
    path=Path(__file__).resolve().parents[1]/"tools"/"z3_lib_cli.py"
    try:
        run=subprocess.run([str(path),"-in","-smt2"],input=script,text=True,
              capture_output=True,timeout=max(0.001,deadline-monotonic()))
    except (OSError,subprocess.TimeoutExpired):
        return None
    if run.returncode or run.stderr.strip() or monotonic()>=deadline: return None
    result=run.stdout.strip()
    if result not in ("sat","unsat"): return None
    independent=verify_aig_exact(a,b)
    if monotonic()>=deadline: return None
    if result=="unsat" and independent.status=="EQUIVALENT": return True
    if result=="sat" and independent.status=="DIFFERENT":
        vals=independent.counterexample
        if vals is not None and a.evaluate(vals)!=b.evaluate(vals): return False
    raise ValueError("SMT and independent ordered-port verifier disagree")
