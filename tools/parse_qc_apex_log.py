import argparse
import collections
import pathlib
import re
import sys


SEARCH_RE = re.compile(r"((?:SEARCH|LOCKED)_[A-Z_]+(?: reason=[a-z_]+)?)")
KV_RE = re.compile(r"([A-Za-z_]+)=([^\s|']+)")


def search_lines(text):
    for raw in text.splitlines():
        if "SEARCH_" in raw or "LOCKED_" in raw:
            yield raw.strip()


def tag_for(line):
    match = SEARCH_RE.search(line)
    return match.group(1) if match else ""


def key_values(line):
    return dict(KV_RE.findall(line))


def numeric_counts(line):
    counts = collections.Counter()
    for key, value in key_values(line).items():
        if re.fullmatch(r"-?\d+", value):
            counts[key] = int(value)
    return counts


def blocker_text(label, line, limit=5):
    if not line or line == "none":
        return f"{label}=none"
    counts = numeric_counts(line)
    counts.pop("ok", None)
    if not counts:
        return f"{label}=none"
    return f"{label}=" + ",".join(f"{k}:{v}" for k, v in counts.most_common(limit))


def reason_from_tag(tag):
    marker = " reason="
    if marker not in tag:
        return None
    return tag.split(marker, 1)[1]


def parse_text(text):
    result = {
        "summary": [],
        "locked": [],
        "reason": [],
        "ensemble_reason": [],
        "apex_reason": [],
        "ensemble_apex_reason": [],
        "final": [],
        "eval": [],
        "ensemble_eval": [],
        "robust": [],
        "ensemble_robust": [],
        "apex_ready": [],
        "ensemble_apex_ready": [],
        "near": [],
        "ensemble_near": [],
        "pick": [],
        "ensemble": [],
    }

    for line in search_lines(text):
        tag = tag_for(line)
        if tag == "SEARCH_SUMMARY":
            result["summary"].append(line)
        elif tag.startswith("LOCKED_FINAL"):
            result["locked"].append(line)
        elif tag == "SEARCH_REASON":
            result["reason"].append(line)
        elif tag == "SEARCH_ENSEMBLE_REASON":
            result["ensemble_reason"].append(line)
        elif tag == "SEARCH_APEX_REASON":
            result["apex_reason"].append(line)
        elif tag == "SEARCH_ENSEMBLE_APEX_REASON":
            result["ensemble_apex_reason"].append(line)
        elif tag == "SEARCH_FINAL":
            result["final"].append(line)
        elif tag == "SEARCH_EVAL_PASS":
            result["eval"].append(line)
        elif tag == "SEARCH_ENSEMBLE_EVAL_PASS":
            result["ensemble_eval"].append(line)
        elif tag == "SEARCH_ROBUST_PASS":
            result["robust"].append(line)
        elif tag == "SEARCH_ENSEMBLE_ROBUST_PASS":
            result["ensemble_robust"].append(line)
        elif tag == "SEARCH_APEX_READY":
            result["apex_ready"].append(line)
        elif tag == "SEARCH_ENSEMBLE_APEX_READY":
            result["ensemble_apex_ready"].append(line)
        elif tag.startswith("SEARCH_NEAR_PASS"):
            result["near"].append(line)
        elif tag.startswith("SEARCH_ENSEMBLE_NEAR_PASS"):
            result["ensemble_near"].append(line)
        elif tag == "SEARCH_PICK":
            result["pick"].append(line)
        elif tag == "SEARCH_ENSEMBLE":
            result["ensemble"].append(line)
    return result


def count_near_reasons(lines):
    counts = collections.Counter()
    for line in lines:
        reason = reason_from_tag(tag_for(line))
        if reason:
            counts[reason] += 1
    return counts


def first_or_none(items):
    return items[0] if items else "none"


def compact_metrics(line):
    if not line or line == "none":
        return "none"
    kv = key_values(line)
    keys = [
        "kind", "reason", "market", "strat", "full_pnl", "full_pf", "full_dd", "cushion",
        "evalstop", "evalpnl", "evalcush", "evaltrades", "evaldays", "evalcons", "evalbtrade", "roll", "rollpr",
        "rollbr", "rollcush", "rb", "rcl", "m", "mmin", "wf", "wfmin", "wfbr",
    ]
    parts = [f"{k}={kv[k]}" for k in keys if k in kv]
    return " ".join(parts) if parts else line


def print_report(parsed):
    print("QC Apex Log Report")
    print(f"summary: {first_or_none(parsed['summary'])}")
    print("locked: " + first_or_none(parsed["locked"]))
    print("locked_metrics: " + compact_metrics(first_or_none(parsed["locked"])))
    print(f"reason: {first_or_none(parsed['reason'])}")
    print(f"ensemble_reason: {first_or_none(parsed['ensemble_reason'])}")
    print(f"apex_reason: {first_or_none(parsed['apex_reason'])}")
    print(f"ensemble_apex_reason: {first_or_none(parsed['ensemble_apex_reason'])}")
    print(
        "top_blockers: "
        + " ".join([
            blocker_text("single_robust", first_or_none(parsed["reason"])),
            blocker_text("single_apex", first_or_none(parsed["apex_reason"])),
            blocker_text("ensemble_robust", first_or_none(parsed["ensemble_reason"])),
            blocker_text("ensemble_apex", first_or_none(parsed["ensemble_apex_reason"])),
        ])
    )
    print("final: " + first_or_none(parsed["final"]))
    print("final_metrics: " + compact_metrics(first_or_none(parsed["final"])))
    print(f"robust_passes: {len(parsed['robust'])}")
    print(f"ensemble_robust_passes: {len(parsed['ensemble_robust'])}")
    print(f"apex_ready: {len(parsed['apex_ready'])}")
    print(f"ensemble_apex_ready: {len(parsed['ensemble_apex_ready'])}")
    print(f"eval_passes: {len(parsed['eval'])}")
    print(f"ensemble_eval_passes: {len(parsed['ensemble_eval'])}")

    near_counts = count_near_reasons(parsed["near"])
    ensemble_near_counts = count_near_reasons(parsed["ensemble_near"])
    if near_counts:
        print("near_pass_reasons: " + " ".join(f"{k}={v}" for k, v in near_counts.most_common()))
    if ensemble_near_counts:
        print("ensemble_near_pass_reasons: " + " ".join(f"{k}={v}" for k, v in ensemble_near_counts.most_common()))

    print("best_robust: " + first_or_none(parsed["robust"]))
    print("best_ensemble_robust: " + first_or_none(parsed["ensemble_robust"]))
    print("best_eval: " + first_or_none(parsed["eval"]))
    print("best_ensemble_eval: " + first_or_none(parsed["ensemble_eval"]))
    print("best_pick: " + first_or_none(parsed["pick"]))
    print("best_pick_metrics: " + compact_metrics(first_or_none(parsed["pick"])))
    print("best_ensemble: " + first_or_none(parsed["ensemble"]))
    print("best_ensemble_metrics: " + compact_metrics(first_or_none(parsed["ensemble"])))


def read_input(path):
    if path:
        return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def self_test():
    sample = """
    2026 SEARCH_SUMMARY streams=MES:1000 days=250 markets=6 configs=78336 shortlist=400 pass=2 robust=1 ensembles=80 ensemble_pass=1 ensemble_robust=0 split=2025-10-01
    SEARCH_REASON no_target=300 full_breach=20 bad_months=2 ok=1
    SEARCH_APEX_REASON no_target=250 eval_breach=10 ok=2
    SEARCH_ENSEMBLE_REASON full_breach=8 low_pf=2
    SEARCH_ENSEMBLE_APEX_REASON no_target=7 ok=1
    SEARCH_ROBUST_PASS rank=1 cfg='MES test'
    SEARCH_APEX_READY rank=1 cfg='MES apex'
    SEARCH_EVAL_PASS rank=1 cfg='MES eval'
    SEARCH_NEAR_PASS reason=full_breach rank=1 cfg='MNQ test'
    SEARCH_ENSEMBLE_NEAR_PASS reason=low_pf rank=1 members='MES > MNQ'
    SEARCH_ENSEMBLE_EVAL_PASS rank=1 members='MES > MNQ'
    SEARCH_ENSEMBLE_APEX_READY rank=1 members='MES > MNQ'
    SEARCH_FINAL kind=single_apex_ready rank=1 market=MES strat=test full_pnl=3300 full_pf=1.42 full_dd=-1100 cushion=900 evalstop=True evalpnl=3050 evalcush=800 evaltrades=9 evaldays=6 evalcons=29% evalbtrade=21% roll=4/180 rollpr=2.2 rollbr=12.0 rollcush=550 rb=2 rcl=50% m=5/12 mmin=-500 wf=4/5 wfmin=-500 wfbr=0
    LOCKED_FINAL reason=ok rank=1 members='NQ > NQ' full_profit=8522 full_pf=2.64 full_dd=-1599 full_evalstop=True full_evalpnl=3409 roll=34/237 roll_passrate=14.3
    SEARCH_FINAL kind=none reason=no_clean_eval_stop_pass
    SEARCH_PICK rank=1 market=MES strat=test full_pnl=3300 full_pf=1.42 full_dd=-1100 cushion=900 bestday=850 cons=24% stress=700 stresspf=1.15 stressbr=False evalstop=True evalpnl=3050 evalcush=800 evaltrades=9 evaldays=6 evalcons=29% evalbtrade=21% evalday=2025-06-01 path=150 pathdd=-900 pathbr=False nbr=3/4 nbrpass=1 nbrmin=-50 roll=4/180 rollpr=2.2 rollbr=12.0 rollcush=550 rb=2 rcl=50% m=5/12 mmin=-500 wf=4/5 wfmin=-500 wfbr=0
    SEARCH_ENSEMBLE rank=1 members='MES > MNQ'
    """
    parsed = parse_text(sample)
    assert len(parsed["robust"]) == 1
    assert len(parsed["apex_ready"]) == 1
    assert len(parsed["eval"]) == 1
    assert len(parsed["ensemble_eval"]) == 1
    assert len(parsed["ensemble_apex_ready"]) == 1
    assert len(parsed["final"]) == 2
    assert len(parsed["locked"]) == 1
    assert "bad_months=2" in parsed["reason"][0]
    assert "eval_breach=10" in parsed["apex_reason"][0]
    assert numeric_counts(parsed["reason"][0])["no_target"] == 300
    assert "single_robust=no_target:300" in blocker_text("single_robust", parsed["reason"][0])
    assert "single_apex=no_target:250" in blocker_text("single_apex", parsed["apex_reason"][0])
    assert "stress=700" in parsed["pick"][0]
    assert "cons=24%" in parsed["pick"][0]
    assert "evalstop=True" in parsed["pick"][0]
    assert "evalcush=800" in parsed["pick"][0]
    assert "evaltrades=9" in parsed["pick"][0]
    assert "evaldays=6" in parsed["pick"][0]
    assert "evalcons=29%" in parsed["pick"][0]
    assert "evalbtrade=21%" in parsed["pick"][0]
    assert "path=150" in parsed["pick"][0]
    assert "nbr=3/4" in parsed["pick"][0]
    assert "wf=4/5" in parsed["pick"][0]
    metrics = compact_metrics(parsed["pick"][0])
    final_metrics = compact_metrics(parsed["final"][0])
    none_final_metrics = compact_metrics(parsed["final"][1])
    assert "market=MES" in metrics
    assert "market=MES" in final_metrics
    assert "kind=none" in none_final_metrics
    assert "reason=no_clean_eval_stop_pass" in none_final_metrics
    assert "evalstop=True" in final_metrics
    assert "evalcush=800" in metrics
    assert "rollcush=550" in metrics
    assert "rb=2" in metrics
    assert "rcl=50%" in metrics
    assert count_near_reasons(parsed["near"])["full_breach"] == 1
    assert count_near_reasons(parsed["ensemble_near"])["low_pf"] == 1
    print("parse_qc_apex_log: ok")


def main():
    parser = argparse.ArgumentParser(description="Summarize qc_apex_es_vwap_orb SEARCH_* backtest logs.")
    parser.add_argument("path", nargs="?", help="Optional QuantConnect log file. Reads stdin when omitted.")
    parser.add_argument("--self-test", action="store_true", help="Run parser self-test.")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    print_report(parse_text(read_input(args.path)))


if __name__ == "__main__":
    main()
