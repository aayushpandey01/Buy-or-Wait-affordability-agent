from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.loaders import load_dataset
from src.fx import load_fx_table
from src.engine import UserContext
from src.decision import build_decision

OUTPUT_COLUMNS = [
    "request_id", "amount_safe_to_pay", "affordability_status",
    "recommended_payment_method", "payment_plan", "earliest_date_for_full_payment",
    "spending_changes_needed", "decision_explanation",
]


def run(data_dir: str, requests_path: str, output_path: str) -> pd.DataFrame:
    ds = load_dataset(data_dir, requests_path=requests_path)
    fx = load_fx_table(os.path.join(data_dir, "exchange_rates.csv"))

    profiles_by_user = {row["user_id"]: row for _, row in ds.profiles.iterrows()}
    options_by_request = {}
    for _, row in ds.payment_options.iterrows():
        options_by_request.setdefault(row["request_id"], []).append(row.to_dict())

    context_cache = {}
    rows = []
    for _, req in ds.requests.iterrows():
        user_id = req["user_id"]
        if user_id not in context_cache:
            profile = profiles_by_user[user_id]
            context_cache[user_id] = UserContext(user_id, profile, ds.events, ds.messages, fx)
        ctx = context_cache[user_id]

        options = options_by_request.get(req["request_id"], [])
        result = build_decision(ctx, req.to_dict(), options)
        result["request_id"] = req["request_id"]
        rows.append(result)

    out_df = pd.DataFrame(rows)[OUTPUT_COLUMNS]
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    out_df.to_csv(output_path, index=False)
    return out_df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="dataset")
    parser.add_argument("--requests", default=None,
                         help="Path to the requests CSV to score (defaults to dataset/requests.csv)")
    parser.add_argument("--output", default="output.csv")
    args = parser.parse_args()

    requests_path = args.requests or os.path.join(args.data_dir, "requests.csv")
    run(args.data_dir, requests_path, args.output)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
