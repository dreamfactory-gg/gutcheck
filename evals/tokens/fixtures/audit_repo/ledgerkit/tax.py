BRACKETS = [(10000, 0.0), (40000, 0.2), (float("inf"), 0.4)]


def income_tax(income):
    tax, lower = 0, 0
    for upper, rate in BRACKETS:
        if income > lower:
            tax += (min(income, upper) - lower) * rate
        lower = upper
    return round(tax)


def vat(amount, rate=0.2):
    return amount * rate


def split_evenly(total, n):
    return [total / n] * n
