"""Natural-language time parsing for Arabic (Egyptian) and English.

Design
------
Resolution happens in a single pass so a day offset and a time-of-day can never
be counted twice:

1. Normalise (Arabic-Indic digits, then Arabic orthography).
2. Self-contained relative expressions ("in 2 hours", "بعد 3 أيام") - these
   carry their own offset and need no further work.
3. Otherwise resolve the **day** once and the **time-of-day** once, then
   combine them.
4. `dateparser` is a genuine last resort, used only when the structured path
   finds nothing. It used to run *first* and short-circuit the Arabic-aware
   matchers, which produced nonsense such as "monday at 9" resolving a year out.

Note on normalisation: ``arabic.normalize_arabic`` maps ``ة`` to ``ه``, so
``الجمعة`` becomes ``الجمعه`` and ``الليلة`` becomes ``الليله``. The keyword
tables below therefore carry both spellings -- see :func:`_with_normalized_variants`.
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("Zaytoon.Scheduler.Parser")


class TimeParser:
    #: Set once by :meth:`_match_dateparser` so the missing-dependency warning
    #: is emitted a single time per process.
    _dateparser_warned = False
    ARABIC_NUMBERS = {
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    }

    #: Genuine weekday names -> Python weekday index (Monday=0).
    #: Relative keywords are deliberately NOT here; see RELATIVE_DAYS.
    ARABIC_DAYS = {
        "السبت": 5, "الأحد": 6, "الاثنين": 0, "الثلاثاء": 1,
        "الأربعاء": 2, "الخميس": 3, "الجمعة": 4,
        "سبت": 5, "احد": 6, "اثنين": 0, "ثلاثاء": 1,
        "اربعاء": 2, "خميس": 3, "جمعة": 4,
    }

    ENGLISH_DAYS = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    }

    #: Relative day keywords -> fixed offset in days.
    #: These must be resolved as literals. Treating "tomorrow" as a weekday
    #: (which an earlier version of ARABIC_DAYS did) makes the weekday
    #: arithmetic return the next matching weekday rather than tomorrow.
    RELATIVE_DAYS = {
        "بعد بكره": 2, "بعد بكرا": 2, "بعد بكرة": 2, "بعد بكر": 2,
        "بكره": 1, "بكرا": 1, "بكرة": 1, "بكر": 1, "غدا": 1, "غداً": 1,
        "after tomorrow": 2, "day after tomorrow": 2,
        "tomorrow": 1, "tmrw": 1, "tmr": 1,
        "الليلة": 0, "اليوم": 0, "النهاردة": 0, "النهارده": 0,
        "today": 0, "tonight": 0, "now": 0,
    }

    #: Keywords that imply an evening time when none is stated.
    NIGHT_WORDS = ("tonight", "الليلة", "الليله", "evening", "مساءً", "مساء")

    #: Default time-of-day used when a day is named but no hour is given.
    DEFAULT_HOUR = 9
    #: Default hour for an unqualified "tonight".
    NIGHT_HOUR = 20

    # Meridiem markers, longest first so "مساءً" wins over "م".
    _PM = ("pm", "pm.", "م", "مساء", "مساءً", "مسائا")
    _AM = ("am", "am.", "ص", "صباح", "صباحاً", "صباحا", "صباحًا")

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------
    @classmethod
    def normalize_arabic_numbers(cls, text: str) -> str:
        for ar, en in cls.ARABIC_NUMBERS.items():
            text = text.replace(ar, en)
        return text

    @classmethod
    def _normalize(cls, text: str) -> str:
        """Lowercase, convert digits, then apply Arabic orthographic folding."""
        out = cls.normalize_arabic_numbers(str(text).lower().strip())
        try:
            from arabic import normalize_arabic

            out = normalize_arabic(out)
        except ImportError:  # pragma: no cover - arabic is a local module
            pass
        return out

    @staticmethod
    def _with_normalized_variants(table: dict) -> dict:
        """Add the ``ة``->``ه`` spelling of every Arabic key.

        ``normalize_arabic`` rewrites ``ة`` to ``ه``, so the matcher would
        otherwise never see ``الجمعة`` and the keyword would silently miss.
        """
        out = dict(table)
        for key, value in table.items():
            if "ة" in key:
                out[key.replace("ة", "ه")] = value
        return out

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    @classmethod
    def parse_time(
        cls, text: str, now: Optional[datetime] = None
    ) -> Optional[datetime]:
        if not text or not str(text).strip():
            return None
        now = now or datetime.now()
        normalized = cls._normalize(text)

        # 1. Self-contained relative expressions.
        result = cls._match_from_now(normalized, now) or cls._match_from_now_ar(
            normalized, now
        )
        if result is not None:
            return result

        # 2. Structured: resolve the day once and the time-of-day once.
        result = cls._resolve_day_and_time(normalized, now)
        if result is not None:
            return result

        # 3. Last resort. Only reached when we understood nothing ourselves.
        return cls._match_dateparser(normalized, now)

    @classmethod
    def _resolve_day_and_time(
        cls, text: str, now: datetime
    ) -> Optional[datetime]:
        day_delta = cls._parse_day(text, now)
        time_of_day = cls._extract_time_of_day(text)
        night = cls._is_night(text)

        if day_delta is None and time_of_day is None:
            return None

        target = now + timedelta(days=day_delta or 0)

        if time_of_day is not None:
            hour, minute = time_of_day
            if night and cls._is_ambiguous_hour(text, hour):
                # "tonight at 8" means 20:00, not 08:00.
                hour = hour + 12 if hour < 12 else hour
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
        elif night:
            target = target.replace(
                hour=cls.NIGHT_HOUR, minute=0, second=0, microsecond=0
            )
        else:
            target = target.replace(
                hour=cls.DEFAULT_HOUR, minute=0, second=0, microsecond=0
            )

        # Roll forward only when the *date* was implicit. "friday at 9" must
        # stay on Friday even if that is months away.
        if (day_delta is None or day_delta == 0) and target <= now:
            target += timedelta(days=1)
        return target

    # ------------------------------------------------------------------
    # Component extraction
    # ------------------------------------------------------------------
    @classmethod
    def _parse_day(cls, text: str, now: datetime) -> Optional[int]:
        """Resolve ``text`` to a day offset from ``now``, or None.

        Relative keywords ("tomorrow", "بكرة") are literal offsets and are
        checked first, longest-match first, so "after tomorrow" beats
        "tomorrow" and "بعد بكرة" beats "بكرة".
        """
        if not text:
            return None
        lowered = text.lower()

        relative = cls._with_normalized_variants(cls.RELATIVE_DAYS)
        for word in sorted(relative, key=len, reverse=True):
            if len(word) > 1 and word in lowered:
                return relative[word]

        today_weekday = now.weekday()
        days = cls._with_normalized_variants(cls.ARABIC_DAYS)
        days.update(cls.ENGLISH_DAYS)
        for word in sorted(days, key=len, reverse=True):
            if len(word) > 2 and word in lowered:
                ahead = days[word] - today_weekday
                if ahead <= 0:
                    ahead += 7
                return ahead

        return None

    @classmethod
    def _extract_time_of_day(cls, text: str) -> Optional[tuple]:
        """Return ``(hour, minute)`` for an explicit time, else None.

        Requires an unambiguous signal: a colon, a meridiem marker, or the
        Arabic/English "at"/"o'clock" prefix. A bare number is never taken, so
        "in 3 days" cannot be read as 03:00.
        """
        # 1. H:MM / H.MM, optional meridiem.
        m = re.search(
            r"(\d{1,2})\s*[:.]\s*(\d{2})\s*(am|pm|ص|م|صباحاً?|مساءً?)?", text
        )
        if m:
            return cls._build_time(int(m.group(1)), int(m.group(2)), m.group(3))

        # 2. H + meridiem (e.g. "8pm", "8 مساء").
        m = re.search(r"(\d{1,2})\s*(am|pm|ص|م|صباحاً?|مساءً?)(?!\w)", text)
        if m:
            return cls._build_time(int(m.group(1)), 0, m.group(2))

        # 3. Arabic "الساعة H" / "الساعة H:MM" (الساعة = "the hour").
        m = re.search(r"الساع[ةه]\s*(\d{1,2})(?:\s*[:.]\s*(\d{2}))?", text)
        if m:
            return cls._build_time(
                int(m.group(1)), int(m.group(2) or 0), None
            )

        # 4. English "at H" / "by H" with no minutes.
        m = re.search(r"\b(?:at|by)\s*(\d{1,2})(?!\s*[:.]\s*\d)(?!\w)", text)
        if m:
            return cls._build_time(int(m.group(1)), 0, None)

        return None

    @classmethod
    def _build_time(
        cls, hour: int, minute: int, suffix: Optional[str]
    ) -> Optional[tuple]:
        suffix = (suffix or "").lower()
        if suffix in cls._PM:
            if hour != 12:
                hour += 12
        elif suffix in cls._AM:
            if hour == 12:
                hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    @classmethod
    def _is_night(cls, text: str) -> bool:
        return any(w in text for w in cls.NIGHT_WORDS)

    @staticmethod
    def _is_ambiguous_hour(text: str, hour: int) -> bool:
        """True when no meridiem marker was given for an hour we had to infer."""
        if hour > 12:
            return False
        markers = re.search(r"(am|pm|ص|م|صباح|مساء)", text)
        return markers is None

    # ------------------------------------------------------------------
    # Relative-from-now matchers (self-contained)
    # ------------------------------------------------------------------
    @classmethod
    def _match_from_now(cls, text: str, now: datetime) -> Optional[datetime]:
        m = re.search(
            r"in\s+(\d+)\s*(minute|min|minutes|second|sec|seconds|hour|hr|hours|day|days|week|weeks|month|months)",
            text,
        )
        if not m:
            return None
        amount = int(m.group(1))
        unit = m.group(2).lower()
        deltas = {
            "minute": "minutes", "min": "minutes", "minutes": "minutes",
            "second": "seconds", "sec": "seconds", "seconds": "seconds",
            "hour": "hours", "hr": "hours", "hours": "hours",
            "day": "days", "days": "days",
            "week": "weeks", "weeks": "weeks",
            "month": "days", "months": "days",  # months approximated
        }
        kw = {deltas.get(unit, "minutes"): amount}
        return now + timedelta(**kw)

    @classmethod
    def _match_from_now_ar(cls, text: str, now: datetime) -> Optional[datetime]:
        m = re.search(
            r"بعد\s+(\d+)\s*(دقيقة|دقائق|ساعة|ساعات|د|س|يوم|ايام|ثانية|ثواني|ث|شهر|شهور|اسبوع)",
            text,
        )
        if not m:
            return None
        amount = int(m.group(1))
        unit = m.group(2)
        if unit in ("دقيقة", "دقائق", "د"):
            return now + timedelta(minutes=amount)
        if unit in ("ساعة", "ساعات", "س"):
            return now + timedelta(hours=amount)
        if unit in ("يوم", "ايام"):
            return now + timedelta(days=amount)
        if unit in ("ثانية", "ثواني", "ث"):
            return now + timedelta(seconds=amount)
        if unit in ("شهر", "شهور"):
            return now + timedelta(days=30 * amount)
        if unit == "اسبوع":
            return now + timedelta(days=7 * amount)
        return now + timedelta(minutes=amount)

    # ------------------------------------------------------------------
    # Last-resort fallback
    # ------------------------------------------------------------------
    @classmethod
    def _match_dateparser(cls, text: str, now: datetime) -> Optional[datetime]:
        try:
            import dateparser
        except ImportError:
            # Silent degradation is how this became a bug report instead of a
            # log line: the module is optional at runtime, so an install without
            # it answers every unusual phrasing with "no time found" and nothing
            # ever says why. Warned once — a per-call warning on a fallback path
            # is just noise with a timestamp on it.
            if not cls._dateparser_warned:
                cls._dateparser_warned = True
                logger.warning(
                    "dateparser is not installed; free-form time expressions "
                    "outside the built-in patterns will not resolve. "
                    "Install it with: pip install dateparser"
                )
            return None
        try:
            result = dateparser.parse(
                text,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "RELATIVE_BASE": now,
                    "RETURN_AS_TIMEZONE_AWARE": False,
                    "PARSERS": [
                        "custom-formats", "relative-time",
                        "timestamp", "absolute-time",
                    ],
                },
            )
        except Exception as e:  # pragma: no cover - dateparser is fragile
            logger.debug("dateparser failed on %r: %s", text, e)
            return None
        if result and result > now - timedelta(hours=1):
            return result
        return None

    # ------------------------------------------------------------------
    # Task text extraction
    # ------------------------------------------------------------------
    @staticmethod
    def extract_task(text: str, parsed_time: Optional[datetime] = None) -> str:
        cleanup = text.strip()

        prefixes_en = [
            r"^(remind me|remind|set reminder|set a reminder|create reminder|schedule|note)\s+",
            r"^(to\s+)?",
        ]
        prefixes_ar = [
            r"^(ذكرني|فكرني|هذكرني|عاوزك تذكرني|عايزك تذكرني|نبهني|علمني|أذكرني|اذكرني)\s+",
            r"^(عمل تذكير|ضيف تذكير|حط تذكير|اضافة مهمة|ضيف مهمة|حجز)\s+",
            r"^(عايز|عاوز)\s+",
            r"^(كل\s+\S+\s+)",
        ]
        prefixes_time = [
            r"^(to\s+(?:at|for|by)\s+\S+(?:\s+\S+){0,3}\s+)",
            r"^(at\s+\S+(?:\s+\S+){0,3}\s+)",
        ]

        for pattern in prefixes_time:
            match = re.match(pattern, cleanup, re.IGNORECASE)
            if match:
                cleanup = cleanup[match.end():]

        for pattern in prefixes_en + prefixes_ar:
            match = re.match(pattern, cleanup, re.IGNORECASE)
            if match:
                cleanup = cleanup[match.end():]

        # Remove trailing time references
        time_patterns = [
            r"\s+(at|by|before)\s+(\d{1,2}[:.]?\d{0,2}\s*(?:am|pm|ص|م|صباحاً?|مساءً?)?)$",
            r"\s+(tomorrow|today|tonight|next\s+\w+)$",
            r"\s+(بكرة|بكرا|اليوم|الليلة|النهاردة|النهارده)$",
            r"\s+(كل\s+\S+|بعد\s+\S+)$",
        ]
        for pattern in time_patterns:
            m = re.search(pattern, cleanup, re.IGNORECASE)
            if m:
                cleanup = cleanup[:m.start()]

        cleanup = cleanup.strip().strip(".,;!؟").strip()
        return cleanup or text[:80]
