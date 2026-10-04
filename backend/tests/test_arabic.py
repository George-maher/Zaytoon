from arabic import (
    detect_language,
    is_egyptian,
    normalize_arabic,
    clean_and_detect,
    get_tts_voice,
    language_name,
)


def test_detect_english():
    assert detect_language("Hello, how are you?") == "en"
    assert detect_language("This is a test message with no Arabic.") == "en"


def test_detect_arabic():
    assert detect_language("\u0645\u0631\u062D\u0628\u0627 \u0643\u064A\u0641 \u0627\u0644\u062D\u0627\u0644\u061F") == "ar"
    assert detect_language("\u0623\u0646\u0627 \u0628\u062D\u0628 \u0627\u0644\u0628\u0631\u0645\u062C\u0629") == "ar"


def test_detect_egyptian_with_few_arabic_chars():
    assert detect_language("\u0625\u0632\u0627\u064A\u0643 \u064A\u0627 \u0641\u0646\u062F\u0645\u061F") == "ar"


def test_detect_empty():
    assert detect_language("") == "en"
    assert detect_language(None) == "en"


def test_is_egyptian_true():
    assert is_egyptian("\u0625\u0632\u0627\u064A\u0643") is True
    assert is_egyptian("\u0639\u0627\u064A\u0632 \u0627\u064A\u0647") is True


def test_is_egyptian_false():
    assert is_egyptian("") is False
    assert is_egyptian("Hello world") is False


def test_normalize_arabic_alif():
    assert normalize_arabic("\u0622\u0643\u0644") == "\u0627\u0643\u0644"
    assert normalize_arabic("\u0623\u0643\u0644") == "\u0627\u0643\u0644"
    assert normalize_arabic("\u0625\u0643\u0644") == "\u0627\u0643\u0644"


def test_normalize_arabic_indic_numbers():
    assert normalize_arabic("\u0661\u0662\u0663") == "123"
    assert normalize_arabic("\u0660\u0669") == "09"


def test_normalize_arabic_tashkeel():
    assert normalize_arabic("\u0643\u0650\u062A\u064E\u0627\u0628") == "\u0643\u062A\u0627\u0628"


def test_normalize_empty():
    assert normalize_arabic("") == ""
    assert normalize_arabic(None) == ""


def test_clean_and_detect_english():
    result = clean_and_detect("Hello world")
    assert result["text"] == "Hello world"
    assert result["language"] == "en"


def test_clean_and_detect_arabic():
    result = clean_and_detect("\u0645\u0631\u062D\u0628\u0627")
    assert result["language"] == "ar"
    assert result["text"] == "\u0645\u0631\u062D\u0628\u0627"


def test_clean_and_detect_empty():
    result = clean_and_detect("")
    assert result["text"] == ""
    assert result["language"] == "en"


def test_get_tts_voice_ar():
    voice = get_tts_voice("ar")
    assert "ar-EG" in voice


def test_get_tts_voice_en():
    voice = get_tts_voice("en")
    assert "en-GB" in voice


def test_get_tts_voice_unknown():
    voice = get_tts_voice("fr")
    assert "en-GB" in voice


def test_language_name():
    assert language_name("ar") == "\u0627\u0644\u0639\u0631\u0628\u064A\u0629"
    assert language_name("en") == "English"
    assert language_name("fr") == "fr"
