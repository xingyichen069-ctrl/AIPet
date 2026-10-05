"""Calendar-day labels across midnight, timezone offsets and an open panel."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import render as R


class MemoryCalendarDates(unittest.TestCase):
    def test_yesterday_is_not_today_even_when_less_than_24_hours_ago(self):
        self.assertEqual(R.calendar_age(datetime.fromisoformat("2026-10-04T23:34:42+08:00"),
                                        datetime.fromisoformat("2026-10-05T16:00:00+08:00")), "昨天")

    def test_midnight_and_year_boundary(self):
        self.assertEqual(R.calendar_age(datetime.fromisoformat("2026-12-31T23:59:59+08:00"),
                                        datetime.fromisoformat("2027-01-01T00:00:00+08:00")), "昨天")

    def test_timestamp_is_converted_to_the_display_timezone_before_comparing(self):
        ref = datetime.fromisoformat("2026-10-05T01:00:00+08:00")
        self.assertEqual(R.calendar_age(datetime.fromisoformat("2026-10-04T17:00:00+00:00"), ref), "今天")
        self.assertEqual(R.calendar_age(datetime.fromisoformat("2026-10-04T15:59:59+00:00"), ref), "昨天")

    def test_older_and_future_dates_have_nonnegative_labels(self):
        ref = datetime(2026, 10, 5, 12, tzinfo=timezone(timedelta(hours=8)))
        for offset, label in ((0, "今天"), (-2, "2 天前"), (1, "明天"), (3, "3 天后")):
            with self.subTest(offset=offset):
                self.assertEqual(R.calendar_age(ref + timedelta(days=offset), ref), label)

    def test_browser_labels_change_after_midnight_without_regenerating_html(self):
        try:
            from PySide6.QtCore import QCoreApplication
            from PySide6.QtQml import QJSEngine
        except ImportError:
            self.skipTest("Qt JavaScript engine unavailable")
        app = QCoreApplication.instance() or QCoreApplication([])
        engine = QJSEngine()
        result = engine.evaluate(R.CALENDAR_JS)
        self.assertFalse(result.isError(), result.toString())
        # Numeric constructors use the browser's local timezone, as the panel does.
        before = engine.evaluate("calendarAge(new Date(2026, 9, 4, 23, 34), new Date(2026, 9, 4, 23, 59))")
        after = engine.evaluate("calendarAge(new Date(2026, 9, 4, 23, 34), new Date(2026, 9, 5, 0, 1))")
        self.assertEqual(before.toString(), "今天")
        self.assertEqual(after.toString(), "昨天")
        self.assertEqual(engine.evaluate("calendarAge('not a timestamp', new Date())").toString(), "")


if __name__ == "__main__":
    unittest.main()
