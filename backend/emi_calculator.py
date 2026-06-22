"""
emi_calculator.py — Pure EMI business logic
No I/O. All calculations use reducing balance method.

Key concept — Pre-EMI / Broken Period Interest:
  When sanction_date != start_date (first EMI date), the bank charges
  simple daily interest for the gap days before the regular EMI cycle starts.

  Formula:
    days = (start_date - sanction_date).days
    pre_emi_interest = principal * (annual_rate / 100) * days / 365

  This appears as EMI #0 in the schedule — paid once, then regular EMIs begin.
"""

import math
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date
from dateutil.relativedelta import relativedelta


def _r2(v: float) -> float:
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── Core EMI formula ──────────────────────────────────────────────────

def calculate_emi(principal: float, annual_rate: float, months: int) -> float:
    """EMI = P * r * (1+r)^n / ((1+r)^n - 1)"""
    if annual_rate == 0:
        return _r2(principal / months)
    r = annual_rate / 12 / 100
    emi = principal * r * math.pow(1 + r, months) / (math.pow(1 + r, months) - 1)
    return _r2(emi)


# ── Pre-EMI (Broken Period) Interest ─────────────────────────────────

def calculate_pre_emi(principal: float, annual_rate: float,
                      sanction_date: str, start_date: str) -> tuple[float, int]:
    """
    Calculate interest for the stub period between sanction and first EMI.

    Returns (pre_emi_interest, days).
    Returns (0.0, 0) when both dates are the same.

    Uses simple interest on a 365-day year basis (actual/365),
    which is the standard Indian bank convention.
    """
    sd = datetime.strptime(sanction_date, "%Y-%m-%d").date()
    ed = datetime.strptime(start_date,    "%Y-%m-%d").date()

    if ed <= sd:
        return 0.0, 0

    days = (ed - sd).days
    # Simple interest: P * R * D / 365
    interest = _r2(principal * (annual_rate / 100) * days / 365)
    return interest, days


# ── Full amortization schedule ────────────────────────────────────────

def generate_schedule(
    loan_id: str,
    principal: float,
    annual_rate: float,
    tenure_months: int,
    start_date: str,
    sanction_date: str | None = None,
    emi_amount: float | None = None,
    start_emi_number: int = 1,
    outstanding_balance: float | None = None,
) -> list[dict]:
    """
    Generate full amortization schedule.

    If sanction_date is provided (and differs from start_date),
    EMI #0 = pre-EMI stub interest row is prepended.
    Regular EMIs are numbered 1..n.
    """
    if emi_amount is None:
        emi_amount = calculate_emi(principal, annual_rate, tenure_months)

    balance = outstanding_balance if outstanding_balance is not None else principal
    r = annual_rate / 12 / 100
    due_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    rows = []

    # ── EMI #0: Pre-EMI stub interest ──
    if sanction_date and sanction_date != start_date:
        pre_int, days = calculate_pre_emi(principal, annual_rate, sanction_date, start_date)
        if pre_int > 0:
            rows.append({
                "loan_id": loan_id,
                "emi_number": 0,
                "due_date": start_date,       # payable on first EMI date
                "emi_amount": pre_int,
                "interest_component": pre_int,
                "principal_component": 0.0,
                "outstanding_balance": balance,
                "status": "Unpaid",
                "paid_date": "",
                "is_pre_emi": 1,
            })
            due_date = due_date + relativedelta(months=1)  # ← advance so EMI #1 starts next month

    # ── Regular EMIs #1..n ──
    for i in range(tenure_months):
        emi_num = start_emi_number + i
        interest = _r2(balance * r)
        principal_comp = _r2(emi_amount - interest)

        if i == tenure_months - 1:          # last EMI: clear any residual
            principal_comp = _r2(balance)
            emi_amount = _r2(interest + principal_comp)

        if principal_comp > balance:        # guard against float overflow
            principal_comp = _r2(balance)
            emi_amount = _r2(interest + principal_comp)

        balance = _r2(max(0.0, balance - principal_comp))

        rows.append({
            "loan_id": loan_id,
            "emi_number": emi_num,
            "due_date": due_date.strftime("%Y-%m-%d"),
            "emi_amount": emi_amount,
            "interest_component": interest,
            "principal_component": principal_comp,
            "outstanding_balance": balance,
            "status": "Unpaid",
            "paid_date": "",
            "is_pre_emi": 0,
        })

        due_date = due_date + relativedelta(months=1)
        if balance == 0:
            break

    return rows


# ── Post-event recalculations ─────────────────────────────────────────

def recalculate_after_part_payment(
    loan_id: str,
    outstanding_balance: float,
    annual_rate: float,
    remaining_months: int,
    next_due_date: str,
    reduce_emi: bool = False,
    current_emi: float | None = None,
    start_emi_number: int = 1,
) -> tuple[float, list[dict]]:
    """After part payment: reduce tenure (default) or reduce EMI."""
    r = annual_rate / 12 / 100

    if not reduce_emi and current_emi and r > 0:
        if current_emi > outstanding_balance * r:
            n = math.ceil(
                -math.log(1 - (outstanding_balance * r) / current_emi) / math.log(1 + r)
            )
            remaining_months = max(1, n)
            # use existing EMI amount (tenure shortens)
            schedule = generate_schedule(
                loan_id=loan_id, principal=outstanding_balance,
                annual_rate=annual_rate, tenure_months=remaining_months,
                start_date=next_due_date, outstanding_balance=outstanding_balance,
                emi_amount=current_emi, start_emi_number=start_emi_number,
            )
            return current_emi, schedule

    schedule = generate_schedule(
        loan_id=loan_id, principal=outstanding_balance,
        annual_rate=annual_rate, tenure_months=remaining_months,
        start_date=next_due_date, outstanding_balance=outstanding_balance,
        start_emi_number=start_emi_number,
    )
    new_emi = schedule[0]["emi_amount"] if schedule else 0.0
    return new_emi, schedule


def recalculate_after_rate_change(
    loan_id: str,
    outstanding_balance: float,
    new_annual_rate: float,
    remaining_months: int,
    next_due_date: str,
    start_emi_number: int = 1,
) -> tuple[float, list[dict]]:
    """Recalculate from next EMI with a new rate."""
    schedule = generate_schedule(
        loan_id=loan_id, principal=outstanding_balance,
        annual_rate=new_annual_rate, tenure_months=remaining_months,
        start_date=next_due_date, outstanding_balance=outstanding_balance,
        start_emi_number=start_emi_number,
    )
    new_emi = schedule[0]["emi_amount"] if schedule else 0.0
    return new_emi, schedule


# ── Part-Payment Interest Savings ─────────────────────────────────────

def _amortize_interest_only(balance: float, annual_rate: float, emi_amount: float,
                            months: int) -> list[float]:
    """
    Simulate a simple amortization and return the interest component
    paid each month (length up to `months`, stops early if balance hits 0).
    Used only for comparison purposes — does not touch the DB or real schedule.
    """
    r = annual_rate / 12 / 100
    out = []
    bal = balance
    for i in range(months):
        if bal <= 0:
            break
        interest = _r2(bal * r)
        principal_comp = _r2(emi_amount - interest)
        if i == months - 1 or principal_comp > bal:
            principal_comp = _r2(bal)
        bal = _r2(max(0.0, bal - principal_comp))
        out.append(interest)
        if bal == 0:
            break
    return out


def calculate_part_payment_savings(
    loan_id: str,
    annual_rate: float,
    balance_before: float,
    balance_after: float,
    emi_before: float,
    emi_after: float,
    remaining_months_before: int,
    payment_date: str,
) -> dict:
    """
    Compare two parallel amortizations — "as if this part payment never
    happened" (balance_before/emi_before/remaining_months_before) vs.
    "what actually happened after the part payment" (balance_after/emi_after)
    — and return interest saved per month going forward, plus a total.

    This is a what-if comparison for display purposes; it does not alter
    any stored schedule.
    """
    without_pp = _amortize_interest_only(
        balance_before, annual_rate, emi_before, remaining_months_before
    )
    # Give the "after" simulation generous headroom (it should finish sooner
    # or in the same time since principal dropped) but never longer than "before".
    with_pp = _amortize_interest_only(
        balance_after, annual_rate, emi_after, remaining_months_before
    )

    months = max(len(without_pp), len(with_pp))
    due = datetime.strptime(payment_date, "%Y-%m-%d").date()
    # Part payment effect starts applying from the next due month
    due = due + relativedelta(months=1)

    monthly = []
    total_saved = 0.0
    for i in range(months):
        interest_without = without_pp[i] if i < len(without_pp) else 0.0
        interest_with = with_pp[i] if i < len(with_pp) else 0.0
        saved = _r2(interest_without - interest_with)
        total_saved += saved
        monthly.append({
            "month": (due + relativedelta(months=i)).strftime("%Y-%m"),
            "interest_without_part_payment": interest_without,
            "interest_with_part_payment": interest_with,
            "interest_saved": saved,
        })

    return {
        "loan_id": loan_id,
        "monthly_savings": monthly,
        "total_interest_saved": _r2(total_saved),
        "months_compared": months,
    }

def compute_summary(loan: dict, schedule: list[dict], payments: list[dict]) -> dict:
    total_paid = sum(float(p["amount_paid"]) for p in payments)
    total_interest = sum(
        float(r["interest_component"]) for r in schedule if r["status"] == "Paid"
    )
    # Exclude pre-EMI row from outstanding/remaining counts
    regular = [r for r in schedule if not r.get("is_pre_emi")]
    unpaid  = [r for r in regular   if r["status"] in ("Unpaid", "Partial")]

    if unpaid:
        outstanding = float(unpaid[0]["outstanding_balance"]) + float(unpaid[0]["principal_component"])
    else:
        outstanding = 0.0

    next_emi_date = unpaid[0]["due_date"] if unpaid else "Loan Closed"
    return {
        "loan_id": loan["loan_id"],
        "total_paid": round(total_paid, 2),
        "total_interest_paid": round(total_interest, 2),
        "outstanding_principal": round(outstanding, 2),
        "remaining_emis": len(unpaid),
        "next_emi_date": next_emi_date,
        "pre_emi_interest": loan.get("pre_emi_interest", 0),
        "pre_emi_days": loan.get("pre_emi_days", 0),
        "sanction_date": loan.get("sanction_date", ""),
    }