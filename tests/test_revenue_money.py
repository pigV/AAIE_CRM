import unittest

from revenue.money import Money, allocate_cents, apply_basis_points, gst_from_inclusive


class RevenueMoneyTests(unittest.TestCase):
    def test_basis_points_round_half_up_using_integer_arithmetic(self):
        self.assertEqual(3300, apply_basis_points(11000, 3000))
        self.assertEqual(1, apply_basis_points(1, 5000))
        self.assertEqual(0, apply_basis_points(1, 4999))

    def test_gst_inclusive_split_is_exact(self):
        self.assertEqual((10000, 1000), gst_from_inclusive(11000, 1000))
        self.assertEqual((1, 0), gst_from_inclusive(1, 1000))

    def test_allocation_preserves_total_and_assigns_residual_deterministically(self):
        self.assertEqual([34, 33, 33], allocate_cents(100, [1, 1, 1]))
        self.assertEqual([67, 33], allocate_cents(100, [2, 1]))
        self.assertEqual(100, sum(allocate_cents(100, [7, 11, 13])))

    def test_money_rejects_float_and_currency_mismatch(self):
        with self.assertRaises(TypeError):
            Money(10.5)
        with self.assertRaises(ValueError):
            Money(100, "AUD") + Money(100, "USD")


if __name__ == "__main__":
    unittest.main()
