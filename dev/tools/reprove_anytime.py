"""Recheck completed proof claims, never infer wall timings retroactively."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mdai.anytime import fixtures, replay, digest, canonical, ROOT
from mdai.aig import AIG
from mdai.candidates import seed_graph, candidate_menu, build_candidate
from mdai.w3verify import verify_z3

def main(src,dst):
    raw=src.read_bytes()
    p=json.loads(raw)
    projected=replay(p)
    cache=set(); fingerprints=set(); n=0
    for fixture,c in zip(fixtures(),projected["cases"]):
        seed=AIG.from_aag(seed_graph(fixture).to_aag())
        menu={a.candidate_id:a for a in candidate_menu(fixture)}
        for r in c["runs"]:
            for e in r["events"]:
                if e["smt"] is not True: continue
                fp=e["fingerprint"]; key=(c["id"],fp)
                if key not in cache:
                    text=build_candidate(fixture,menu[e["candidate"]]).to_aag()
                    if digest(text.encode())!=fp or verify_z3(seed,AIG.from_aag(text),2) is not True:
                        raise ValueError("redecoded proof failed")
                    cache.add(key)
                n+=1; fingerprints.add(fp)
    if not n:
        raise ValueError("no proven claims to recheck")
    receipt=dict(schema="mdai-anytime-reproof-v1",status="PASS",trace_sha256=digest(raw),
                 reproved=n,unique_reproved=len(fingerprints),case_graph_pairs=len(cache),
                 time_verified_by_reproof=False)
    with dst.open("xb") as f: f.write(canonical(receipt)+b"\n")
    print(receipt)
if __name__=="__main__":
    main(*(Path(x) for x in sys.argv[1:]))
