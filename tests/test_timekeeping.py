"""验证全局北京时间默认值和旧时间戳兼容排序。"""

import unittest
from datetime import timedelta

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.timekeeping import (
    CHINA_TIMEZONE_NAME,
    iso_now_china,
    now_china,
    timestamp_sort_key,
)


class TimekeepingTests(unittest.TestCase):
    def test_default_time_is_explicit_china_standard_time(self) -> None:
        value = now_china()

        self.assertEqual(value.utcoffset(), timedelta(hours=8))
        self.assertEqual(value.tzname(), CHINA_TIMEZONE_NAME)
        self.assertTrue(iso_now_china().endswith("+08:00"))

    def test_old_utc_and_new_china_timestamps_sort_by_actual_instant(self) -> None:
        earlier_china = "2026-01-01T00:30:00+08:00"
        later_utc = "2025-12-31T17:00:00+00:00"

        self.assertLess(
            timestamp_sort_key(earlier_china),
            timestamp_sort_key(later_utc),
        )
        self.assertLess(
            timestamp_sort_key("not-a-time"),
            timestamp_sort_key(earlier_china),
        )


if __name__ == "__main__":
    unittest.main()
