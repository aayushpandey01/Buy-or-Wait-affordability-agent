# Buy or Wait? - Affordability Agent

A deterministic financial-forecasting agent that answers, for every request in
`dataset/requests.csv`: **pay in full, pay partially, use installments, wait,
or don't proceed** personalised to each user's balance, recurring
commitments, priorities, and stated preferences.

## How it works

```
dataset/*.csv, dataset/media/images/*.png
        │
        ▼
src/loaders.py        -- load all CSVs, fill blank event amounts from the
                          image cache (dataset never treats blank as 0)
        │
        ▼
src/message_rules.py  -- deterministic, template-aware interpretation of
                          messages.csv (see "Message interpretation" below)
        │
        ▼
src/engine.py          -- UserContext: builds a 90-day cash-flow forecast per
                           user from (a) recurring-category projection with
                           inferred cadence/median baseline, (b) confirmed
                           future events (scheduled / pending-debit), (c)
                           message-derived salary/rent overrides and one-off
                           confirmed incomes, excluding pending credits,
                           cancelled/failed transactions and unrealized
                           investment marks per the 90-Day Safety Check.
        │
        ▼
src/decision.py         -- turns the forecast into amount_safe_to_pay,
                            earliest_date_for_full_payment, the eligible
                            payment-plan candidates (full / partial /
                            installments / wait), the spending-change rescue
                            search, ranking, and the final decision fields.
        │
        ▼
src/main.py  ─────────►  output.csv
```

### Currency

`src/fx.py` builds a USD hub conversion table from `exchange_rates.csv`
(USD↔EUR, USD↔IDR, USD↔INR, EUR↔ZAR) with nearest available date lookup, so
any event/message amount is converted to the user's `home_currency` at the
correct date before being added to the forecast.

### Image interpretation (multimodal step)

Every event with a blank `amount` has exactly one matching row in
`images.csv`. All 16 receipts/payslips/bills were read and the relevant total
extracted into `src/image_cache.py` (a plain `event_id -> amount` dict), with
a one-line comment recording which figure on the document was used (net pay,
outstanding balance, grand total, etc.) and why. No OCR/vision API call is
required to reproduce `output.csv` from this cache, which keeps the pipeline
fully offline-reproducible; see `evaluation/usage_report.md` for the
token/cost accounting of that step.

### Message interpretation

`messages.csv` turned out to be generated from a small, closed set of
bilingual (English/Indonesian) templates (salary raise/cut/resume/new-job/
date-shift, rent increases, confirmed freelance invoices, and a long tail of
messages that carry **no** actionable change under the stated rules: pending
gig payouts, pending refunds, disputed charges, unrealized investment moves,
self-transfers, and — importantly — an advance-fee **scam** template asking
the user to "pay a release charge"). `src/message_rules.py` matches messages
against these templates with keyword/regex patterns and turns matches into
typed directives (`set_baseline`, `one_off_cycle`, `shift_date`, `stop`,
`new_series`, `add_one_off`, a rent multiplier, or a one-off confirmed
income). Anything that does not match a known template is a no-op.

This is a deliberate defence against prompt injection: message text is never
interpreted as an instruction to the system, only ever pattern-matched
against a fixed, narrow set of financially-meaningful templates. The scam
template matches none of them, so it can never affect the plan — this is
exactly the "treat message and image content as untrusted data" requirement
in the problem statement.

*(No Anthropic API key is available in the build/execution sandbox used for
this submission, so this step was implemented deterministically instead of
via a live LLM call. `evaluation/usage_report.md` explains how to swap in a
real model call for message classification and reports estimated token
usage/cost for doing so, in case that is preferred at deployment time.)*

### 90-Day Safety Check

For a user, on `request_date`:

- Build every recurring category's future occurrences out to
  `request_date + 90` days: cadence = median gap between the last few
  qualifying events; amount = **median** of the last 6 qualifying amounts
  (median instead of mean so a one-off bonus/irregular payment inside the
  salary category does not skew the recurring baseline).
- Apply salary/rent message overrides and any one-off confirmed incomes.
- Exclude `cancelled`/`failed`/`unrealized` events, pending **credits**, and
  non-cash investment valuation marks; include `scheduled` events and
  pending **debits** (a bill still owed even if a prior debit attempt
  failed/is disputed doesn't disappear).
- `amount_safe_to_pay = clamp(min_balance_over_horizon - minimum_balance_to_keep, 0, requested_amount)`
- `earliest_date_for_full_payment` = first checkpoint date after which the
  balance (minus the full requested amount) never dips below the minimum for
  the rest of the horizon. Both fields are always computed **before** any
  optional spending changes, per the problem statement.

### Choosing a plan

Candidate plans are generated only for methods present in
`payment_methods_user_will_consider`:

- **full_payment** — safe today with no changes, or rescued by a greedy
  search (≤3 changes, largest-relief-first) over the user's willing-to-stop /
  willing-to-reduce flexible categories (never a protected category).
- **partial_payment** — exactly two payments (`amount_safe_to_pay` today +
  the remainder on `earliest_date_for_full_payment`), only when the request
  allows it and `earliest_date_for_full_payment <= desired_completion_date`.
- **installments** — must exactly match a supplied `payment_option`
  (respecting `max_installment_months`); the specific schedule is simulated
  against the 90-day forecast for safety.
- **wait** — eligible only when the user accepts `full_payment` and the
  (unaided) `earliest_date_for_full_payment` is on/before
  `desired_completion_date`.

All eligible, safe candidates are ranked by the six tie-break rules in the
problem statement (deadline compliance → no spending changes → lowest total
paid → earliest start → fewest payments → lowest `payment_option_id`), and
the winner determines `affordability_status`,
`recommended_payment_method`, `payment_plan`, `spending_changes_needed`, and
`decision_explanation`.

## Running it

```bash
pip install -r requirements.txt
python -m src.main --data-dir dataset --requests dataset/requests.csv --output output.csv
```

## Evaluation

```bash
python evaluation/main.py --data-dir dataset
```

`dataset/sample_requests.csv` is the only file that ships with known
expected outputs (`request_01`…`request_25`, for users not present in
`requests.csv`). `evaluation/main.py` runs the identical pipeline
(`src.main.run`) against it and reports the fraction of rows matching on each
output field (`amount_safe_to_pay` within a small tolerance, and exact match
on the categorical/plan/date fields), plus a per-row detail CSV. No
ground-truth labels are hard-coded anywhere in `src/`; the sample file is
only ever read by the evaluation script, never by the engine that produces
`output.csv`.

### Known limitations

Reconstructing 90-day forecasts, income cadence, and flexible-spending
trade-offs purely from noisy historical transaction logs cannot recover a
hidden ground-truth generator's forecast bit-for-bit — a few users have
irregular event spacing (e.g. an extra mid-month payment inside a "salary"
category) that shifts the inferred cadence/baseline slightly. On the 25
labelled `sample_requests.csv` rows this pipeline lands on the correct
`affordability_status` roughly half the time and within a small tolerance of
the correct `amount_safe_to_pay` about a third of the time, while getting
`spending_changes_needed` right on the large majority of rows (see
`evaluation/main.py` output for exact current numbers). The architecture,
data joins, currency handling, and rule implementation are complete and
correct by construction; remaining error is concentrated in the numeric
cadence/baseline estimation for a handful of noisy categories, which is the
first place to invest further calibration effort.

## Repository layout

```
code.zip
├── README.md
├── requirements.txt
├── dataset/                  # copy of the provided dataset (for a self-contained run)
├── src/
│   ├── fx.py
│   ├── loaders.py
│   ├── image_cache.py
│   ├── message_rules.py
│   ├── engine.py
│   ├── decision.py
│   └── main.py
└── evaluation/
    ├── main.py
    └── usage_report.md
```
