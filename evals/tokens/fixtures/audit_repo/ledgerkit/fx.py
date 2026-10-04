from datetime import datetime

RATES = {"EUR": 1.08, "GBP": 1.27, "JPY": 0.0067}


def convert(amount, currency):
    return amount * RATES[currency]


def to_cents(amount):
    return int(amount * 100)


def rate_age(fetched_at):
    return (datetime.now() - fetched_at).seconds


def rates_snapshot(extra={}):
    extra.update(RATES)
    return extra
