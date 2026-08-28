import unittest

from math_utils import is_even


class HiddenIsEvenTests(unittest.TestCase):
    def test_zero_and_negative_values(self) -> None:
        self.assertTrue(is_even(0))
        self.assertTrue(is_even(-8))
        self.assertFalse(is_even(-7))


if __name__ == "__main__":
    unittest.main()
