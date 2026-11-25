"""Write-once development-only exact synthesis measurements after cohort freeze."""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

from .abc_runner import MILESTONE_RECIPES, decode_binary, encode_binary, run_abc
from .candidates import seed_graph
from .w3verify import verify_z3
from .w4expand import ROOT, REFERENCE_ABC, bounded, canon, pin, put, sha

FREEZE = ROOT / "evidence" / "milestone_preregister"
DEST = ROOT / "evidence" / "milestone_synthesis"
ABC_SHA = "c078b63884b8b615e56a9defe506478df10be8e02b1abd870d7d10678ce83753"
PRIOR = "rewrite_refactor"
MENU = tuple(MILESTONE_RECIPES)
NEW = "lut_mfs"
OLD = tuple(a for a in MENU if a != NEW)
REPEATS = (0, 1)
SLOT = 20.0
TIES = (PRIOR, *(a for a in MENU if a != PRIOR))


def accept_prereg(spec, cohort_sha, manifest_sha):
    if (spec["version"] != "milestone-lut-mfs-v1"
            or spec["scope"] != "synthetic A/B development only"
            or spec["cohort_sha256"] != cohort_sha or spec["source_manifest_sha256"] != manifest_sha
            or spec["arms"] != {a:list(MILESTONE_RECIPES[a]) for a in MENU}
            or spec["historical_fixed_reference"] != PRIOR
            or spec["train_selection"]["tie_break"] != list(TIES)
            or spec["train_selection"]["select_best_fixed_separately_for_old_four_and_expanded_five"] is not True
            or spec["stop_rule"]["meaningful_incremental_headroom_requires_all"] != {
                "minimum_expanded_minus_old_train_selected_fixed_eval_and_nodes":0,
                "minimum_independent_eval_groups_improved_by_oracle":2,
                "minimum_total_eval_oracle_increment_and_nodes":2,
            }
            or spec["attempts"] != {
                "external_equivalence_proof_required":True,"max_verification_s":5,
                "per_action_total_s":SLOT,"planned_total":240,"repeats":list(REPEATS),
                "retry":False,"same_seed_all_arms":True,"source_depth_cap":True,
            }):
        raise ValueError("runner and preregistered experiment differ")


def train_selection(groups, menu=MENU):
    if not groups or any(g["partition"] != "train" for g in groups):
        raise ValueError("fixed-policy selection must receive only train")
    totals = {a: sum(g["actions"][a]["strict_gain"] for g in groups) for a in menu}
    return max((a for a in TIES if a in totals),key=totals.__getitem__), totals


def read_inputs(cohort_sha, prereg_sha):
    cohort_bytes = bounded(FREEZE / "cohort.json")
    prereg_bytes = bounded(FREEZE / "preregistered.json")
    if sha(cohort_bytes) != cohort_sha or sha(prereg_bytes) != prereg_sha:
        raise ValueError("frozen digest mismatch")
    cohort = json.loads(cohort_bytes)
    prereg = json.loads(prereg_bytes)
    manifest = bounded(FREEZE / "manifest.jsonl")
    if cohort["outcome_blind"] is not True or sha(manifest) != cohort["source_manifest_sha256"]:
        raise ValueError("manifest digest or freeze contract mismatch")
    accept_prereg(prereg, cohort_sha, sha(manifest))
    rows = {}
    for line in manifest.splitlines():
        row = json.loads(line)
        tid = row["task_id"]
        if tid in rows or row["split"] != "development" or row["family"] not in ("A", "B"):
            raise ValueError("unexpected manifest row")
        rows[tid] = row
    seen_groups, seen_roots, seen_tasks = set(), set(), set()
    selected = []
    for group in cohort["groups"]:
        row = rows[group["task_id"]]
        part = group["partition"]
        if (part not in {"train", "dev"} or group["group_id"] != row["canonical_id"]
                or group["root_id"] != row["root_id"] or group["family"] != row["family"]
                or row["task_id"] in seen_tasks or row["root_id"] in seen_roots
                or row["canonical_id"] in seen_groups):
            raise ValueError("invalid frozen group")
        seen_tasks.add(row["task_id"])
        seen_roots.add(row["root_id"])
        seen_groups.add(row["canonical_id"])
        selected.append((group, row))
    if (list(cohort["task_ids"]) != [r["task_id"] for _, r in selected]
            or seen_tasks != set(rows) or len(rows) * len(MENU) * len(REPEATS) != 240
            or {(g["partition"], r["family"]) for g, r in selected} != {
                 ("train","A"), ("train","B"), ("dev","A"), ("dev","B")}):
        raise ValueError("freeze membership is not an exact train/eval A/B join")
    return selected, sha(manifest)


def compare(groups):
    if not groups:
        raise ValueError("expected verified groups")
    gains = lambda g, a: g["actions"][a]["strict_gain"]
    train = [g for g in groups if g["partition"] == "train"]
    evaluation = [g for g in groups if g["partition"] == "eval"]
    if not train or not evaluation:
        raise ValueError("empty train or evaluation")
    selected, totals = train_selection(train)
    old_fixed, _ = train_selection(train, OLD)
    def aggregate(rows):
        def best(g, menu):
            return max(gains(g,a) for a in menu)
        return {
            "groups": len(rows),
            "prior_fixed_gain": sum(gains(g,PRIOR) for g in rows),
            "old_train_selected_gain": sum(gains(g,old_fixed) for g in rows),
            "train_selected_gain": sum(gains(g,selected) for g in rows),
            "oracle_gain": sum(best(g,MENU) for g in rows),
            "prior_menu_oracle_gain": sum(best(g,OLD) for g in rows),
            "groups_oracle_beats_train_selected": sum(best(g,MENU) > gains(g,selected) for g in rows),
            "groups_new_oracle_beats_old": sum(best(g,MENU) > best(g,OLD) for g in rows),
        }
    return (selected, old_fixed, totals, aggregate(evaluation),
            {f:aggregate([g for g in evaluation if g["family"] == f]) for f in ("A","B")})


def positive_predefined_contrast(evaluation):
    return bool(evaluation["groups_new_oracle_beats_old"] >= 2
                and evaluation["oracle_gain"] - evaluation["prior_menu_oracle_gain"] >= 2
                and evaluation["train_selected_gain"] >= evaluation["old_train_selected_gain"])


def run(abc, cohort_sha, prereg_sha):
    rows, manifest_sha = read_inputs(cohort_sha, prereg_sha)
    if not abc.is_absolute() or pin(abc) != ABC_SHA:
        raise ValueError("ABC executable pin mismatch")
    version = subprocess.run([str(abc),"-s","-c","version"], text=True, capture_output=True, timeout=5, check=True)
    if REFERENCE_ABC["expected_version_line"] not in version.stdout.splitlines():
        raise ValueError("ABC version drift")
    z3 = ROOT/"tools"/"z3_lib_cli.py"
    z3v = subprocess.check_output([str(z3),"-version"],text=True,timeout=5).strip()
    if z3v != "Z3 4.13.3.0":
        raise ValueError("Z3 version drift")
    DEST.mkdir(exist_ok=False)
    receipts = DEST/"receipts"
    receipts.mkdir()
    put(DEST/"execution_pins.json",{
        "cohort_sha256":cohort_sha,"preregistered_sha256":prereg_sha,"manifest_sha256":manifest_sha,
        "abc_sha256":ABC_SHA,"abc_version":version.stdout.strip(),"z3_version":z3v,"z3_bridge_sha256":pin(z3),
        "runner_sha256":pin(Path(__file__)),"abc_adapter_sha256":pin(ROOT/"mdai"/"abc_runner.py"),
        "verifier_sha256":pin(ROOT/"mdai"/"w3verify.py"),
        "menu":{a:list(MILESTONE_RECIPES[a]) for a in MENU},"repeats":list(REPEATS),
        "slot_s":SLOT,"verification_cap_s":5,"prior_fixed_action":PRIOR,"train_tie_order":list(TIES),
    })
    groups = []
    statuses = Counter()
    for i,(g,row) in enumerate(sorted(rows,key=lambda x:x[0]["partition"] != "train")):
        if g["partition"] == "dev" and not (DEST/"train_selection.json").exists():
            train_complete = all(a["status"] == "VERIFIED" for prior in groups for a in prior["actions"].values())
            fixed, totals = train_selection(groups) if train_complete else (None, None)
            old_fixed, _ = train_selection(groups, OLD) if train_complete else (None, None)
            put(DEST/"train_selection.json",{"fixed_action":fixed,"old_fixed_action":old_fixed,"group_action_totals":totals,
                                            "paired_trial_action_totals":{a:v*len(REPEATS) for a,v in totals.items()} if totals else None,
                                            "selected_before_evaluation":True,
                                            "groups":len(groups),"complete":train_complete})
            if not train_complete:
                break
        base = seed_graph(row)
        baseline = vars(base.metrics())
        dest = receipts/f"group-{i:03d}"
        dest.mkdir()
        (dest/"seed.aag").write_text(base.to_aag(symbols=True),encoding="ascii")
        expected_input = encode_binary(base)
        actions = {}
        for a in MENU:
            trials = []
            for repeat in REPEATS:
                chunk = dest/f"repeat-{repeat}-{a}"
                result = run_abc(base, abc_path=abc, recipe=a, timeout_s=SLOT, verify=verify_z3,
                                 evidence_dir=chunk, milestone_menu=True)
                statuses[result.status] += 1
                proof = {"status":result.status,"receipt_path":str((chunk/"receipt.json").relative_to(DEST)),
                         "receipt_sha256":pin(chunk/"receipt.json")}
                if result.status == "VERIFIED" and result.equivalent is True:
                    output_bytes = bounded(chunk/"output.aig")
                    in_data = bounded(chunk/"input.aig")
                    if in_data != expected_input or decode_binary(output_bytes,require_labels=True).metrics() != result.output_metrics:
                        raise ValueError("ABC raw evidence mismatch")
                    proof.update(input_sha256=sha(in_data),output_sha256=sha(output_bytes),
                                 output_metrics=vars(result.output_metrics))
                trials.append(proof)
            verified = all(t["status"] == "VERIFIED" and "output_metrics" in t for t in trials)
            stable = verified and trials[0]["output_metrics"] == trials[1]["output_metrics"]
            depth_ok = stable and trials[0]["output_metrics"]["depth"] <= baseline["depth"]
            reduction = baseline["and_count"] - trials[0]["output_metrics"]["and_count"] if stable else None
            actions[a] = {"status":"VERIFIED" if stable else "BLOCKED",
                          "strict_gain":max(0,reduction) if depth_ok else 0 if stable else None,
                          "and_reduction":reduction,"depth_ok":depth_ok,"repeat_stable":stable,
                          "output_repeat_identical":verified and trials[0]["output_sha256"] == trials[1]["output_sha256"],
                          "trials":trials}
        groups.append({"group_id":g["group_id"],"family":row["family"],
                       "partition":"eval" if g["partition"] == "dev" else "train",
                       "source":baseline,"actions":actions,
                       "seed_sha256":pin(dest/"seed.aag"),"seed_path":str((dest/"seed.aag").relative_to(DEST))})
    put(DEST/"group_metrics.json",{"groups":groups})
    summary = {"state":"BLOCKED","prior_fixed_action":PRIOR,"menu":list(MENU),
               "counts":{"groups":len(groups),"trials":sum(statuses.values()),"statuses":dict(statuses)},
               "cohort_sha256":cohort_sha,"preregistered_sha256":prereg_sha,"manifest_sha256":manifest_sha,
               "execution_pins_sha256":pin(DEST/"execution_pins.json"),
               "train_selection_sha256":pin(DEST/"train_selection.json"),
               "group_metrics_sha256":pin(DEST/"group_metrics.json"),
               "runtime_claim":False,"ranker_fit":False,"model_calls":0,"holdout_accessed":False}
    if len(groups) == len(rows) and all(a["status"] == "VERIFIED" for g in groups for a in g["actions"].values()):
        fixed, old_fixed, train, evaluation, by_family = compare(groups)
        locked = json.loads(bounded(DEST/"train_selection.json"))
        if (fixed, old_fixed) != (locked["fixed_action"], locked["old_fixed_action"]):
            raise ValueError("locked train selection differs from replay")
        summary.update(state="VERIFIED_DEVELOPMENT_ONLY",train_selected_action=fixed,
                       old_train_selected_action=old_fixed,train_action_totals=train,
                       eval_totals=evaluation,eval_by_family=by_family,
                       stop_decision=("SIGNAL_FOR_SEPARATE_CONFIRMATION" if positive_predefined_contrast(evaluation)
                                      else "STOP_MENU_EXPANSION_NO_PREDEFINED_SIGNAL"))
    put(DEST/"summary.json",summary)
    print(canon(summary))
    return summary["state"] == "VERIFIED_DEVELOPMENT_ONLY"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort-sha256",required=True)
    ap.add_argument("--prereg-sha256",required=True)
    ap.add_argument("--abc",type=Path)
    ap.add_argument("--check-only",action="store_true")
    args = ap.parse_args()
    hashes = args.cohort_sha256,args.prereg_sha256
    if args.check_only:
        rows,digest = read_inputs(*hashes)
        print(canon({"groups":len(rows),"manifest_sha256":digest,"executed":False}))
    elif args.abc:
        raise SystemExit(0 if run(args.abc,*hashes) else 1)
    else:
        ap.error("requires --abc")


if __name__ == "__main__":
    main()
