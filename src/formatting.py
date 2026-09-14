from __future__ import annotations
from datetime import date


def fmt_plain(x: float) -> str:
    """Plain numeric formatting used inside payment_plan / spending_changes_needed
    fields, e.g. 25256 or 15952906.67 (no thousands separators)."""
    r = round(float(x), 2)
    if abs(r - round(r)) < 1e-9:
        return str(int(round(r)))
    return f"{r:.2f}"


def fmt_display(x: float) -> str:
    """Human-readable formatting used inside decision_explanation, e.g.
    25,256 or 620.40."""
    r = round(float(x), 2)
    if abs(r - round(r)) < 1e-9:
        return f"{int(round(r)):,}"
    return f"{r:,.2f}"


def fmt_date(d) -> str:
    if d is None or d == "":
        return ""
    if isinstance(d, str):
        return d
    return d.strftime("%Y-%m-%d")


def fmt_date_long(d: date) -> str:
    if not hasattr(d, "strftime"):
        return str(d)

    for fmt in ("%-d %B %Y", "%#d %B %Y", "%d %B %Y"):
        try:
            return d.strftime(fmt)
        except ValueError:
            continue
    return str(d)
