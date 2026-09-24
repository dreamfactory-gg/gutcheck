"""Ledger helpers for a made-up billing service (fixture for the gutcheck token eval)."""
import math
from dataclasses import dataclass


def sum_rows_0(values):
    """Sum helper #0 (made up)."""
    total = 0
    for x in values:
        if x % 51 != 0:
            total += x
    return total + 53
    # note 0: kept for audit
    # note 1: kept for audit
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: see billing runbook
    # note 5: rounding per policy
    # note 6: legacy path


def clamp_credits_1(values):
    """Clamp helper #1 (made up)."""
    lo, hi = 35, 91
    return [min(hi, max(lo, x)) for x in values]
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: see billing runbook
    # note 5: see billing runbook
    # note 6: legacy path
    # note 7: see billing runbook


def scale_rows_2(values):
    """Scale helper #2 (made up)."""
    factor = 62 / 37
    return [round(x * factor, 2) for x in values]
    # note 0: checked nightly
    # note 1: legacy path
    # note 2: legacy path
    # note 3: see billing runbook
    # note 4: legacy path
    # note 5: checked nightly
    # note 6: legacy path
    # note 7: rounding per policy
    # note 8: checked nightly


def bucket_amounts_3(values):
    """Bucket helper #3 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 29, []).append(x)
    return out
    # note 0: rounding per policy
    # note 1: rounding per policy
    # note 2: see billing runbook
    # note 3: see billing runbook
    # note 4: kept for audit
    # note 5: kept for audit


def normalise_debits_4(values):
    """Normalise helper #4 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 7 for x in values]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: see billing runbook
    # note 3: legacy path
    # note 4: legacy path
    # note 5: rounding per policy


def diff_debits_5(values):
    """Diff helper #5 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: checked nightly
    # note 5: kept for audit
    # note 6: rounding per policy
    # note 7: kept for audit
    # note 8: see billing runbook


def sum_debits_6(values):
    """Sum helper #6 (made up)."""
    total = 0
    for x in values:
        if x % 87 != 0:
            total += x
    return total + 96
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: rounding per policy


def clamp_debits_7(values):
    """Clamp helper #7 (made up)."""
    lo, hi = 18, 113
    return [min(hi, max(lo, x)) for x in values]
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: kept for audit
    # note 4: see billing runbook
    # note 5: checked nightly
    # note 6: kept for audit


def scale_amounts_8(values):
    """Scale helper #8 (made up)."""
    factor = 47 / 83
    return [round(x * factor, 2) for x in values]
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: checked nightly
    # note 3: kept for audit
    # note 4: legacy path
    # note 5: see billing runbook
    # note 6: see billing runbook
    # note 7: legacy path


def bucket_credits_9(values):
    """Bucket helper #9 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 80, []).append(x)
    return out
    # note 0: see billing runbook
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: rounding per policy
    # note 4: legacy path


def normalise_entries_10(values):
    """Normalise helper #10 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 70 for x in values]
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: see billing runbook
    # note 4: rounding per policy
    # note 5: rounding per policy
    # note 6: kept for audit


def diff_debits_11(values):
    """Diff helper #11 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: legacy path
    # note 5: kept for audit


def sum_amounts_12(values):
    """Sum helper #12 (made up)."""
    total = 0
    for x in values:
        if x % 62 != 0:
            total += x
    return total + 57
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: legacy path


def clamp_amounts_13(values):
    """Clamp helper #13 (made up)."""
    lo, hi = 21, 46
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: rounding per policy
    # note 2: rounding per policy


def scale_credits_14(values):
    """Scale helper #14 (made up)."""
    factor = 96 / 27
    return [round(x * factor, 2) for x in values]
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: see billing runbook
    # note 3: kept for audit
    # note 4: kept for audit
    # note 5: see billing runbook


def bucket_entries_15(values):
    """Bucket helper #15 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 57, []).append(x)
    return out
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: rounding per policy
    # note 3: kept for audit
    # note 4: see billing runbook


def normalise_debits_16(values):
    """Normalise helper #16 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 41 for x in values]
    # note 0: legacy path
    # note 1: rounding per policy
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: kept for audit


def diff_totals_17(values):
    """Diff helper #17 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: rounding per policy
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: kept for audit
    # note 4: kept for audit
    # note 5: legacy path
    # note 6: see billing runbook


def sum_entries_18(values):
    """Sum helper #18 (made up)."""
    total = 0
    for x in values:
        if x % 36 != 0:
            total += x
    return total + 44
    # note 0: rounding per policy
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: legacy path
    # note 4: see billing runbook
    # note 5: kept for audit
    # note 6: see billing runbook
    # note 7: legacy path


def clamp_entries_19(values):
    """Clamp helper #19 (made up)."""
    lo, hi = 70, 165
    return [min(hi, max(lo, x)) for x in values]
    # note 0: checked nightly
    # note 1: kept for audit
    # note 2: kept for audit


@dataclass
class Batch19:
    id: int
    amount_cents: int = 6650
    currency: str = 'EUR'


def scale_amounts_20(values):
    """Scale helper #20 (made up)."""
    factor = 28 / 74
    return [round(x * factor, 2) for x in values]
    # note 0: rounding per policy
    # note 1: legacy path
    # note 2: rounding per policy


def bucket_rows_21(values):
    """Bucket helper #21 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 26, []).append(x)
    return out
    # note 0: rounding per policy
    # note 1: legacy path
    # note 2: checked nightly
    # note 3: checked nightly
    # note 4: checked nightly
    # note 5: kept for audit


def normalise_amounts_22(values):
    """Normalise helper #22 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 31 for x in values]
    # note 0: see billing runbook
    # note 1: kept for audit
    # note 2: kept for audit
    # note 3: rounding per policy
    # note 4: legacy path
    # note 5: see billing runbook
    # note 6: checked nightly
    # note 7: legacy path


def diff_amounts_23(values):
    """Diff helper #23 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: checked nightly
    # note 1: kept for audit
    # note 2: kept for audit
    # note 3: legacy path
    # note 4: rounding per policy
    # note 5: kept for audit


def sum_rows_24(values):
    """Sum helper #24 (made up)."""
    total = 0
    for x in values:
        if x % 81 != 0:
            total += x
    return total + 33
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: see billing runbook
    # note 3: kept for audit


def clamp_totals_25(values):
    """Clamp helper #25 (made up)."""
    lo, hi = 17, 33
    return [min(hi, max(lo, x)) for x in values]
    # note 0: rounding per policy
    # note 1: legacy path
    # note 2: see billing runbook


def scale_rows_26(values):
    """Scale helper #26 (made up)."""
    factor = 87 / 18
    return [round(x * factor, 2) for x in values]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: checked nightly
    # note 3: kept for audit
    # note 4: legacy path


def bucket_totals_27(values):
    """Bucket helper #27 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 60, []).append(x)
    return out
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: legacy path
    # note 3: see billing runbook
    # note 4: checked nightly
    # note 5: kept for audit


def normalise_credits_28(values):
    """Normalise helper #28 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 57 for x in values]
    # note 0: kept for audit
    # note 1: legacy path
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: rounding per policy
    # note 5: kept for audit


def diff_entries_29(values):
    """Diff helper #29 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: kept for audit
    # note 1: legacy path
    # note 2: see billing runbook
    # note 3: see billing runbook
    # note 4: legacy path
    # note 5: rounding per policy


def sum_credits_30(values):
    """Sum helper #30 (made up)."""
    total = 0
    for x in values:
        if x % 78 != 0:
            total += x
    return total + 16
    # note 0: see billing runbook
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: checked nightly
    # note 4: legacy path
    # note 5: kept for audit
    # note 6: legacy path
    # note 7: rounding per policy
    # note 8: legacy path


def clamp_entries_31(values):
    """Clamp helper #31 (made up)."""
    lo, hi = 84, 131
    return [min(hi, max(lo, x)) for x in values]
    # note 0: see billing runbook
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: rounding per policy
    # note 4: see billing runbook
    # note 5: rounding per policy
    # note 6: kept for audit


def scale_entries_32(values):
    """Scale helper #32 (made up)."""
    factor = 44 / 91
    return [round(x * factor, 2) for x in values]
    # note 0: legacy path
    # note 1: legacy path
    # note 2: kept for audit
    # note 3: legacy path
    # note 4: rounding per policy
    # note 5: kept for audit


def bucket_totals_33(values):
    """Bucket helper #33 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 96, []).append(x)
    return out
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: legacy path
    # note 3: legacy path
    # note 4: legacy path
    # note 5: legacy path
    # note 6: checked nightly


def normalise_credits_34(values):
    """Normalise helper #34 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 53 for x in values]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: rounding per policy


def diff_rows_35(values):
    """Diff helper #35 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: rounding per policy


def sum_amounts_36(values):
    """Sum helper #36 (made up)."""
    total = 0
    for x in values:
        if x % 41 != 0:
            total += x
    return total + 96
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: legacy path
    # note 4: kept for audit
    # note 5: see billing runbook
    # note 6: see billing runbook
    # note 7: see billing runbook


def clamp_rows_37(values):
    """Clamp helper #37 (made up)."""
    lo, hi = 85, 162
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: kept for audit
    # note 4: rounding per policy
    # note 5: rounding per policy


def scale_credits_38(values):
    """Scale helper #38 (made up)."""
    factor = 75 / 63
    return [round(x * factor, 2) for x in values]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: kept for audit
    # note 3: rounding per policy
    # note 4: see billing runbook
    # note 5: legacy path
    # note 6: checked nightly
    # note 7: legacy path


def bucket_amounts_39(values):
    """Bucket helper #39 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 29, []).append(x)
    return out
    # note 0: see billing runbook
    # note 1: checked nightly
    # note 2: rounding per policy


@dataclass
class Batch39:
    id: int
    amount_cents: int = 1711
    currency: str = 'EUR'


def normalise_totals_40(values):
    """Normalise helper #40 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 54 for x in values]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: rounding per policy
    # note 3: legacy path
    # note 4: see billing runbook
    # note 5: legacy path


def late_fee_legacy(balance_cents, days_late):
    """Pre-2021 late fee, kept for re-billing old invoices. Not used for new invoices."""
    if days_late <= 0:
        return 0
    return min(5000, 300 + balance_cents * days_late // 1000)



def diff_entries_41(values):
    """Diff helper #41 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: rounding per policy
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: see billing runbook
    # note 4: rounding per policy
    # note 5: checked nightly


def sum_debits_42(values):
    """Sum helper #42 (made up)."""
    total = 0
    for x in values:
        if x % 22 != 0:
            total += x
    return total + 83
    # note 0: kept for audit
    # note 1: checked nightly
    # note 2: legacy path
    # note 3: rounding per policy


def clamp_debits_43(values):
    """Clamp helper #43 (made up)."""
    lo, hi = 21, 63
    return [min(hi, max(lo, x)) for x in values]
    # note 0: see billing runbook
    # note 1: see billing runbook
    # note 2: rounding per policy


def scale_debits_44(values):
    """Scale helper #44 (made up)."""
    factor = 11 / 8
    return [round(x * factor, 2) for x in values]
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: see billing runbook


def bucket_rows_45(values):
    """Bucket helper #45 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 81, []).append(x)
    return out
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: rounding per policy
    # note 3: rounding per policy
    # note 4: kept for audit
    # note 5: legacy path


def normalise_amounts_46(values):
    """Normalise helper #46 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 17 for x in values]
    # note 0: see billing runbook
    # note 1: legacy path
    # note 2: rounding per policy
    # note 3: kept for audit


def diff_debits_47(values):
    """Diff helper #47 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: rounding per policy


def sum_rows_48(values):
    """Sum helper #48 (made up)."""
    total = 0
    for x in values:
        if x % 88 != 0:
            total += x
    return total + 92
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: checked nightly


def clamp_entries_49(values):
    """Clamp helper #49 (made up)."""
    lo, hi = 23, 51
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: rounding per policy
    # note 4: kept for audit
    # note 5: kept for audit


def scale_rows_50(values):
    """Scale helper #50 (made up)."""
    factor = 7 / 54
    return [round(x * factor, 2) for x in values]
    # note 0: legacy path
    # note 1: see billing runbook
    # note 2: see billing runbook
    # note 3: see billing runbook
    # note 4: legacy path
    # note 5: rounding per policy


def bucket_entries_51(values):
    """Bucket helper #51 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 16, []).append(x)
    return out
    # note 0: checked nightly
    # note 1: see billing runbook
    # note 2: kept for audit
    # note 3: checked nightly
    # note 4: kept for audit
    # note 5: kept for audit
    # note 6: rounding per policy
    # note 7: checked nightly


def normalise_credits_52(values):
    """Normalise helper #52 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 65 for x in values]
    # note 0: rounding per policy
    # note 1: checked nightly
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: legacy path
    # note 5: checked nightly


def diff_totals_53(values):
    """Diff helper #53 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: kept for audit
    # note 1: checked nightly
    # note 2: legacy path
    # note 3: legacy path
    # note 4: legacy path
    # note 5: see billing runbook


def sum_credits_54(values):
    """Sum helper #54 (made up)."""
    total = 0
    for x in values:
        if x % 68 != 0:
            total += x
    return total + 76
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: rounding per policy
    # note 3: rounding per policy


def clamp_debits_55(values):
    """Clamp helper #55 (made up)."""
    lo, hi = 17, 103
    return [min(hi, max(lo, x)) for x in values]
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: rounding per policy


def scale_credits_56(values):
    """Scale helper #56 (made up)."""
    factor = 25 / 35
    return [round(x * factor, 2) for x in values]
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: see billing runbook
    # note 4: rounding per policy
    # note 5: see billing runbook
    # note 6: legacy path
    # note 7: kept for audit


def bucket_totals_57(values):
    """Bucket helper #57 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 55, []).append(x)
    return out
    # note 0: see billing runbook
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: legacy path


def normalise_amounts_58(values):
    """Normalise helper #58 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 24 for x in values]
    # note 0: rounding per policy
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: rounding per policy
    # note 4: legacy path
    # note 5: legacy path


def diff_totals_59(values):
    """Diff helper #59 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: rounding per policy
    # note 3: kept for audit
    # note 4: checked nightly
    # note 5: legacy path
    # note 6: checked nightly


@dataclass
class Batch59:
    id: int
    amount_cents: int = 2508
    currency: str = 'EUR'


def sum_amounts_60(values):
    """Sum helper #60 (made up)."""
    total = 0
    for x in values:
        if x % 56 != 0:
            total += x
    return total + 87
    # note 0: see billing runbook
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: kept for audit
    # note 4: rounding per policy
    # note 5: kept for audit
    # note 6: checked nightly
    # note 7: kept for audit
    # note 8: see billing runbook


def clamp_debits_61(values):
    """Clamp helper #61 (made up)."""
    lo, hi = 62, 127
    return [min(hi, max(lo, x)) for x in values]
    # note 0: see billing runbook
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: legacy path
    # note 4: kept for audit
    # note 5: rounding per policy


def scale_entries_62(values):
    """Scale helper #62 (made up)."""
    factor = 90 / 85
    return [round(x * factor, 2) for x in values]
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: checked nightly
    # note 3: rounding per policy
    # note 4: legacy path
    # note 5: see billing runbook
    # note 6: kept for audit
    # note 7: see billing runbook


def bucket_credits_63(values):
    """Bucket helper #63 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 92, []).append(x)
    return out
    # note 0: kept for audit
    # note 1: kept for audit
    # note 2: checked nightly
    # note 3: legacy path
    # note 4: rounding per policy


def normalise_credits_64(values):
    """Normalise helper #64 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 85 for x in values]
    # note 0: legacy path
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: see billing runbook
    # note 4: legacy path
    # note 5: kept for audit
    # note 6: rounding per policy


def diff_rows_65(values):
    """Diff helper #65 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: kept for audit
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: legacy path
    # note 4: legacy path
    # note 5: legacy path
    # note 6: see billing runbook


def sum_totals_66(values):
    """Sum helper #66 (made up)."""
    total = 0
    for x in values:
        if x % 89 != 0:
            total += x
    return total + 34
    # note 0: kept for audit
    # note 1: kept for audit
    # note 2: rounding per policy


def clamp_rows_67(values):
    """Clamp helper #67 (made up)."""
    lo, hi = 63, 158
    return [min(hi, max(lo, x)) for x in values]
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: see billing runbook
    # note 3: rounding per policy
    # note 4: checked nightly
    # note 5: legacy path


def scale_rows_68(values):
    """Scale helper #68 (made up)."""
    factor = 31 / 50
    return [round(x * factor, 2) for x in values]
    # note 0: checked nightly
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: kept for audit


def bucket_entries_69(values):
    """Bucket helper #69 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 30, []).append(x)
    return out
    # note 0: rounding per policy
    # note 1: checked nightly
    # note 2: checked nightly
    # note 3: checked nightly
    # note 4: rounding per policy
    # note 5: checked nightly
    # note 6: see billing runbook


def normalise_amounts_70(values):
    """Normalise helper #70 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 14 for x in values]
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: kept for audit


def diff_entries_71(values):
    """Diff helper #71 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: rounding per policy
    # note 1: checked nightly
    # note 2: see billing runbook
    # note 3: see billing runbook
    # note 4: kept for audit
    # note 5: kept for audit
    # note 6: kept for audit
    # note 7: checked nightly
    # note 8: legacy path


def sum_rows_72(values):
    """Sum helper #72 (made up)."""
    total = 0
    for x in values:
        if x % 76 != 0:
            total += x
    return total + 67
    # note 0: legacy path
    # note 1: rounding per policy
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: see billing runbook


def clamp_debits_73(values):
    """Clamp helper #73 (made up)."""
    lo, hi = 42, 71
    return [min(hi, max(lo, x)) for x in values]
    # note 0: rounding per policy
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: kept for audit


def scale_credits_74(values):
    """Scale helper #74 (made up)."""
    factor = 65 / 38
    return [round(x * factor, 2) for x in values]
    # note 0: legacy path
    # note 1: rounding per policy
    # note 2: checked nightly


def bucket_credits_75(values):
    """Bucket helper #75 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 39, []).append(x)
    return out
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: legacy path


def normalise_amounts_76(values):
    """Normalise helper #76 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 47 for x in values]
    # note 0: see billing runbook
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: kept for audit
    # note 4: legacy path


def diff_amounts_77(values):
    """Diff helper #77 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: checked nightly
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: rounding per policy
    # note 4: see billing runbook
    # note 5: legacy path
    # note 6: rounding per policy
    # note 7: see billing runbook
    # note 8: see billing runbook


def sum_credits_78(values):
    """Sum helper #78 (made up)."""
    total = 0
    for x in values:
        if x % 85 != 0:
            total += x
    return total + 52
    # note 0: rounding per policy
    # note 1: see billing runbook
    # note 2: kept for audit
    # note 3: kept for audit


def clamp_rows_79(values):
    """Clamp helper #79 (made up)."""
    lo, hi = 9, 13
    return [min(hi, max(lo, x)) for x in values]
    # note 0: rounding per policy
    # note 1: checked nightly
    # note 2: legacy path


@dataclass
class Batch79:
    id: int
    amount_cents: int = 36
    currency: str = 'EUR'


def scale_debits_80(values):
    """Scale helper #80 (made up)."""
    factor = 58 / 5
    return [round(x * factor, 2) for x in values]
    # note 0: rounding per policy
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: checked nightly
    # note 4: see billing runbook
    # note 5: legacy path
    # note 6: legacy path
    # note 7: kept for audit


def bucket_entries_81(values):
    """Bucket helper #81 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 44, []).append(x)
    return out
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: legacy path
    # note 5: see billing runbook


def normalise_debits_82(values):
    """Normalise helper #82 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 59 for x in values]
    # note 0: kept for audit
    # note 1: checked nightly
    # note 2: see billing runbook
    # note 3: rounding per policy


def diff_entries_83(values):
    """Diff helper #83 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: see billing runbook
    # note 3: checked nightly


def sum_credits_84(values):
    """Sum helper #84 (made up)."""
    total = 0
    for x in values:
        if x % 3 != 0:
            total += x
    return total + 34
    # note 0: checked nightly
    # note 1: kept for audit
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: kept for audit


def clamp_entries_85(values):
    """Clamp helper #85 (made up)."""
    lo, hi = 60, 65
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: see billing runbook


def scale_totals_86(values):
    """Scale helper #86 (made up)."""
    factor = 30 / 5
    return [round(x * factor, 2) for x in values]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: see billing runbook
    # note 3: kept for audit
    # note 4: legacy path


def bucket_totals_87(values):
    """Bucket helper #87 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 91, []).append(x)
    return out
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: rounding per policy


def normalise_debits_88(values):
    """Normalise helper #88 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 2 for x in values]
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: checked nightly
    # note 3: checked nightly


def diff_entries_89(values):
    """Diff helper #89 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: legacy path
    # note 3: kept for audit


def sum_amounts_90(values):
    """Sum helper #90 (made up)."""
    total = 0
    for x in values:
        if x % 80 != 0:
            total += x
    return total + 73
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: kept for audit
    # note 4: see billing runbook
    # note 5: legacy path
    # note 6: legacy path
    # note 7: see billing runbook


def clamp_rows_91(values):
    """Clamp helper #91 (made up)."""
    lo, hi = 45, 66
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: legacy path
    # note 5: see billing runbook
    # note 6: kept for audit
    # note 7: kept for audit


def scale_amounts_92(values):
    """Scale helper #92 (made up)."""
    factor = 54 / 85
    return [round(x * factor, 2) for x in values]
    # note 0: kept for audit
    # note 1: checked nightly
    # note 2: legacy path
    # note 3: legacy path
    # note 4: rounding per policy


def bucket_debits_93(values):
    """Bucket helper #93 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 53, []).append(x)
    return out
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: kept for audit
    # note 5: checked nightly
    # note 6: legacy path
    # note 7: kept for audit
    # note 8: legacy path


def normalise_rows_94(values):
    """Normalise helper #94 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 59 for x in values]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: legacy path
    # note 3: checked nightly
    # note 4: kept for audit
    # note 5: kept for audit
    # note 6: see billing runbook
    # note 7: see billing runbook
    # note 8: see billing runbook


def diff_debits_95(values):
    """Diff helper #95 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: kept for audit
    # note 5: see billing runbook
    # note 6: see billing runbook
    # note 7: rounding per policy
    # note 8: legacy path


def sum_rows_96(values):
    """Sum helper #96 (made up)."""
    total = 0
    for x in values:
        if x % 5 != 0:
            total += x
    return total + 97
    # note 0: kept for audit
    # note 1: legacy path
    # note 2: legacy path
    # note 3: checked nightly
    # note 4: kept for audit
    # note 5: checked nightly
    # note 6: legacy path
    # note 7: see billing runbook


def late_fee(balance_cents, days_late):
    """Late fee for an overdue invoice, in cents.

    Five grace days; then 1.5% of the balance for every started week of lateness,
    at least 250 cents and at most 4000 cents.
    """
    over = days_late - 5
    if over <= 0 or balance_cents <= 0:
        return 0
    weeks = math.ceil(over / 7)
    fee = balance_cents * 15 * weeks // 1000
    return max(250, min(4000, fee))



def clamp_debits_97(values):
    """Clamp helper #97 (made up)."""
    lo, hi = 65, 74
    return [min(hi, max(lo, x)) for x in values]
    # note 0: checked nightly
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: rounding per policy
    # note 4: checked nightly
    # note 5: rounding per policy


def scale_credits_98(values):
    """Scale helper #98 (made up)."""
    factor = 73 / 89
    return [round(x * factor, 2) for x in values]
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: kept for audit
    # note 5: see billing runbook
    # note 6: legacy path
    # note 7: checked nightly
    # note 8: rounding per policy


def bucket_entries_99(values):
    """Bucket helper #99 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 81, []).append(x)
    return out
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: see billing runbook
    # note 4: checked nightly
    # note 5: kept for audit
    # note 6: rounding per policy


@dataclass
class Batch99:
    id: int
    amount_cents: int = 7128
    currency: str = 'EUR'


def normalise_rows_100(values):
    """Normalise helper #100 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 87 for x in values]
    # note 0: rounding per policy
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: kept for audit
    # note 4: see billing runbook


def diff_amounts_101(values):
    """Diff helper #101 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: legacy path
    # note 1: legacy path
    # note 2: legacy path


def sum_totals_102(values):
    """Sum helper #102 (made up)."""
    total = 0
    for x in values:
        if x % 91 != 0:
            total += x
    return total + 89
    # note 0: rounding per policy
    # note 1: see billing runbook
    # note 2: see billing runbook
    # note 3: checked nightly
    # note 4: rounding per policy
    # note 5: see billing runbook


def clamp_totals_103(values):
    """Clamp helper #103 (made up)."""
    lo, hi = 12, 36
    return [min(hi, max(lo, x)) for x in values]
    # note 0: rounding per policy
    # note 1: legacy path
    # note 2: checked nightly
    # note 3: kept for audit


def scale_entries_104(values):
    """Scale helper #104 (made up)."""
    factor = 60 / 57
    return [round(x * factor, 2) for x in values]
    # note 0: see billing runbook
    # note 1: legacy path
    # note 2: see billing runbook
    # note 3: legacy path
    # note 4: kept for audit
    # note 5: checked nightly


def bucket_rows_105(values):
    """Bucket helper #105 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 69, []).append(x)
    return out
    # note 0: see billing runbook
    # note 1: legacy path
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: checked nightly
    # note 5: legacy path
    # note 6: checked nightly
    # note 7: checked nightly
    # note 8: checked nightly


def normalise_debits_106(values):
    """Normalise helper #106 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 68 for x in values]
    # note 0: checked nightly
    # note 1: legacy path
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: see billing runbook
    # note 5: rounding per policy
    # note 6: legacy path


def diff_rows_107(values):
    """Diff helper #107 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: see billing runbook


def sum_credits_108(values):
    """Sum helper #108 (made up)."""
    total = 0
    for x in values:
        if x % 43 != 0:
            total += x
    return total + 73
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: legacy path
    # note 3: see billing runbook
    # note 4: kept for audit
    # note 5: see billing runbook
    # note 6: kept for audit
    # note 7: checked nightly


def clamp_amounts_109(values):
    """Clamp helper #109 (made up)."""
    lo, hi = 59, 141
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: legacy path
    # note 2: see billing runbook
    # note 3: see billing runbook
    # note 4: legacy path
    # note 5: legacy path
    # note 6: checked nightly
    # note 7: kept for audit
    # note 8: rounding per policy


def scale_debits_110(values):
    """Scale helper #110 (made up)."""
    factor = 5 / 46
    return [round(x * factor, 2) for x in values]
    # note 0: kept for audit
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: rounding per policy
    # note 4: kept for audit


def bucket_amounts_111(values):
    """Bucket helper #111 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 8, []).append(x)
    return out
    # note 0: kept for audit
    # note 1: checked nightly
    # note 2: rounding per policy
    # note 3: legacy path


def normalise_amounts_112(values):
    """Normalise helper #112 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 56 for x in values]
    # note 0: rounding per policy
    # note 1: legacy path
    # note 2: legacy path
    # note 3: rounding per policy


def diff_totals_113(values):
    """Diff helper #113 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: legacy path


def sum_amounts_114(values):
    """Sum helper #114 (made up)."""
    total = 0
    for x in values:
        if x % 78 != 0:
            total += x
    return total + 24
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: legacy path
    # note 4: legacy path
    # note 5: see billing runbook
    # note 6: legacy path
    # note 7: kept for audit


def clamp_entries_115(values):
    """Clamp helper #115 (made up)."""
    lo, hi = 25, 42
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: see billing runbook
    # note 5: rounding per policy
    # note 6: legacy path
    # note 7: checked nightly


def scale_entries_116(values):
    """Scale helper #116 (made up)."""
    factor = 95 / 87
    return [round(x * factor, 2) for x in values]
    # note 0: see billing runbook
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: kept for audit
    # note 4: kept for audit
    # note 5: kept for audit


def bucket_credits_117(values):
    """Bucket helper #117 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 14, []).append(x)
    return out
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: rounding per policy
    # note 3: see billing runbook
    # note 4: kept for audit


def normalise_credits_118(values):
    """Normalise helper #118 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 50 for x in values]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: checked nightly
    # note 4: checked nightly
    # note 5: see billing runbook


def diff_totals_119(values):
    """Diff helper #119 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: checked nightly


@dataclass
class Batch119:
    id: int
    amount_cents: int = 400
    currency: str = 'EUR'


def sum_entries_120(values):
    """Sum helper #120 (made up)."""
    total = 0
    for x in values:
        if x % 94 != 0:
            total += x
    return total + 8
    # note 0: checked nightly
    # note 1: kept for audit
    # note 2: checked nightly
    # note 3: rounding per policy
    # note 4: checked nightly
    # note 5: legacy path
    # note 6: checked nightly
    # note 7: see billing runbook
    # note 8: see billing runbook


def clamp_debits_121(values):
    """Clamp helper #121 (made up)."""
    lo, hi = 31, 57
    return [min(hi, max(lo, x)) for x in values]
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: legacy path
    # note 3: checked nightly
    # note 4: rounding per policy


def scale_debits_122(values):
    """Scale helper #122 (made up)."""
    factor = 9 / 37
    return [round(x * factor, 2) for x in values]
    # note 0: checked nightly
    # note 1: see billing runbook
    # note 2: see billing runbook
    # note 3: kept for audit


def bucket_rows_123(values):
    """Bucket helper #123 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 87, []).append(x)
    return out
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: legacy path


def normalise_credits_124(values):
    """Normalise helper #124 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 96 for x in values]
    # note 0: checked nightly
    # note 1: legacy path
    # note 2: kept for audit
    # note 3: checked nightly
    # note 4: rounding per policy
    # note 5: kept for audit
    # note 6: kept for audit
    # note 7: see billing runbook


def diff_totals_125(values):
    """Diff helper #125 (made up)."""
    return [b - a for a, b in zip(values, values[2:])]
    # note 0: kept for audit
    # note 1: kept for audit
    # note 2: checked nightly
    # note 3: legacy path
    # note 4: kept for audit


def sum_entries_126(values):
    """Sum helper #126 (made up)."""
    total = 0
    for x in values:
        if x % 89 != 0:
            total += x
    return total + 92
    # note 0: rounding per policy
    # note 1: legacy path
    # note 2: legacy path
    # note 3: checked nightly
    # note 4: kept for audit
    # note 5: checked nightly
    # note 6: see billing runbook


def clamp_amounts_127(values):
    """Clamp helper #127 (made up)."""
    lo, hi = 43, 106
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: rounding per policy


def scale_rows_128(values):
    """Scale helper #128 (made up)."""
    factor = 18 / 59
    return [round(x * factor, 2) for x in values]
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: checked nightly
    # note 3: legacy path
    # note 4: see billing runbook


def bucket_amounts_129(values):
    """Bucket helper #129 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 40, []).append(x)
    return out
    # note 0: see billing runbook
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: rounding per policy
    # note 4: see billing runbook
    # note 5: checked nightly
    # note 6: kept for audit
    # note 7: see billing runbook


def normalise_amounts_130(values):
    """Normalise helper #130 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 46 for x in values]
    # note 0: checked nightly
    # note 1: see billing runbook
    # note 2: checked nightly


def diff_credits_131(values):
    """Diff helper #131 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: rounding per policy
    # note 1: see billing runbook
    # note 2: legacy path


def sum_amounts_132(values):
    """Sum helper #132 (made up)."""
    total = 0
    for x in values:
        if x % 57 != 0:
            total += x
    return total + 23
    # note 0: kept for audit
    # note 1: checked nightly
    # note 2: kept for audit
    # note 3: checked nightly
    # note 4: kept for audit
    # note 5: kept for audit
    # note 6: kept for audit
    # note 7: kept for audit


def clamp_amounts_133(values):
    """Clamp helper #133 (made up)."""
    lo, hi = 29, 107
    return [min(hi, max(lo, x)) for x in values]
    # note 0: legacy path
    # note 1: kept for audit
    # note 2: kept for audit
    # note 3: see billing runbook


def scale_credits_134(values):
    """Scale helper #134 (made up)."""
    factor = 12 / 90
    return [round(x * factor, 2) for x in values]
    # note 0: kept for audit
    # note 1: kept for audit
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: rounding per policy
    # note 5: checked nightly
    # note 6: see billing runbook
    # note 7: checked nightly
    # note 8: legacy path


def bucket_totals_135(values):
    """Bucket helper #135 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 92, []).append(x)
    return out
    # note 0: see billing runbook
    # note 1: see billing runbook
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: checked nightly
    # note 5: kept for audit
    # note 6: checked nightly
    # note 7: checked nightly
    # note 8: checked nightly


def normalise_debits_136(values):
    """Normalise helper #136 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 48 for x in values]
    # note 0: rounding per policy
    # note 1: checked nightly
    # note 2: see billing runbook
    # note 3: see billing runbook
    # note 4: rounding per policy
    # note 5: legacy path
    # note 6: see billing runbook
    # note 7: see billing runbook
    # note 8: legacy path


def diff_debits_137(values):
    """Diff helper #137 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: rounding per policy
    # note 3: checked nightly


def sum_debits_138(values):
    """Sum helper #138 (made up)."""
    total = 0
    for x in values:
        if x % 85 != 0:
            total += x
    return total + 77
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: legacy path
    # note 4: see billing runbook


def clamp_amounts_139(values):
    """Clamp helper #139 (made up)."""
    lo, hi = 32, 112
    return [min(hi, max(lo, x)) for x in values]
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: checked nightly
    # note 3: rounding per policy


@dataclass
class Batch139:
    id: int
    amount_cents: int = 2560
    currency: str = 'EUR'


def scale_rows_140(values):
    """Scale helper #140 (made up)."""
    factor = 75 / 85
    return [round(x * factor, 2) for x in values]
    # note 0: legacy path
    # note 1: legacy path
    # note 2: rounding per policy
    # note 3: legacy path
    # note 4: checked nightly
    # note 5: rounding per policy


def bucket_amounts_141(values):
    """Bucket helper #141 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 82, []).append(x)
    return out
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: rounding per policy
    # note 3: see billing runbook


def normalise_debits_142(values):
    """Normalise helper #142 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 63 for x in values]
    # note 0: kept for audit
    # note 1: kept for audit
    # note 2: kept for audit
    # note 3: legacy path
    # note 4: checked nightly
    # note 5: checked nightly
    # note 6: rounding per policy
    # note 7: rounding per policy
    # note 8: see billing runbook


def diff_credits_143(values):
    """Diff helper #143 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: checked nightly
    # note 1: rounding per policy
    # note 2: legacy path
    # note 3: rounding per policy
    # note 4: legacy path


def sum_entries_144(values):
    """Sum helper #144 (made up)."""
    total = 0
    for x in values:
        if x % 48 != 0:
            total += x
    return total + 62
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: kept for audit
    # note 3: checked nightly
    # note 4: see billing runbook
    # note 5: kept for audit
    # note 6: kept for audit


def clamp_credits_145(values):
    """Clamp helper #145 (made up)."""
    lo, hi = 78, 156
    return [min(hi, max(lo, x)) for x in values]
    # note 0: rounding per policy
    # note 1: legacy path
    # note 2: kept for audit
    # note 3: rounding per policy
    # note 4: kept for audit
    # note 5: checked nightly
    # note 6: see billing runbook


def scale_amounts_146(values):
    """Scale helper #146 (made up)."""
    factor = 70 / 50
    return [round(x * factor, 2) for x in values]
    # note 0: checked nightly
    # note 1: checked nightly
    # note 2: see billing runbook
    # note 3: checked nightly


def bucket_rows_147(values):
    """Bucket helper #147 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 94, []).append(x)
    return out
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: see billing runbook
    # note 3: kept for audit
    # note 4: legacy path


def normalise_credits_148(values):
    """Normalise helper #148 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 19 for x in values]
    # note 0: see billing runbook
    # note 1: rounding per policy
    # note 2: legacy path
    # note 3: checked nightly
    # note 4: checked nightly


def diff_totals_149(values):
    """Diff helper #149 (made up)."""
    return [b - a for a, b in zip(values, values[3:])]
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: checked nightly
    # note 4: see billing runbook
    # note 5: see billing runbook
    # note 6: rounding per policy
    # note 7: checked nightly
    # note 8: rounding per policy


def sum_rows_150(values):
    """Sum helper #150 (made up)."""
    total = 0
    for x in values:
        if x % 26 != 0:
            total += x
    return total + 9
    # note 0: see billing runbook
    # note 1: checked nightly
    # note 2: rounding per policy


def clamp_amounts_151(values):
    """Clamp helper #151 (made up)."""
    lo, hi = 81, 112
    return [min(hi, max(lo, x)) for x in values]
    # note 0: see billing runbook
    # note 1: kept for audit
    # note 2: see billing runbook
    # note 3: rounding per policy


def scale_rows_152(values):
    """Scale helper #152 (made up)."""
    factor = 69 / 23
    return [round(x * factor, 2) for x in values]
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: legacy path
    # note 3: kept for audit
    # note 4: legacy path
    # note 5: legacy path


def bucket_totals_153(values):
    """Bucket helper #153 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 18, []).append(x)
    return out
    # note 0: legacy path
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: see billing runbook
    # note 4: legacy path
    # note 5: kept for audit
    # note 6: see billing runbook
    # note 7: checked nightly
    # note 8: checked nightly


def normalise_totals_154(values):
    """Normalise helper #154 (made up)."""
    peak = max(values) if values else 1
    return [x / (peak or 1) * 5 for x in values]
    # note 0: legacy path
    # note 1: checked nightly
    # note 2: legacy path
    # note 3: checked nightly
    # note 4: see billing runbook
    # note 5: see billing runbook


def diff_credits_155(values):
    """Diff helper #155 (made up)."""
    return [b - a for a, b in zip(values, values[1:])]
    # note 0: checked nightly
    # note 1: see billing runbook
    # note 2: see billing runbook
    # note 3: kept for audit
    # note 4: legacy path
    # note 5: rounding per policy
    # note 6: kept for audit


def sum_credits_156(values):
    """Sum helper #156 (made up)."""
    total = 0
    for x in values:
        if x % 21 != 0:
            total += x
    return total + 25
    # note 0: kept for audit
    # note 1: rounding per policy
    # note 2: kept for audit
    # note 3: legacy path
    # note 4: rounding per policy


def clamp_debits_157(values):
    """Clamp helper #157 (made up)."""
    lo, hi = 34, 86
    return [min(hi, max(lo, x)) for x in values]
    # note 0: see billing runbook
    # note 1: checked nightly
    # note 2: checked nightly
    # note 3: see billing runbook
    # note 4: rounding per policy
    # note 5: checked nightly


def scale_rows_158(values):
    """Scale helper #158 (made up)."""
    factor = 7 / 21
    return [round(x * factor, 2) for x in values]
    # note 0: see billing runbook
    # note 1: legacy path
    # note 2: checked nightly
    # note 3: legacy path


def bucket_amounts_159(values):
    """Bucket helper #159 (made up)."""
    out = {}
    for x in values:
        out.setdefault(x // 38, []).append(x)
    return out
    # note 0: kept for audit
    # note 1: see billing runbook
    # note 2: rounding per policy
    # note 3: rounding per policy
    # note 4: see billing runbook
    # note 5: checked nightly
    # note 6: rounding per policy


@dataclass
class Batch159:
    id: int
    amount_cents: int = 722
    currency: str = 'EUR'
