
import unittest
from backend.ledger.split import split_equally


class TestSplitEqually(unittest.TestCase):

    def test_equal_split(self):
        result = split_equally(10000, [3, 1, 2])
        self.assertEqual(
            result,
            {1: 3334, 2: 3333, 3: 3333}
        )

    def test_total_is_preserved(self):
        result = split_equally(10001, [1, 2, 3])
        self.assertEqual(sum(result.values()), 10001)

    def test_invalid_amount(self):
        with self.assertRaises(ValueError):
            split_equally(-100, [1, 2])

    def test_duplicate_users(self):
        with self.assertRaises(ValueError):
            split_equally(1000, [1, 1])


if __name__ == "__main__":
    unittest.main()
