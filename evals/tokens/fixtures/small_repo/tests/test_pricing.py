import unittest

from shop import pricing


class PricingTest(unittest.TestCase):
    def test_small_order(self):
        self.assertEqual(pricing.subtotal_cents(250, 3), 750)

    def test_bulk_starts_at_threshold(self):
        self.assertEqual(pricing.subtotal_cents(100, 10), 850)

    def test_bulk_above_threshold(self):
        self.assertEqual(pricing.subtotal_cents(100, 20), 1700)

    def test_free_shipping(self):
        self.assertEqual(pricing.order_total_cents(1000, 5), 5000)

    def test_paid_shipping(self):
        self.assertEqual(pricing.order_total_cents(100, 2), 699)


if __name__ == "__main__":
    unittest.main()
