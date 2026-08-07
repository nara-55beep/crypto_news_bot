import argparse
import pathlib
import re
import sys


KV_RE = re.compile(r"([A-Za-z_]+)=([^\s|']+)")


def read_text(path):
    if path:
        return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def find_locked_line(text):
    for raw in text.splitlines():
        if "LOCKED_FINAL" in raw:
            return raw.strip()
    return ""


def kv(line):
    return dict(KV_RE.findall(line))


def as_float(values, key, default=0.0):
    value = values.get(key)
    if value is None:
        return default
    value = value.replace(",", "").replace("%", "")
    try:
        return float(value)
    except ValueError:
        return default


def gate(values):
    checks = [
        ("eval_stop_pass", values.get("full_evalstop") == "True"),
        ("eval_pnl_3000", as_float(values, "full_evalpnl") >= 3000.0),
        ("eval_cushion_250", as_float(values, "full_evalcush") >= 250.0),
        ("full_pass", values.get("full_pass") == "True"),
        ("positive_stress", as_float(values, "full_stress") > 0.0),
    ]
    ok = all(passed for _, passed in checks)
    return ok, checks


def main():
    parser = argparse.ArgumentParser(description="Pass/fail gate for qc_apex_es_vwap_orb LOCKED_FINAL logs.")
    parser.add_argument("path", nargs="?", help="Optional QC log file. Reads stdin when omitted.")
    parser.add_argument("--self-test", action="store_true", help="Run a small parser/gate self-test.")
    args = parser.parse_args()

    if args.self_test:
        text = "LOCKED_FINAL reason=lucky_pass_trade rank=1 full_pass=True full_profit=6861 full_trades=80 full_pf=1.76 full_dd=-2638 full_breachday=2025-08-15 full_stress=4224 full_evalstop=True full_evalpnl=3163 full_evalcush=1513 roll=0/237 roll_breachrate=0.0"
    else:
        text = read_text(args.path)

    line = find_locked_line(text)
    if not line:
        print("QC_APEX_LOCKED_GATE status=fail reason=missing_LOCKED_FINAL")
        raise SystemExit(1)

    values = kv(line)
    ok, checks = gate(values)
    status = "pass" if ok else "fail"
    print(f"QC_APEX_LOCKED_GATE status={status}")
    print(
        "metrics "
        f"evalpnl={values.get('full_evalpnl')} full_profit={values.get('full_profit')} "
        f"dd={values.get('full_dd')} pf={values.get('full_pf')} trades={values.get('full_trades')} "
        f"roll={values.get('roll')} roll_breachrate={values.get('roll_breachrate')} stress={values.get('full_stress')}"
    )
    print("checks " + " ".join(f"{name}={passed}" for name, passed in checks))
    print("line " + line)
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
