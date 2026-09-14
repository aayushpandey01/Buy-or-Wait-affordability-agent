from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional, Tuple

from .engine import (UserContext, HORIZON_DAYS, compute_checkpoints,
                      min_balance_over_horizon, find_earliest_full_payment_date,
                      simulate_plan_safety, CATEGORY_LABELS)
from .formatting import fmt_plain, fmt_display, fmt_date, fmt_date_long


def find_spending_change_rescue(ctx: UserContext, base_items: List[dict],
                                 current_balance: float, min_balance_to_keep: float,
                                 request_date: date, forecast_end: date,
                                 requested_amount: float, max_changes: int = 3):
    
    checkpoints = compute_checkpoints(base_items, current_balance, request_date, forecast_end)
    if min_balance_over_horizon(checkpoints) >= min_balance_to_keep + requested_amount:
        return base_items, []  # already safe with zero changes (shouldn't normally be reached)

    candidates = []
    by_category: dict = {}
    for it in base_items:
        by_category.setdefault(it["category"], []).append(it)

    for cat, occs in by_category.items():
        if cat in ctx.protect_categories:
            continue
        flex = occs[0].get("flexibility")
        rep_event_id = None
        for o in occs:
            if o.get("event_id"):
                rep_event_id = o["event_id"]
        best_action = None
        best_relief = 0.0
        best_new_amount = None
        if cat in ctx.stop_categories and flex in ("stoppable", "reducible_or_stoppable"):
            relief = sum(abs(o["amount"]) for o in occs)
            if relief > best_relief:
                best_relief, best_action, best_new_amount = relief, "stop", 0.0
        if cat in ctx.reduce_categories and flex in ("reducible", "reducible_or_stoppable"):
            min_allowed = occs[0].get("min_allowed")
            if min_allowed is not None:
                relief = sum(max(0.0, abs(o["amount"]) - min_allowed) for o in occs)
                if relief > best_relief:
                    best_relief, best_action, best_new_amount = relief, "reduce", min_allowed
        if best_action and rep_event_id:
            candidates.append({
                "category": cat, "action": best_action, "relief": best_relief,
                "event_id": rep_event_id, "new_amount": best_new_amount,
            })

    candidates.sort(key=lambda c: -c["relief"])

    chosen = []
    working_items = [dict(it) for it in base_items]
    for cand in candidates:
        if len(chosen) >= max_changes:
            break
        if cand["relief"] <= 0:
            continue
        for it in working_items:
            if it["category"] == cand["category"]:
                if cand["action"] == "stop":
                    it["amount"] = 0.0
                else:
                    sign = -1 if it["amount"] < 0 else 1
                    it["amount"] = sign * cand["new_amount"]
        chosen.append(cand)
        checkpoints = compute_checkpoints(working_items, current_balance, request_date, forecast_end)
        if min_balance_over_horizon(checkpoints) >= min_balance_to_keep + requested_amount:
            return working_items, chosen

    return None, None


def _category_label(cat: str) -> str:
    return CATEGORY_LABELS.get(cat, cat.replace("_", " "))


def describe_changes(changes: List[dict]) -> str:
    parts = []
    for c in changes:
        label = _category_label(c["category"])
        if c["action"] == "stop":
            parts.append(f"Stop the {label}")
        else:
            parts.append(f"reduce the {label} to {{amt:{c['event_id']}}}")
    return parts


def build_decision(ctx: UserContext, request: dict, payment_options: List[dict]) -> dict:
    request_date: date = request["request_date"]
    desired_completion_date: date = request["desired_completion_date"]
    requested_amount: float = float(request["requested_amount"])
    allows_partial = bool(request["allows_partial_payment"])
    forecast_end = request_date + timedelta(days=HORIZON_DAYS)

    profile = ctx.profile
    current_balance = float(profile["current_available_balance"])
    min_balance_to_keep = float(profile["minimum_balance_to_keep"])
    home_ccy = ctx.home_ccy

    base_items = ctx.build_forecast_items(request_date)
    checkpoints = compute_checkpoints(base_items, current_balance, request_date, forecast_end)
    min_bal = min_balance_over_horizon(checkpoints)

    amount_safe_to_pay = max(0.0, min(requested_amount, min_bal - min_balance_to_keep))
    edfp = find_earliest_full_payment_date(checkpoints, min_balance_to_keep, requested_amount)

    candidates = []

    # 1a. full_payment, zero changes
    if "full_payment" in ctx.accepted_methods and edfp == request_date:
        candidates.append({
            "method": "full_payment", "payments": [(request_date, requested_amount)],
            "requires_changes": False, "changes": [], "payment_option_id": None,
        })

    # 1b. full_payment rescued by spending changes
    if "full_payment" in ctx.accepted_methods and edfp != request_date:
        rescued_items, changes = find_spending_change_rescue(
            ctx, base_items, current_balance, min_balance_to_keep, request_date,
            forecast_end, requested_amount)
        if rescued_items is not None and changes:
            candidates.append({
                "method": "full_payment", "payments": [(request_date, requested_amount)],
                "requires_changes": True, "changes": changes, "payment_option_id": None,
            })

    # 2. partial_payment
    if (allows_partial and "partial_payment" in ctx.accepted_methods and
            0 < amount_safe_to_pay < requested_amount and edfp is not None and
            edfp <= desired_completion_date):
        candidates.append({
            "method": "partial_payment",
            "payments": [(request_date, amount_safe_to_pay),
                         (edfp, requested_amount - amount_safe_to_pay)],
            "requires_changes": False, "changes": [], "payment_option_id": None,
        })

    # 3. installments (must match a supplied option exactly)
    if "installments" in ctx.accepted_methods:
        max_months = profile.get("max_installment_months")
        max_months = float(max_months) if max_months is not None and str(max_months) != "nan" else None
        for opt in payment_options:
            if opt["payment_method"] != "installments":
                continue
            n = int(opt["number_of_payments"])
            if max_months is not None and n > max_months:
                continue
            freq = int(opt["payment_frequency_days"]) if opt.get("payment_frequency_days") else 30
            first = opt["first_payment_date"]
            payments = [(first + timedelta(days=freq * i), float(opt["payment_amount"]))
                        for i in range(n)]
            last_date = payments[-1][0]
            if last_date > desired_completion_date:
                continue
            if simulate_plan_safety(base_items, payments, current_balance, min_balance_to_keep,
                                     request_date, forecast_end):
                candidates.append({
                    "method": "installments", "payments": payments,
                    "requires_changes": False, "changes": [],
                    "payment_option_id": opt["payment_option_id"],
                })

    # 4. wait
    if ("full_payment" in ctx.accepted_methods and edfp is not None and
            edfp > request_date and edfp <= desired_completion_date):
        candidates.append({
            "method": "wait", "payments": [(edfp, requested_amount)],
            "requires_changes": False, "changes": [], "payment_option_id": None,
        })

    def sort_key(c):
        opt_id = c["payment_option_id"]
        opt_num = int(opt_id.split("_")[-1]) if opt_id else 0
        total_paid = sum(a for _, a in c["payments"])
        first_date = c["payments"][0][0]
        n_payments = len(c["payments"])
        deadline_ok = all(d <= desired_completion_date for d, _ in c["payments"])
        return (
            0 if deadline_ok else 1,
            1 if c["requires_changes"] else 0,
            total_paid,
            first_date,
            n_payments,
            opt_num,
        )

    candidates.sort(key=sort_key)

    if not candidates:
        status = "not_affordable"
        method = "not_recommended"
        payment_plan_str = "none"
        spending_changes_str = "none"
        if edfp is not None:
            explanation = (
                f"Do not make this payment by {fmt_date_long(desired_completion_date)}. "
                f"None of the available options keeps the {home_ccy} {fmt_display(min_balance_to_keep)} "
                f"minimum protected."
            )
        else:
            explanation = (
                f"Do not proceed with the {home_ccy} {fmt_display(requested_amount)} request. "
                f"Although {home_ccy} {fmt_display(amount_safe_to_pay)} is available today, the full "
                f"amount cannot be completed safely within 90 days."
            )
        return {
            "amount_safe_to_pay": round(amount_safe_to_pay, 2),
            "affordability_status": status,
            "recommended_payment_method": method,
            "payment_plan": payment_plan_str,
            "earliest_date_for_full_payment": fmt_date(edfp) if edfp else "",
            "spending_changes_needed": spending_changes_str,
            "decision_explanation": explanation,
        }

    winner = candidates[0]
    method = winner["method"]
    payments = winner["payments"]

    if method == "full_payment" and not winner["requires_changes"] and payments[0][0] == request_date:
        status = "affordable_now"
    elif winner["requires_changes"] or method in ("partial_payment", "installments"):
        status = "affordable_with_plan"
    elif method == "wait":
        status = "affordable_later"
    else:
        status = "affordable_with_plan"

    payment_plan_str = "|".join(f"{fmt_date(d)}:{fmt_plain(a)}" for d, a in payments)

    if winner["changes"]:
        spending_changes_str = "|".join(
            (f"stop:{c['event_id']}" if c["action"] == "stop"
             else f"reduce_to:{c['event_id']}:{fmt_plain(c['new_amount'])}")
            for c in winner["changes"]
        )
    else:
        spending_changes_str = "none"

    # decision_explanation
    if method == "full_payment" and winner["requires_changes"]:
        change_phrases = []
        for c in winner["changes"]:
            label = _category_label(c["category"])
            if c["action"] == "stop":
                change_phrases.append(f"Stop the {label}")
            else:
                change_phrases.append(f"reduce the {label} to {home_ccy} {fmt_display(c['new_amount'])}")
        prefix = " and ".join(change_phrases)
        explanation = (f"{prefix}, then pay {home_ccy} {fmt_display(requested_amount)} today. "
                        f"This leaves at least {home_ccy} {fmt_display(min_balance_to_keep)} available.")
    elif method == "full_payment":
        explanation = (f"Pay {home_ccy} {fmt_display(requested_amount)} today. This leaves at least "
                        f"{home_ccy} {fmt_display(min_balance_to_keep)} available over the next 90 days.")
    elif method == "partial_payment":
        explanation = (f"Pay {home_ccy} {fmt_display(payments[0][1])} today and the remaining "
                        f"{home_ccy} {fmt_display(payments[1][1])} on {fmt_date_long(payments[1][0])}. "
                        f"This completes the full request and keeps the {home_ccy} "
                        f"{fmt_display(min_balance_to_keep)} minimum protected.")
    elif method == "installments":
        explanation = (f"Use {len(payments)} installments of {home_ccy} {fmt_display(payments[0][1])}, "
                        f"starting {fmt_date_long(payments[0][0])}. This leaves at least {home_ccy} "
                        f"{fmt_display(min_balance_to_keep)} available.")
    elif method == "wait":
        explanation = (f"Pay {home_ccy} {fmt_display(requested_amount)} in full on {fmt_date_long(payments[0][0])}. "
                        f"Paying earlier would take the balance below the {home_ccy} "
                        f"{fmt_display(min_balance_to_keep)} minimum.")
    else:
        explanation = ""

    return {
        "amount_safe_to_pay": round(amount_safe_to_pay, 2),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": payment_plan_str,
        "earliest_date_for_full_payment": fmt_date(edfp) if edfp else "",
        "spending_changes_needed": spending_changes_str,
        "decision_explanation": explanation,
    }
