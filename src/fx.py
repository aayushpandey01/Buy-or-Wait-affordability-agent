from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Tuple

import pandas as pd

CURRENCIES = ["USD", "EUR", "IDR", "INR", "ZAR"]


@dataclass
class FXTable:
    # for every rate_date (sorted), a dict of {currency: value_of_1_USD_in_that_currency}
    dates: List[date]
    usd_basis: List[Dict[str, float]]

    def _closest_index(self, d: date) -> int:
        idx = bisect.bisect_right(self.dates, d) - 1
        if idx < 0:
            idx = 0
        if idx >= len(self.dates):
            idx = len(self.dates) - 1
        return idx

    def convert(self, amount: float, from_ccy: str, to_ccy: str, on_date: date) -> float:
        if from_ccy == to_ccy:
            return amount
        idx = self._closest_index(on_date)
        basis = self.usd_basis[idx]
        usd_amount = amount / basis[from_ccy]
        return usd_amount * basis[to_ccy]


def load_fx_table(path: str) -> FXTable:
    df = pd.read_csv(path, parse_dates=["rate_date"])
    df["rate_date"] = df["rate_date"].dt.date
    grouped: Dict[date, Dict[Tuple[str, str], float]] = {}
    for _, row in df.iterrows():
        grouped.setdefault(row["rate_date"], {})[(row["from_currency"], row["to_currency"])] = row["rate"]

    dates = sorted(grouped.keys())
    usd_basis_list: List[Dict[str, float]] = []
    last_basis: Dict[str, float] = {c: None for c in CURRENCIES}
    last_basis["USD"] = 1.0

    for d in dates:
        pairs = grouped[d]
        basis = dict(last_basis)
        basis["USD"] = 1.0
        if ("USD", "EUR") in pairs:
            basis["EUR"] = pairs[("USD", "EUR")]
        elif ("EUR", "USD") in pairs:
            basis["EUR"] = 1.0 / pairs[("EUR", "USD")]
        if ("USD", "IDR") in pairs:
            basis["IDR"] = pairs[("USD", "IDR")]
        if ("USD", "INR") in pairs:
            basis["INR"] = pairs[("USD", "INR")]
        if ("EUR", "ZAR") in pairs and basis.get("EUR") is not None:
            basis["ZAR"] = basis["EUR"] * pairs[("EUR", "ZAR")]
        # fill any still-missing currency from the previous known basis (rates are
        # effectively constant across the dataset, so this is a safe fallback).
        for c in CURRENCIES:
            if basis.get(c) is None:
                basis[c] = last_basis.get(c)
        usd_basis_list.append(basis)
        last_basis = basis

    return FXTable(dates=dates, usd_basis=usd_basis_list)
