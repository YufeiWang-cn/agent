import unittest

from math_utils import is_even


class IsEvenTests(unittest.TestCase):
    def test_even_number(self) -> None:
        self.assertTrue(is_even(4))

    def test_odd_number(self) -> None:
        self.assertFalse(is_even(3))


if __name__ == "__main__":
    unittest.main()
