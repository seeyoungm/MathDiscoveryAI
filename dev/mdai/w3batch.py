"""Preregistered development-only controls and separately labelled model replay."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from .abc_runner import RECIPES
from .candidates import candidate_menu,build_candidate,seed_graph
from .devgen import generate_development,partition_groups
from .selector import check_adequacy
from .w3runner import Proposal,run_match
from .w3verify import verify_z3

def canon(x):
    return json.dumps(x,sort_keys=True,separators=(",",":"))
def pin(path):
    return sha256(path.read_bytes()).hexdigest()
def dump(path,obj):
    with path.open("x") as f:
        f.write(json.dumps(obj,indent=2,sort_keys=True)+"\n")
def strict_json(s):
    def hook(pairs):
        o={}
        for k,v in pairs:
            if k in o: raise ValueError("duplicate key")
            o[k]=v
        return o
    return json.loads(s,object_pairs_hook=hook)
def guarded_pilot(path, chosen):
    keys=("A0","A1","B0","B1")
    response=strict_json((path/"response.txt").read_text())
    if type(response) is not dict or set(response)!=set(keys):
        raise ValueError("invalid response key set")
    schedule=[chosen[0],chosen[1],next(r for r in chosen if r["family"]=="B" and r["outputs"][0]["width"]==4),
              next(r for r in chosen if r["family"]=="B" and r["outputs"][0]["width"]==8)]
    known_a="0132 0213 0231 0312 0321 1023 1032 1203 1230 1302 1320 2013 2031 2103 2130 2301 2310 3012 3021 3102".split()
    expected_a=[tuple(map(int,r)) for r in known_a]
    action_map={}
    dispositions={}
    for key,row in zip(keys,schedule):
        menu=candidate_menu(row)
        picks=response[key]
        bound=20 if key[0]=="A" else 1
        if type(picks) is not list or not 1<=len(picks)<=3 or any(type(x) is not int or not 0<=x<bound for x in picks) or len(set(picks))!=len(picks):
            raise ValueError("invalid ranking")
        if key[0]=="A":
            match=[a.parameter for a in menu]==expected_a and all(a.edit_kind=="shannon_order" for a in menu)
            match = match and row["truth_table"]=={"A0":46177,"A1":39931}[key]
        else:
            match=len(menu)==1 and menu[0].edit_kind=="neutral_double_not"
        if not match:
            dispositions[key]="MENU_DRIFT_REJECTED"
            continue
        approved=[menu[i] for i in picks]
        action_map[row["task_id"]]=approved+[a for a in menu if a not in approved]
        dispositions[key]="REPLAYABLE_WITH_CANONICAL_TAIL"
    return action_map,dispositions
def main():
    arg=argparse.ArgumentParser()
    arg.add_argument("--abc",required=True)
    ns=arg.parse_args()
    root=Path(__file__).resolve().parents[1]
    cfgfile=root/"w3_config.json"
    cfg=json.loads(cfgfile.read_text())
    assert cfg["wall_budget_s"]==60 and cfg["candidate_limit"]==20 and cfg["repeats"]==[0,1,2] and list(RECIPES)==cfg["abc_portfolio"]
    base=root/"evidence"/"week3"
    pilot=base/"model_pilot"
    out=base/"matched_dev_controls"
    out.mkdir(exist_ok=False)
    rows=generate_development()
    assert sha256("".join(canon(x)+"\n" for x in rows).encode()).hexdigest()==cfg["source_manifest_sha256"]
    chosen=[r for r in rows if r["family"]=="A"][:10]
    for width in (4,8):
        chosen += [r for r in rows if r["family"]=="B" and r["outputs"][0]["width"]==width][:5]
    assert len(chosen)==20 and len(partition_groups(chosen)["development"])==20
    ranks,dispositions=guarded_pilot(pilot,chosen)
    rid={"source":"actual native model call, parent recorded text; replay only","model":"native model configuration not exposed",
         "request_sha256":pin(pilot/"request.txt"),"response_sha256":pin(pilot/"response.txt")}
    timer=(int((pilot/"parent_capture_after_response.epoch").read_text())-
           int((pilot/"parent_capture_before_dispatch.epoch").read_text()))
    dump(pilot/"receipt.json",{"identity":rid,"case_dispositions":dispositions,
         "parent_capture_window_coarse_s":timer,"per_task_live_latency_known":False,"metered_model_cost_known":False,
         "ranked_scope":"top up to 3, followed by deterministic canonical menu tail; not a live comparison"})
    dump(out/"cohort.json",{"source_hash":cfg["source_manifest_sha256"],"config_hash":pin(cfgfile),
        "task_ids":[r["task_id"] for r in chosen],"family_counts":dict(Counter(r["family"] for r in chosen)),
        "menus":{r["task_id"]:[a.candidate_id for a in candidate_menu(r)] for r in chosen}})
    def one(pair):
        idx,r=pair
        case=out/f"case-{idx:02}"
        case.mkdir()
        task=r["task_id"]
        seedfile=case/"seed.aag"
        seedfile.write_text(seed_graph(r).to_aag(symbols=True))
        seed_hash=pin(seedfile)
        def proposals(acts, rank=False):
            return [Proposal(a.candidate_id, lambda remain,a=a:build_candidate(r,a),i+1 if rank else None)
                    for i,a in enumerate(acts)]
        def run(name,rep,run_path):
            t=monotonic()
            from .aig import AIG
            source=AIG.from_aag(seedfile.read_text())
            default={"task_id":task,"repeat":rep,"seed":1000+rep,"verify":verify_z3,
              "evidence_dir":case/run_path,"started_monotonic":t}
            if name=="abc":
                return run_match(source,arm=name,abc_path=ns.abc,**default)
            if name=="replay":
                return run_match(source,arm=name,proposals=proposals(ranks[task],True),replay_identity=rid,**default)
            return run_match(source,arm=name,proposals=proposals(candidate_menu(r)),**default)
        runs=[]
        replays=[]
        for repeat in cfg["repeats"]:
            for arm in ("baseline","abc"):
                rec=run(arm,repeat,f"{arm}-{repeat}")
                runs.append(rec)
            if task in ranks:
                rec=run("replay",repeat,f"replay-{repeat}")
                again=run("replay",repeat,f"replay-audit-{repeat}")
                replays.append(rec["outcome_sha256"]==again["outcome_sha256"])
                runs.append(rec)
        assert pin(seedfile)==seed_hash
        assert all(r.get("terminal_status")=="PORTFOLIO_EXHAUSTED" for r in runs)
        return r,runs,replays
    with ThreadPoolExecutor(max_workers=3) as pool:
        completed=list(pool.map(one,enumerate(chosen)))
    arm_count=Counter()
    statuses=Counter()
    gains=Counter()
    replay_checks=[]
    label_rows=[]
    for task,runs,replays in completed:
        replay_checks.extend(replays)
        for x in runs:
            arm=x["key"]["arm"]
            arm_count[arm]+=1
            gains[arm]+=x["verified_improvement"]
            for a in x["attempts"]:
                statuses[(arm,a["status"])]+=1
                if arm=="abc":
                    label=a["label"]
                    label_rows.append({"task_id":task["task_id"],"group_id":task["canonical_id"],
                        "family":task["family"],"features":a["pre_action"],"repeat":x["key"]["repeat"],
                        "partition":"development","action":a["action"],"outcome":{
                            "equivalent":label["equivalent"] is True,"depth_ok":label["depth_ok"] is True,
                            "and_reduction":label["and_reduction"]}})
    (out/"abc_action_observations.jsonl").write_text("".join(canon(row)+"\n" for row in label_rows))
    readiness=check_adequacy(label_rows,list(RECIPES))
    dump(out/"week4_readiness.json",{"scope":"fixed ABC action labels only, not pooled task-local candidate IDs",
          "assessment":readiness,"formal_gate3_pass":False,"actual_fit_run":False})
    summary={"gate3":"PARTIAL","matched_live_model_runs":0,"reasons":["native model configuration not verified",
      "no live per-task AI budget metering for full cohort","Week 4 real training not released"],
      "independent_dev_tasks":len(chosen),"control_runs":dict(arm_count),"attempt_statuses":{
        arm:{status:n for (source,status),n in statuses.items() if source==arm} for arm in arm_count},
      "within_arm_strict_improvement_runs":dict(gains),"pilot_dispositions":dispositions,
      "deterministic_replay_verified":sum(replay_checks),"deterministic_replay_expected":len(replay_checks),
      "comparison_scope":"isolated development smoke, fixed conditions; not AI effect or generalization",
      "wall_timing_caveat":"Three-task parallelism; replay excludes real model cost.",
      "selector_data_ready":readiness["ready"],"selector_data_limitations":readiness["reasons"],
      "heldout_executed":False,"model_training":False}
    dump(out/"summary.json",summary)
    print(canon(summary))
    expected={"VERIFIED","DUPLICATE","DEPTH_CAP"}
    if not all(replay_checks) or any(n for (a,s),n in statuses.items() if s not in expected):
        raise SystemExit("unexpected execution status; do not promote")
if __name__=="__main__": main()
