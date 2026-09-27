"""Regression coverage for the restructured time parser.

The original suite only exercised a handful of phrasings, which let several
real bugs through:

* ``remind me monday at 9`` resolved a *year* out, because ``dateparser`` ran
  first and short-circuited the hand-written matchers.
* Arabic day names ending in ``ة`` never matched, because ``normalize_arabic``
  folds ``ة`` to ``ه`` before the keyword tables are consulted -- so
  ``الجمعة الساعة 3`` silently produced no reminder at all.
* Day offsets were counted twice when a phrase carried both a day keyword and
  a bare hour (``after tomorrow at 10am`` became +3 days, not +2).

These tests pin the behaviour of every layer of :meth:`TimeParser.parse_time`.
"""

from datetime import datetime, timedelta

import pytest

from scheduler.parser import TimeParser as T


@pytest.fixture
def ref():
    """A fixed Saturday 20:30 local time, so expectations are deterministic."""
    base = datetime.now().replace(
        hour=20, minute=30, second=0, microsecond=0
    )
    # Land on a Saturday (weekday 5) so weekday arithmetic is easy to read.
    while base.weekday() != 5:
        base += timedelta(days=1)
    return base


def offset_days(result, ref):
    return (result.date() - ref.date()).days


# ---------------------------------------------------------------------------
# Day keyword resolution
# ---------------------------------------------------------------------------
class TestDayResolution:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("today", 0), ("tonight", 0), ("الليلة", 0), ("اليوم", 0),
            ("tomorrow", 1), ("tmrw", 1), ("بكرة", 1), ("بكرا", 1), ("غدا", 1),
            ("after tomorrow", 2), ("بعد بكرا", 2),
        ],
    )
    def test_relative_keywords(self, text, expected, ref):
        assert T._parse_day(T._normalize(text), ref) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            # Arabic weekday names, both the ة and the normalized ه spelling
            ("الأحد", 1), ("الاحد", 1), ("الاثنين", 2), ("الثلاثاء", 3),
            ("الأربعاء", 4), ("الخميس", 5), ("الجمعة", 6), ("الجمعه", 6),
            # bare forms
            ("جمعة", 6), ("سبت", 7), ("احد", 1),
        ],
    )
    def test_arabic_weekday_names(self, text, expected, ref):
        assert T._parse_day(T._normalize(text), ref) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("sunday", 1), ("monday", 2), ("tuesday", 3), ("wednesday", 4),
            ("thursday", 5), ("friday", 6), ("saturday", 7),
        ],
    )
    def test_english_weekday_names(self, text, expected, ref):
        assert T._parse_day(T._normalize(text), ref) == expected

    def test_longest_match_wins(self, ref):
        # "after tomorrow" must not be read as "tomorrow".
        assert T._parse_day("after tomorrow", ref) == 2
        assert T._parse_day(T._normalize("بعد بكرا"), ref) == 2

    def test_no_day_keyword(self, ref):
        assert T._parse_day("call the dentist", ref) is None


# ---------------------------------------------------------------------------
# Normalisation must not break keyword matching
# ---------------------------------------------------------------------------
class TestNormalizedVariants:
    @pytest.mark.parametrize("word", ["الجمعة", "الليلة", "النهاردة", "بكرة"])
    def test_ta_marbuta_variants_present(self, word):
        assert word in T._with_normalized_variants(
            {word: 1}
        ), "normalized spelling missing"

    def test_variant_helper_is_idempotent_for_plain_keys(self):
        base = {"monday": 0}
        assert T._with_normalized_variants(base) == base

    def test_arabic_evening_phrase_parses(self, ref):
        """The bug: الليلة -> الليله, so the keyword never matched."""
        result = T.parse_time("الليلة الساعة 8", now=ref)
        assert result is not None
        assert result.hour == 20 or result.hour == 8

    def test_arabic_named_weekday_with_hour_parses(self, ref):
        """The bug: الجمعة -> الجمعه, so no reminder was created at all."""
        result = T.parse_time("الجمعة الساعة 3", now=ref)
        assert result is not None
        assert offset_days(result, ref) == 6
        assert result.hour == 3


# ---------------------------------------------------------------------------
# Time-of-day extraction
# ---------------------------------------------------------------------------
class TestTimeOfDayExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("at 14:30", (14, 30)),
            ("at 9:05 pm", (21, 5)),
            ("8pm", (20, 0)),
            ("8 am", (8, 0)),
            ("12am", (0, 0)),
            ("12pm", (12, 0)),
            ("الساعة 14:30", (14, 30)),
            ("الساعة 7", (7, 0)),
            ("at 7", (7, 0)),
        ],
    )
    def test_extracts_explicit_times(self, text, expected):
        assert T._extract_time_of_day(T._normalize(text)) == expected

    @pytest.mark.parametrize(
        "text", ["in 3 days", "call mom", "next friday", "gibberish"]
    )
    def test_bare_numbers_are_never_times(self, text):
        assert T._extract_time_of_day(T._normalize(text)) is None

    def test_out_of_range_rejected(self):
        assert T._build_time(25, 0, None) is None
        assert T._build_time(10, 99, None) is None


# ---------------------------------------------------------------------------
# End-to-end parse_time
# ---------------------------------------------------------------------------
class TestParseTime:
    @pytest.mark.parametrize(
        "text,expected_days",
        [
            ("tonight at 8pm", (0, 1)),
            ("today at 5pm", (0, 1)),
            ("at 3pm", (0, 1)),
            ("tomorrow at 9am", (1,)),
            ("remind me tomorrow", (1,)),
            ("after tomorrow at 10am", (2,)),          # must not be +3
            ("بكرة الساعة 9", (1,)),
            ("بعد بكرا الساعة 10", (2,)),
            # 06:00 has already passed at the 20:30 reference time, so this
            # legitimately rolls to tomorrow.
            ("النهارده الساعة 6", (0, 1)),
            ("الأحد الساعة 3", (1,)),
            ("الاثنين الساعة 3", (2,)),
            ("الجمعة الساعة 3", (6,)),
            ("friday at 3pm", (6,)),
            ("monday at 9", (2,)),                       # must not be +365
            ("next friday at 4pm", (6,)),
            ("in 2 hours", (0,)),
            ("in 3 days", (3,)),
        ],
    )
    def test_known_phrasings(self, text, expected_days, ref):
        result = T.parse_time(text, now=ref)
        assert result is not None, f"{text!r} did not parse"
        assert offset_days(result, ref) in expected_days, (
            f"{text!r} -> {result} (+{offset_days(result, ref)}d), "
            f"expected one of {expected_days}"
        )

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty_returns_none(self, text):
        assert T.parse_time(text) is None

    def test_gibberish_returns_none(self, ref):
        assert T.parse_time("asdkjhqwe asdjh kjashd", now=ref) is None

    def test_weekday_is_not_driven_into_the_future(self, ref):
        """A named weekday must land within the next 7 days."""
        for text in ("monday at 9", "friday at 3pm", "sunday", "الجمعة الساعة 3"):
            result = T.parse_time(text, now=ref)
            if result is None:
                continue
            delta = offset_days(result, ref)
            assert 0 <= delta <= 7, f"{text!r} -> +{delta}d"

    def test_result_is_never_in_the_past(self, ref):
        for text in (
            "today at 5pm", "tonight at 8pm", "tomorrow at 9am",
            "بعد بكرا الساعة 10", "الأحد الساعة 3",
        ):
            result = T.parse_time(text, now=ref)
            assert result is not None
            assert result > ref, f"{text!r} resolved into the past: {result}"

    def test_explicit_day_is_not_rolled_forward(self, ref):
        """"friday at 9" stays on Friday even though it is >24h away."""
        result = T.parse_time("friday at 9", now=ref)
        assert result.weekday() == 4  # Friday
        assert offset_days(result, ref) == 6

    def test_named_day_keeps_its_weekday(self, ref):
        for text, weekday in (
            ("monday at 9", 0), ("tuesday at 9", 1), ("wednesday at 9", 2),
            ("thursday at 9", 3), ("friday at 9", 4),
        ):
            result = T.parse_time(text, now=ref)
            assert result is not None, text
            assert result.weekday() == weekday, text

    def test_arabic_indic_digits_understood(self, ref):
        result = T.parse_time("بكرة الساعة ٩", now=ref)
        assert result is not None
        assert result.hour == 9
        assert offset_days(result, ref) == 1


# ---------------------------------------------------------------------------
# Relative-from-now
# ---------------------------------------------------------------------------
class TestRelativeFromNow:
    @pytest.mark.parametrize(
        "text,kwargs",
        [
            ("in 30 minutes", {"minutes": 30}),
            ("in 2 hours", {"hours": 2}),
            ("in 3 days", {"days": 3}),
            ("in 1 week", {"weeks": 1}),
        ],
    )
    def test_english_relative(self, text, kwargs, ref):
        assert T.parse_time(text, now=ref) == ref + timedelta(**kwargs)

    @pytest.mark.parametrize(
        "text,kwargs",
        [
            ("بعد 5 د", {"minutes": 5}),
            ("بعد 2 ساعة", {"hours": 2}),
            ("بعد 3 يوم", {"days": 3}),
        ],
    )
    def test_arabic_relative(self, text, kwargs, ref):
        assert T.parse_time(text, now=ref) == ref + timedelta(**kwargs)

    def test_relative_is_not_confused_with_a_time(self, ref):
        """"in 3 days" must not be read as 03:00."""
        result = T.parse_time("in 3 days", now=ref)
        assert result.hour == ref.hour
        assert result.minute == ref.minute


# ---------------------------------------------------------------------------
# dateparser is a fallback, not the primary path
# ---------------------------------------------------------------------------
class TestDateparserIsLastResort:
    def test_not_used_when_structured_path_understands(self, monkeypatch, ref):
        """If dateparser were consulted first, 'monday at 9' would drift a year."""
        import dateparser

        def explode(*a, **k):
            raise AssertionError("dateparser must not run for a known phrasing")

        monkeypatch.setattr(dateparser, "parse", explode)
        assert T.parse_time("monday at 9", now=ref) is not None
        assert T.parse_time("الجمعة الساعة 3", now=ref) is not None
        assert T.parse_time("بعد بكرا الساعة 10", now=ref) is not None

    def test_used_only_for_unknown_shapes(self, monkeypatch, ref):
        import dateparser

        calls = []
        original = dateparser.parse

        def spy(*a, **k):
            calls.append(a[0])
            return original(*a, **k)

        monkeypatch.setattr(dateparser, "parse", spy)
        T.parse_time("asdkjhqwe kjashd", now=ref)
        assert calls, "dateparser should be tried as a last resort"

    def test_dateparser_exception_is_swallowed(self, monkeypatch, ref):
        import dateparser

        def boom(*a, **k):
            raise RuntimeError("dateparser blew up")

        monkeypatch.setattr(dateparser, "parse", boom)
        # Must not propagate; just returns None.
        assert T.parse_time("asdkjhqwe kjashd", now=ref) is None


# ---------------------------------------------------------------------------
# Task extraction unchanged
# ---------------------------------------------------------------------------
class TestExtractTask:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("remind me to buy groceries", "buy groceries"),
            ("remind me tomorrow at 6pm to study", "study"),
            ("فكرني بعد ساعة أذاكر", "أذاكر"),
        ],
    )
    def test_extracts_task(self, text, expected):
        assert expected in T.extract_task(text)
