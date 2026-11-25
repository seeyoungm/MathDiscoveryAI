"""Retrospective structural diagnostic of frozen A/B development receipts, no ABC run."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from .abc_runner import decode_binary
from .w4expand import ROOT, bounded, pin, put

HISTORICAL = ROOT / "evidence" / "nextstage_synthesis"
CURRENT = ROOT / "evidence" / "milestone_synthesis"
BASELINE = "rewrite_refactor"


def structure_signature(aig):
    """Numbering and AND child-order independent; NOT a Boolean canonical form."""
    lines = aig.to_aag(symbols=False).splitlines()
    _, _, n, _, o, _ = lines[0].split()
    n,o=int(n),int(o)
    signatures = {0:b"const-zero"}
    for i in range(n):
        signatures[i+1] = b"input-"+i.to_bytes(4,"big")
    def literal(s):
        lit=int(s)
        return bytes([lit & 1])+signatures[lit//2]
    for text in lines[1+n+o:]:
        lhs, x, y = text.split()
        left,right = sorted((literal(x),literal(y)))
        signatures[int(lhs)//2] = sha256(b"AND-"+left+b":"+right).digest()
    return sha256(b"".join(sha256(literal(s)).digest() for s in lines[1+n:1+n+o])).hexdigest()


def digest_json(path, digest):
    if pin(path) != digest:
        raise ValueError(f"source digest mismatch: {path.name}")
    return json.loads(bounded(path, 10_000_000))


def diagnose_run(root:Path, alternate:str):
    summary = json.loads(bounded(root/"summary.json"))
    if summary["state"] != "VERIFIED_DEVELOPMENT_ONLY":
        raise ValueError("not a complete result")
    metrics = digest_json(root/"group_metrics.json",summary["group_metrics_sha256"])
    pins = digest_json(root/"execution_pins.json",summary["execution_pins_sha256"])
    totals=Counter()
    bucket_counts={}
    signatures=[]
    expected_commands={}
    for g in metrics["groups"]:
        counts = Counter()
        acts=g["actions"]
        ref = acts[BASELINE]
        alt = acts[alternate]
        if ref["status"] != "VERIFIED" or alt["status"] != "VERIFIED":
            raise ValueError("pair must be verified")
        gain_change=alt["strict_gain"] - ref["strict_gain"]
        counts["groups"]+=1
        counts["strict_gain_above_reference_groups"]+=gain_change>0
        counts["strict_gain_equal_reference_groups"]+=gain_change==0
        counts["strict_gain_below_reference_groups"]+=gain_change<0
        counts["depth_cap_rejected_groups"]+=not alt["depth_ok"]
        old_menu = [a for a in acts if a!=alternate]
        old_gain = max(acts[a]["strict_gain"] for a in old_menu)
        counts["unique_menu_best_groups"]+=alt["strict_gain"]>old_gain
        counts["same_menu_best_groups"]+=alt["strict_gain"]==old_gain
        counts["neither_reference_nor_alternative_improves_groups"]+=(ref["strict_gain"]==alt["strict_gain"]==0)
        counts["positive_alternative_gain_groups"]+=alt["strict_gain"]>0
        counts["positive_reference_gain_groups"]+=ref["strict_gain"]>0
        counts["same_gate_count_depth_groups"]+=ref["trials"][0]["output_metrics"]==alt["trials"][0]["output_metrics"]
        seqs={}
        for action in (BASELINE,alternate):
            seqs[action]=[]
            for t in acts[action]["trials"]:
                path=root/t["receipt_path"]
                receipt=digest_json(path,t["receipt_sha256"])
                expected="; ".join(("read_aiger input.aig",*pins["menu"][action],"write_aiger -s output.aig"))
                if (receipt["literal_commands"] != expected or receipt["recipe"] != action
                        or receipt["status"] != "VERIFIED" or not receipt["equivalent"]):
                    raise ValueError("unverified or unexpected command")
                output=path.with_name("output.aig")
                if pin(output)!=t["output_sha256"]:
                    raise ValueError("unexpected final AIG digest")
                data=bounded(output)
                aig=decode_binary(data,require_labels=True)
                if vars(aig.metrics())!=receipt["output_metrics"]:
                    raise ValueError("raw output metric mismatch")
                seqs[action].append((structure_signature(aig), t["output_sha256"]))
                expected_commands[action]=expected
            counts[f"{action}_within_pair_same_structure_groups"]+=seqs[action][0][0]==seqs[action][1][0]
            counts[f"{action}_within_pair_raw_bytes_same_groups"]+=seqs[action][0][1]==seqs[action][1][1]
        same=sum(x[0]==y[0] for x,y in zip(seqs[BASELINE],seqs[alternate]))
        counts["both_repeats_same_between_actions_groups"]+=same==2
        counts["either_repeat_differs_between_actions_groups"]+=same<2
        counts["either_repeat_differs_but_same_metrics_groups"]+=same<2 and bool(counts["same_gate_count_depth_groups"])
        counts["different_structure_and_alt_behind_groups"]+=same<2 and gain_change<0
        signatures.append({"group_id":g["group_id"],"partition":g["partition"],"family":g["family"],
                           "alternative_minus_reference_strict_gain":gain_change,"paired_structure_matches":same})
        totals.update(counts)
        bucket_counts.setdefault(f'{g["partition"]}:{g["family"]}',Counter()).update(counts)
    return {"alternate":alternate,"reference":BASELINE,"literal_commands":expected_commands,
            "summary_sha256":pin(root/"summary.json"),"group_metrics_sha256":summary["group_metrics_sha256"],
            "totals":dict(totals),"by_partition_family":{k:dict(v) for k,v in bucket_counts.items()},
            "groups":signatures}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--historical-only",action="store_true")
    args=ap.parse_args()
    result={"state":"READ_ONLY_AB_DEVELOPMENT_DIAGNOSTIC",
            "interpretation_limit":"Final circuit shape and costs are observed; intermediate choices, global optimum, performance, and internal causes are not established.",
            "historical":diagnose_run(HISTORICAL,"resub_balance")}
    if not args.historical_only:
        result["current"]=diagnose_run(CURRENT,"lut_mfs")
    path=ROOT/"evidence"/("milestone_historical_diagnostic.json" if args.historical_only else "milestone_synthesis_diagnostic.json")
    put(path,result)
    print(json.dumps({k:v["totals"] for k,v in result.items() if isinstance(v,dict)},sort_keys=True))


if __name__=="__main__":
    main()
