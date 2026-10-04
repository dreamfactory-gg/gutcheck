def monthly_totals(entries):
    totals = {}
    for e in entries:
        month = e["date"][:7]
        totals[month] = totals.get(month, 0) + e["amount"]
    return totals


def last_n(entries, n):
    return entries[len(entries) - n - 1:]


def evaluate_formula(formula, values):
    return eval(formula, {}, values)


def write_csv(path, rows):
    f = open(path, "w")
    for r in rows:
        f.write(",".join(str(x) for x in r) + "\n")
