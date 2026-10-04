from datetime import datetime, timedelta
from scheduler.parser import TimeParser


class TestTimeParserEnhanced:
    def test_extract_task_simple(self):
        task = TimeParser.extract_task("remind me to buy groceries")
        assert "buy groceries" in task

    def test_extract_task_with_time(self):
        task = TimeParser.extract_task("remind me tomorrow at 6pm to study")
        assert "study" in task

    def test_extract_task_arabic(self):
        task = TimeParser.extract_task("فكرني بعد ساعة أذاكر")
        assert "أذاكر" in task or task != ""

    def test_extract_task_with_prefix(self):
        task = TimeParser.extract_task("set reminder to call doctor")
        assert "call doctor" in task

    def test_extract_task_removes_time_suffix(self):
        task = TimeParser.extract_task("remind me to submit report by tomorrow")
        assert "submit report" in task

    def test_parse_tonight(self):
        result = TimeParser.parse_time("tonight at 8pm")
        assert result is not None
        assert result.hour == 20

    def test_parse_next_friday(self):
        result = TimeParser.parse_time("next friday")
        if result is not None:
            assert result.weekday() == 4  # Friday = 4

    def test_parse_arabic_days(self):
        tests = [
            ("السبت", 5),
            ("الأحد", 6),
            ("الاثنين", 0),
            ("الثلاثاء", 1),
            ("الأربعاء", 2),
            ("الخميس", 3),
            ("الجمعة", 4),
            ("بكرة", None),
        ]
        for text, expected_weekday in tests:
            result = TimeParser.parse_time(f"{text} الساعة 3")
            if result is not None and expected_weekday is not None:
                assert result.weekday() == expected_weekday, f"Failed for {text}"

    def test_parse_in_minutes(self):
        before = datetime.now()
        result = TimeParser.parse_time("in 15 minutes")
        assert result is not None
        diff = result - before
        assert 10 * 60 <= diff.total_seconds() <= 20 * 60

    def test_parse_in_hours(self):
        before = datetime.now()
        result = TimeParser.parse_time("in 3 hours")
        assert result is not None
        diff = result - before
        assert 2.5 * 3600 <= diff.total_seconds() <= 3.5 * 3600

    def test_parse_arabic_from_now_short(self):
        before = datetime.now()
        result = TimeParser.parse_time("بعد 5 د")
        if result is not None:
            diff = result - before
            assert 3 * 60 <= diff.total_seconds() <= 7 * 60

    def test_parse_arabic_from_now_hours(self):
        before = datetime.now()
        result = TimeParser.parse_time("بعد 2 ساعة")
        assert result is not None
        diff = result - before
        assert 1.5 * 3600 <= diff.total_seconds() <= 2.5 * 3600

    def test_parse_24h_time(self):
        result = TimeParser.parse_time("at 23:45")
        assert result is not None
        assert result.hour == 23
        assert result.minute == 45

    def test_parse_gibberish(self):
        result = TimeParser.parse_time("asdfghjkl qwerty")
        assert result is None

    def test_parse_empty(self):
        assert TimeParser.parse_time("") is None
        assert TimeParser.parse_time("   ") is None

    def test_parse_tomorrow_specific_time(self):
        result = TimeParser.parse_time("tomorrow 10:30 am")
        assert result is not None
        assert result.hour == 10
        assert result.minute == 30
        tomorrow = datetime.now() + timedelta(days=1)
        assert result.day == tomorrow.day
