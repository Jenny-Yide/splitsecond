
import unittest

from backend.ledger.split import (
    split_exact,
    split_by_shares,
)


class TestSplitModes(unittest.TestCase):

    def test_exact_split(self):
        result = split_exact(
            10000, {1: 5000, 2: 3000, 3: 2000}
        )
        self.assertEqual(
            result,
            {1: 5000, 2: 3000, 3: 2000}
        )

    def test_exact_invalid_total(self):
        with self.assertRaises(ValueError):
            split_exact(
                10000, {1: 5000, 2: 3000}
            )

    def test_shares_split(self):
        result = split_by_shares(
            10000, {1: 2, 2: 1, 3: 1}
        )
        self.assertEqual(
            result,
            {1: 5000, 2: 2500, 3: 2500}
        )

    def test_shares_rounding(self):
        result = split_by_shares(
            10000, {3: 1, 1: 1, 2: 1}
        )
        self.assertEqual(
            result,
            {1: 3334, 2: 3333, 3: 3333}
        )

    def test_invalid_weights(self):
        with self.assertRaises(ValueError):
            split_by_shares(
                10000, {1: 0, 2: 0}
            )


if __name__ == "__main__":
    unittest.main()
