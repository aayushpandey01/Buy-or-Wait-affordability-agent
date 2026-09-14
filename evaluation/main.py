from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.main import run

FIELDS = [
    "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
    "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed",
]


def _num_close(a, b, rel=0.05, abs_tol=1.0):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b), 1.0))


def score(pred_path: str, truth_path: str) -> dict:
    pred = pd.read_csv(pred_path).set_index("request_id")
    truth = pd.read_csv(truth_path).set_index("request_id")
    truth = truth.loc[[i for i in truth.index if i in pred.index]]

    per_field_correct = {f: 0 for f in FIELDS}
    n = len(truth)
    rows_report = []
    for rid, trow in truth.iterrows():
        prow = pred.loc[rid]
        row_report = {"request_id": rid}
        for f in FIELDS:
            tv = trow[f]
            pv = prow[f]
            if f == "amount_safe_to_pay":
                ok = _num_close(tv, pv)
            elif f in ("payment_plan", "spending_changes_needed"):
                ok = str(tv).strip() == str(pv).strip()
            elif f == "earliest_date_for_full_payment":
                tv_s = "" if pd.isna(tv) else str(tv)
                pv_s = "" if pd.isna(pv) else str(pv)
                ok = tv_s == pv_s
            else:
                ok = str(tv).strip() == str(pv).strip()
            per_field_correct[f] += int(ok)
            row_report[f + "_ok"] = ok
        rows_report.append(row_report)

    report = {f: per_field_correct[f] / n for f in FIELDS}
    report["n"] = n
    return report, pd.DataFrame(rows_report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset"))
    args = parser.parse_args()

    sample_path = os.path.join(args.data_dir, "sample_requests.csv")
    pred_path = os.path.join(os.getcwd(), "tmp_eval_output.csv")
    run(args.data_dir, sample_path, pred_path)

    report, detail = score(pred_path, sample_path)
    print("Evaluation against dataset/sample_requests.csv (n=%d):" % report["n"])
    for f in FIELDS:
        print(f"  {f:32s}: {report[f]*100:5.1f}%")
    detail_path = os.path.join(os.getcwd(), "tmp_eval_detail.csv")
    os.makedirs(os.path.dirname(detail_path), exist_ok=True)
    detail.to_csv(detail_path, index=False)
    print(f"\nPer-row detail written to {detail_path}")


if __name__ == "__main__":
    main()
