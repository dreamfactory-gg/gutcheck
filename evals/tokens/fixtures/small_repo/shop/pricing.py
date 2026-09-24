"""Order pricing for a made-up shop."""

BULK_THRESHOLD = 10          # units; orders of this many or more get the bulk discount
BULK_DISCOUNT = 0.15
SHIPPING_FLAT_CENTS = 499
FREE_SHIPPING_FROM_CENTS = 5000


def subtotal_cents(unit_cents, qty):
    if qty < 0:
        raise ValueError("qty must be >= 0")
    total = unit_cents * qty
    if qty > BULK_THRESHOLD:
        total = round(total * (1 - BULK_DISCOUNT))
    return total


def shipping_cents(subtotal):
    return 0 if subtotal >= FREE_SHIPPING_FROM_CENTS else SHIPPING_FLAT_CENTS


def order_total_cents(unit_cents, qty):
    sub = subtotal_cents(unit_cents, qty)
    return sub + shipping_cents(sub)
