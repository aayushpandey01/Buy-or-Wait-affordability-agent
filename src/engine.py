from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .fx import FXTable
from .message_rules import build_user_directives, SalaryDirective, RentDirective, OneOffIncome

HORIZON_DAYS = 90

# Categories that behave as recurring cash flows (projected forward using
# inferred cadence). Everything else (refund, investment_sale, windfall) is
# one-off: included only when a concrete qualifying event exists, never
# extrapolated. investment_valuation is always excluded (non-cash).
RECURRING_EVENT_TYPES = {"expense", "debt_payment", "subscription"}

CATEGORY_LABELS = {
    "streaming": "streaming subscription",
    "music_subscription": "music subscription",
    "cloud_storage": "online backup subscription",
    "delivery_membership": "delivery membership",
    "gym": "gym membership",
    "dining": "dining spending",
    "entertainment": "entertainment spending",
    "shopping": "discretionary shopping",
    "groceries": "grocery spending",
    "transport": "transport spending",
    "family_support": "family support payments",
    "work_expense": "work expense spending",
    "housing": "housing spending",
    "rent": "rent",
}


def _qualifies(status: str, direction: str) -> bool:
    """Whether an event row counts toward cadence/baseline & the forecast at all."""
    if status in ("cancelled", "failed", "unrealized"):
        return False
    if status == "pending" and direction == "credit":
        return False
    return True


def _role(event_type: str, category: str) -> str:
    if event_type == "investment_valuation":
        return "excluded"
    if event_type in ("refund", "investment_sale"):
        return "one_off"
    if event_type == "income" and category == "windfall":
        return "one_off"
    if event_type in RECURRING_EVENT_TYPES or event_type == "investment_purchase" or \
       (event_type == "income" and category == "salary"):
        return "recurring"
    return "one_off"


def _signed(amount: float, direction: str) -> float:
    return amount if direction == "credit" else -amount


class UserContext:
    """Precomputed per-user financial context, reused across the (usually
    single) request that references this user."""

    def __init__(self, user_id: str, profile: pd.Series, events: pd.DataFrame,
                 messages: pd.DataFrame, fx: FXTable):
        self.user_id = user_id
        self.profile = profile
        self.home_ccy = profile["home_currency"]
        self.fx = fx
        self.directives = build_user_directives(messages, user_id)

        my_events = events[events["user_id"] == user_id].copy()
        my_events = my_events[my_events.apply(
            lambda r: _qualifies(r["status"], r["direction"]), axis=1)]
        my_events["role"] = my_events.apply(
            lambda r: _role(r["event_type"], r["category"]), axis=1)
        my_events = my_events[my_events["role"] != "excluded"]

        # Convert every amount to home currency at its own event_date.
        def to_home(row):
            return fx.convert(row["amount"], row["currency"], self.home_ccy, row["event_date"])

        my_events = my_events.copy()
        my_events["home_amount"] = my_events.apply(to_home, axis=1)
        my_events["signed_home_amount"] = my_events.apply(
            lambda r: _signed(r["home_amount"], r["direction"]), axis=1)
        my_events["min_allowed_home"] = my_events.apply(
            lambda r: (fx.convert(r["minimum_allowed_amount"], r["currency"], self.home_ccy, r["event_date"])
                       if pd.notna(r["minimum_allowed_amount"]) else None), axis=1)

        self.events = my_events.sort_values("event_date")

        protect = str(profile.get("expense_categories_to_protect") or "")
        reduce_ = str(profile.get("expense_categories_user_is_willing_to_reduce") or "")
        stop_ = str(profile.get("expense_categories_user_is_willing_to_stop") or "")
        self.protect_categories = set(x for x in protect.split("|") if x)
        self.reduce_categories = set(x for x in reduce_.split("|") if x)
        self.stop_categories = set(x for x in stop_.split("|") if x)
        methods = str(profile.get("payment_methods_user_will_consider") or "")
        self.accepted_methods = set(x for x in methods.split("|") if x)

    def build_category_occurrences(self, category: str, request_date: date,
                                    forecast_end: date) -> Tuple[List[dict], Optional[dict]]:
        rows = self.events[(self.events["category"] == category) &
                            (self.events["role"] == "recurring")]
        items = [{
            "date": r["event_date"], "amount": r["signed_home_amount"],
            "event_id": r["event_id"], "flexibility": r["flexibility"],
            "min_allowed": r["min_allowed_home"], "category": category,
            "projected": False,
        } for _, r in rows.iterrows()]
        items.sort(key=lambda x: x["date"])
        if not items:
            return [], None

        dates = [it["date"] for it in items]
        if len(dates) >= 2:
            diffs = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
            diffs = [d for d in diffs if d > 0][-12:] or [30]
            diffs_sorted = sorted(diffs)
            if len(diffs_sorted) >= 4:
                q1 = diffs_sorted[len(diffs_sorted) // 4]
                q3 = diffs_sorted[(3 * len(diffs_sorted)) // 4]
                iqr = max(q3 - q1, 1)
                filtered = [d for d in diffs_sorted if q1 - 1.5 * iqr <= d <= q3 + 1.5 * iqr]
                cadence = int(round(median(filtered if filtered else diffs_sorted)))
            else:
                cadence = int(round(median(diffs_sorted)))
            cadence = max(7, min(cadence, 90))
        else:
            cadence = 30

        last_n = items[-6:]
        amt_sorted = sorted(it["amount"] for it in last_n)
        mid = len(amt_sorted) // 2
        if len(amt_sorted) % 2:
            baseline_amount = amt_sorted[mid]
        else:
            baseline_amount = (amt_sorted[mid - 1] + amt_sorted[mid]) / 2
        if len(amt_sorted) >= 4:
            q1 = amt_sorted[len(amt_sorted) // 4]
            q3 = amt_sorted[(3 * len(amt_sorted)) // 4]
            iqr = max(q3 - q1, 1e-9)
            filtered = [a for a in amt_sorted if q1 - 1.5 * iqr <= a <= q3 + 1.5 * iqr]
            if filtered:
                base_med = median(filtered)
                baseline_amount = base_med
        anchor = last_n[-1]
        anchor_date = anchor["date"]

        concrete = [it for it in items if request_date <= it["date"] <= forecast_end]
        existing_dates = {it["date"] for it in concrete}
        projected = []
        k = 1
        while True:
            d = anchor_date + timedelta(days=cadence * k)
            if d > forecast_end:
                break
            if d >= request_date and d not in existing_dates:
                projected.append({
                    "date": d, "amount": baseline_amount, "event_id": anchor["event_id"],
                    "flexibility": anchor["flexibility"], "min_allowed": anchor["min_allowed"],
                    "category": category, "projected": True,
                })
            k += 1
            if k > 500:
                break

        occurrences = concrete + projected
        occurrences.sort(key=lambda x: x["date"])
        meta = {"cadence": cadence, "baseline": baseline_amount, "anchor_date": anchor_date,
                "anchor_event_id": anchor["event_id"], "flexibility": anchor["flexibility"],
                "min_allowed": anchor["min_allowed"]}
        return occurrences, meta

    def get_one_off_items(self, request_date: date, forecast_end: date) -> List[dict]:
        rows = self.events[(self.events["role"] == "one_off") &
                            (self.events["event_date"] >= request_date) &
                            (self.events["event_date"] <= forecast_end)]
        items = [{"date": r["event_date"], "amount": r["signed_home_amount"],
                  "event_id": r["event_id"], "flexibility": "fixed", "min_allowed": None,
                  "category": r["category"], "projected": False}
                 for _, r in rows.iterrows()]
        return items

    def get_all_categories(self) -> List[str]:
        return sorted(self.events[self.events["role"] == "recurring"]["category"].unique().tolist())

    def apply_salary_directives(self, occurrences: List[dict], request_date: date,
                                 forecast_end: date) -> List[dict]:
        occurrences = list(occurrences)
        for d in self.directives.salary:
            if d.kind == "set_baseline":
                amt_home = self.fx.convert(d.amount, d.currency or self.home_ccy,
                                            self.home_ccy, d.effective_date)
                for o in occurrences:
                    if o["date"] >= d.effective_date:
                        o["amount"] = amt_home
            elif d.kind == "new_series":
                amt_home = self.fx.convert(d.amount, d.currency or self.home_ccy,
                                            self.home_ccy, d.effective_date)
                occurrences = [o for o in occurrences if o["date"] < d.effective_date]
                dd = d.effective_date
                while dd <= forecast_end:
                    if dd >= request_date:
                        occurrences.append({"date": dd, "amount": amt_home, "event_id": None,
                                             "flexibility": "fixed", "min_allowed": None,
                                             "category": "salary", "projected": True})
                    dd = dd + timedelta(days=30)
            elif d.kind == "stop":
                occurrences = [o for o in occurrences if o["date"] < d.effective_date]
            elif d.kind == "one_off_cycle":
                amt_home = self.fx.convert(d.amount, d.currency or self.home_ccy,
                                            self.home_ccy, d.sent_at)
                future = sorted([o for o in occurrences if o["date"] >= d.sent_at],
                                 key=lambda x: x["date"])
                if future:
                    future[0]["amount"] = amt_home
            elif d.kind == "shift_date":
                future = sorted([o for o in occurrences if o["date"] >= d.sent_at],
                                 key=lambda x: x["date"])
                if future:
                    future[0]["date"] = d.effective_date
            elif d.kind == "add_one_off":
                amt_home = self.fx.convert(d.amount, d.currency or self.home_ccy,
                                            self.home_ccy, d.effective_date)
                if request_date <= d.effective_date <= forecast_end:
                    occurrences.append({"date": d.effective_date, "amount": amt_home,
                                         "event_id": None, "flexibility": "fixed",
                                         "min_allowed": None, "category": "salary",
                                         "projected": True})
        occurrences.sort(key=lambda x: x["date"])
        return occurrences

    def apply_rent_directives(self, occurrences: List[dict]) -> List[dict]:
        for d in self.directives.rent:
            for o in occurrences:
                if o["date"] > d.effective_after:
                    o["amount"] *= d.multiplier
        return occurrences

    def build_forecast_items(self, request_date: date) -> List[dict]:
        forecast_end = request_date + timedelta(days=HORIZON_DAYS)
        all_items: List[dict] = []
        categories = set(self.get_all_categories()) | {"salary", "rent", "housing"}
        for cat in categories:
            occ, meta = self.build_category_occurrences(cat, request_date, forecast_end)
            if cat == "salary":
                occ = self.apply_salary_directives(occ, request_date, forecast_end)
            if cat in ("rent", "housing"):
                occ = self.apply_rent_directives(occ)
            all_items.extend(occ)
        all_items.extend(self.get_one_off_items(request_date, forecast_end))
        for oi in self.directives.one_off_incomes:
            if request_date <= oi.date <= forecast_end:
                amt_home = self.fx.convert(oi.amount, oi.currency, self.home_ccy, oi.date)
                all_items.append({"date": oi.date, "amount": amt_home, "event_id": None,
                                   "flexibility": "fixed", "min_allowed": None,
                                   "category": "other_income", "projected": True})
        return all_items


def compute_checkpoints(items: List[dict], current_balance: float, request_date: date,
                         forecast_end: date) -> List[Tuple[date, float]]:
    by_date: Dict[date, float] = {}
    for it in items:
        by_date.setdefault(it["date"], 0.0)
        by_date[it["date"]] += it["amount"]
    dates = sorted(by_date.keys())
    checkpoints = []
    if request_date not in by_date:
        checkpoints.append((request_date, current_balance))
    running = current_balance
    for d in dates:
        running += by_date[d]
        checkpoints.append((d, running))
    checkpoints.sort(key=lambda x: x[0])
    return checkpoints


def min_balance_over_horizon(checkpoints: List[Tuple[date, float]]) -> float:
    return min(bal for _, bal in checkpoints)


def suffix_min_map(checkpoints: List[Tuple[date, float]]) -> Dict[date, float]:
    result = {}
    running = float("inf")
    for d, bal in reversed(checkpoints):
        running = min(running, bal)
        result[d] = running
    return result


def find_earliest_full_payment_date(checkpoints: List[Tuple[date, float]],
                                     min_balance_to_keep: float,
                                     requested_amount: float) -> Optional[date]:
    suffix = suffix_min_map(checkpoints)
    threshold = min_balance_to_keep + requested_amount
    for d, _ in checkpoints:
        if suffix[d] >= threshold:
            return d
    return None


def simulate_plan_safety(base_items: List[dict], payments: List[Tuple[date, float]],
                          current_balance: float, min_balance_to_keep: float,
                          request_date: date, forecast_end: date) -> bool:
    items = list(base_items)
    horizon_end = max(forecast_end, max((p[0] for p in payments), default=forecast_end))
    for pdate, pamt in payments:
        items.append({"date": pdate, "amount": -pamt})
    checkpoints = compute_checkpoints(items, current_balance, request_date, horizon_end)
    return min_balance_over_horizon(checkpoints) >= min_balance_to_keep
