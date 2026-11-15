
import unittest

from backend.ledger.settle import settle_up


class TestSettleUp(unittest.TestCase):

    def test_three_people(self):
        balances = {
            1: 6666,
            2: -3333,
            3: -3333,
        }

        result = settle_up(balances)

        self.assertEqual(
            result,
            [
                (2, 1, 3333),
                (3, 1, 3333),
            ],
        )

    def test_already_settled(self):
        self.assertEqual(settle_up({1: 0, 2: 0}), [])

    def test_invalid_balances(self):
        with self.assertRaises(ValueError):
            settle_up({1: 1000, 2: -500})

    def test_two_people(self):
        self.assertEqual(
            settle_up({1: -1500, 2: 1500}),
            [(1, 2, 1500)],
        )


if __name__ == "__main__":
    unittest.main()
