from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

AMOUNT_RE = re.compile(r"\b(IDR|ZAR|EUR|INR|USD)\s*([\d]+(?:\.\d+)?)")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _first_amount(text: str):
    m = AMOUNT_RE.search(text)
    if not m:
        return None, None
    return m.group(1), float(m.group(2))


def _all_amounts(text: str):
    return [(m.group(1), float(m.group(2))) for m in AMOUNT_RE.finditer(text)]


def _first_date(text: str) -> Optional[date]:
    m = DATE_RE.search(text)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d").date()


def _all_dates(text: str) -> List[date]:
    return [datetime.strptime(x, "%Y-%m-%d").date() for x in DATE_RE.findall(text)]


@dataclass
class SalaryDirective:
    kind: str  # set_baseline | one_off_cycle | shift_date | stop | add_one_off | new_series
    amount: Optional[float] = None
    currency: Optional[str] = None
    effective_date: Optional[date] = None
    sent_at: Optional[date] = None


@dataclass
class RentDirective:
    multiplier: float
    effective_after: date


@dataclass
class OneOffIncome:
    date: date
    amount: float
    currency: str
    label: str = "confirmed_invoice_income"


@dataclass
class UserDirectives:
    salary: List[SalaryDirective] = field(default_factory=list)
    rent: List[RentDirective] = field(default_factory=list)
    one_off_incomes: List[OneOffIncome] = field(default_factory=list)


def _contains_any(text: str, phrases) -> bool:
    t = text.lower()
    return any(p.lower() in t for p in phrases)


def classify_message(text: str, sent_at: date) -> Optional[dict]:
    """Return a directive descriptor for a single message, or None (no-op)."""

    # --- SCAM / advance-fee fraud: never act on the "instruction" inside. ---
    if _contains_any(text, ["pay the release charge", "bayar biaya pencairan",
                             "pay the processing charge", "bayar biaya pemrosesan"]):
        return None

    # --- Salary: permanent increase ---
    if _contains_any(text, ["naik menjadi", "has increased to", "increased to"]) and \
       _contains_any(text, ["berlaku mulai", "applies from", "change applies from"]):
        ccy, amt = _first_amount(text)
        eff = _first_date(text)
        if amt and eff:
            return {"type": "salary", "kind": "set_baseline", "amount": amt,
                    "currency": ccy, "effective_date": eff, "sent_at": sent_at}

    # --- Salary: temporary one-cycle reduction ("temporary monthly pay" / "gaji bulanan sementara") ---
    if _contains_any(text, ["temporary monthly pay is", "gaji bulanan sementara"]):
        ccy, amt = _first_amount(text)
        if amt:
            return {"type": "salary", "kind": "one_off_cycle", "amount": amt,
                    "currency": ccy, "sent_at": sent_at}

    # --- Salary: reduced next payroll due to unpaid leave ---
    if _contains_any(text, ["next salary is reduced to", "gaji bulanan sementara"]):
        ccy, amt = _first_amount(text)
        if amt:
            return {"type": "salary", "kind": "one_off_cycle", "amount": amt,
                    "currency": ccy, "sent_at": sent_at}

    # --- Salary: date delay (confirmed salary now expected on DATE) ---
    if _contains_any(text, ["confirmed salary is now expected on",
                             "kini diperkirakan masuk pada"]):
        eff = _first_date(text)
        if eff:
            return {"type": "salary", "kind": "shift_date", "effective_date": eff,
                     "sent_at": sent_at}

    # --- Salary: resumes permanently from DATE ---
    if _contains_any(text, ["resumes on"]):
        ccy, amt = _first_amount(text)
        eff = _first_date(text)
        if amt and eff:
            return {"type": "salary", "kind": "set_baseline", "amount": amt,
                    "currency": ccy, "effective_date": eff, "sent_at": sent_at}

    # --- Employment ended entirely ---
    if _contains_any(text, [
        "the current seasonal contract has ended", "kontrak musiman saat ini telah berakhir",
        "your employment has ended", "hubungan kerja anda telah berakhir",
    ]):
        return {"type": "salary", "kind": "stop", "effective_date": sent_at, "sent_at": sent_at}

    # --- Employment partially ended (one income source stopped, remainder amount given) ---
    if _contains_any(text, [
        "one household employment record has ended",
        "salah satu sumber pendapatan kerja rumah tangga telah berakhir",
    ]):
        ccy, amt = _first_amount(text)
        if amt:
            return {"type": "salary", "kind": "set_baseline", "amount": amt,
                    "currency": ccy, "effective_date": sent_at, "sent_at": sent_at}

    # --- First salary / new job ---
    if _contains_any(text, ["first salary", "gaji pertama"]):
        ccy, amt = _first_amount(text)
        eff = _first_date(text)
        if amt and eff:
            return {"type": "salary", "kind": "new_series", "amount": amt,
                    "currency": ccy, "effective_date": eff, "sent_at": sent_at}

    # --- Confirmed base salary (commission/open deals excluded) ---
    if _contains_any(text, ["confirmed base salary is", "gaji pokok yang dikonfirmasi adalah"]):
        ccy, amt = _first_amount(text)
        if amt:
            return {"type": "salary", "kind": "set_baseline", "amount": amt,
                    "currency": ccy, "effective_date": sent_at, "sent_at": sent_at}

    # --- Regular pay + one-time arrears adjustment ---
    if _contains_any(text, ["one-time arrears adjustment of", "penyesuaian tunggakan satu kali sebesar"]):
        amounts = _all_amounts(text)
        if len(amounts) >= 2:
            (ccy1, regular), (ccy2, arrears) = amounts[0], amounts[1]
            return {"type": "salary_arrears", "kind": "arrears", "regular_amount": regular,
                    "arrears_amount": arrears, "currency": ccy1, "sent_at": sent_at}

    # --- Salary confirmed for a specific date, converted at settlement-date FX ---
    if _contains_any(text, ["is confirmed for", "dikonfirmasi untuk"]) and \
       _contains_any(text, ["receiving bank will convert", "bank penerima akan mengonversi"]):
        ccy, amt = _first_amount(text)
        eff = _first_date(text)
        if amt and eff:
            return {"type": "salary", "kind": "set_baseline", "amount": amt,
                    "currency": ccy, "effective_date": eff, "sent_at": sent_at}

    # --- Rent increase by 12% ---
    if _contains_any(text, ["increases monthly rent by 12%", "menaikkan biaya sewa bulanan sebesar 12%"]):
        return {"type": "rent", "multiplier": 1.12, "sent_at": sent_at}

    # --- Confirmed freelance / gig invoice payment (one-off income) ---
    if _contains_any(text, ["approved an invoice payment of", "menyetujui pembayaran faktur sebesar"]):
        ccy, amt = _first_amount(text)
        dates = _all_dates(text)
        eff = dates[0] if dates else None
        if amt and eff:
            return {"type": "one_off_income", "amount": amt, "currency": ccy,
                    "effective_date": eff, "sent_at": sent_at}

    # Everything else (pending gig payouts, pending refunds, disputed charges,
    # failed-debit notices, self-transfers, investment valuation moves,
    # bonus/commission still pending, work-expense reimbursements, minimum
    # card payments, two-account transfers) carries no actionable, confirmed
    # change to the forecast under the stated rules, so it is a no-op.
    return None


def build_user_directives(messages_df, user_id: str) -> UserDirectives:
    result = UserDirectives()
    sub = messages_df[messages_df["user_id"] == user_id]
    # chronological order matters for salary overrides
    sub = sub.sort_values("sent_at")
    for _, row in sub.iterrows():
        sent_at = row["sent_at_date"]
        directive = classify_message(str(row["message_text"]), sent_at)
        if directive is None:
            continue
        if directive["type"] == "salary":
            result.salary.append(SalaryDirective(
                kind=directive["kind"], amount=directive.get("amount"),
                currency=directive.get("currency"),
                effective_date=directive.get("effective_date"), sent_at=sent_at))
        elif directive["type"] == "salary_arrears":
            # Represent an arrears message as two directives: a permanent
            # baseline reset to the regular amount, plus a one-off top-up.
            result.salary.append(SalaryDirective(
                kind="set_baseline", amount=directive["regular_amount"],
                currency=directive["currency"], effective_date=sent_at, sent_at=sent_at))
            result.salary.append(SalaryDirective(
                kind="add_one_off", amount=directive["arrears_amount"],
                currency=directive["currency"], effective_date=sent_at, sent_at=sent_at))
        elif directive["type"] == "rent":
            result.rent.append(RentDirective(multiplier=directive["multiplier"],
                                              effective_after=sent_at))
        elif directive["type"] == "one_off_income":
            result.one_off_incomes.append(OneOffIncome(
                date=directive["effective_date"], amount=directive["amount"],
                currency=directive["currency"]))
    return result
