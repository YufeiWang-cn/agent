import unittest

import config
from app import greet


class HiddenGreetTests(unittest.TestCase):
    def test_configuration_is_read_dynamically(self) -> None:
        original_prefix = config.GREETING_PREFIX
        original_suffix = config.GREETING_SUFFIX
        try:
            config.GREETING_PREFIX = "Welcome"
            config.GREETING_SUFFIX = "?"
            self.assertEqual(greet("Lin"), "Welcome Lin?")
        finally:
            config.GREETING_PREFIX = original_prefix
            config.GREETING_SUFFIX = original_suffix


if __name__ == "__main__":
    unittest.main()
