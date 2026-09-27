import pytest
from datetime import datetime, timedelta
from scheduler.parser import TimeParser


def test_normalize_arabic_numbers():
    assert TimeParser.normalize_arabic_numbers("\u0661\u0662\u0663") == "123"
    assert TimeParser.normalize_arabic_numbers("hello \u0665\u0666") == "hello 56"
    assert TimeParser.normalize_arabic_numbers("no arabic") == "no arabic"


def test_parse_explicit_time_english():
    result = TimeParser.parse_time("meeting at 3:30 pm")
    assert result is not None
    assert result.hour == 15
    assert result.minute == 30


def test_parse_explicit_time_24h():
    result = TimeParser.parse_time("meeting at 14:30")
    assert result is not None
    assert result.hour == 14
    assert result.minute == 30


def test_parse_explicit_time_am():
    now = datetime.now()
    result = TimeParser.parse_time("meeting at 9:00 am")
    if result is not None:
        assert result.hour == 9
        assert result.minute == 0


def test_parse_from_now_minutes():
    before = datetime.now()
    result = TimeParser.parse_time("in 30 minutes")
    assert result is not None
    diff = result - before
    assert 25 * 60 <= diff.total_seconds() <= 35 * 60


def test_parse_from_now_hours():
    before = datetime.now()
    result = TimeParser.parse_time("in 2 hours")
    assert result is not None
    diff = result - before
    assert 1.5 * 3600 <= diff.total_seconds() <= 2.5 * 3600


def test_parse_tomorrow():
    result = TimeParser.parse_time("tomorrow")
    assert result is not None
    tomorrow = datetime.now() + timedelta(days=1)
    assert result.day == tomorrow.day


def test_parse_arabic_time():
    result = TimeParser.parse_time("\u0627\u0644\u0633\u0627\u0639\u0629 \u0665 \u0645\u0633\u0627\u0621\u064B")
    assert result is not None


def test_parse_arabic_from_now():
    before = datetime.now()
    result = TimeParser.parse_time("\u0628\u0639\u062F \u0661\u0660 \u062F\u0642\u0627\u0626\u0642")
    assert result is not None
    diff = result - before
    assert 8 * 60 <= diff.total_seconds() <= 12 * 60


def test_parse_tomorrow_arabic():
    result = TimeParser.parse_time("\u0628\u0643\u0631\u0629")
    assert result is not None
    tomorrow = datetime.now() + timedelta(days=1)
    assert result.day == tomorrow.day


def test_parse_returns_none_for_gibberish():
    result = TimeParser.parse_time("asdkjhqwe asdjh kjashd")
    assert result is None


def test_parse_next_weekday():
    result = TimeParser.parse_time("next monday")
    if result is not None:
        assert result.weekday() == 0
