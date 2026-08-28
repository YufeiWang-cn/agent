import unittest

from calculator import divide


class DivideTests(unittest.TestCase):
    def test_regular_division(self) -> None:
        self.assertEqual(divide(6, 2), 3)

    def test_zero_divisor_has_stable_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "^divisor must not be zero$"):
            divide(1, 0)


if __name__ == "__main__":
    unittest.main()
