from voice.tts import TextToSpeech


def test_split_sentences_basic():
    tts = TextToSpeech()
    result = tts.split_sentences("Hello world. How are you? I'm fine.")
    assert len(result) >= 2


def test_split_sentences_arabic():
    tts = TextToSpeech()
    text = "\u0645\u0631\u062D\u0628\u0627. \u0643\u064A\u0641 \u0627\u0644\u062D\u0627\u0644\u061F \u0623\u0646\u0627 \u0628\u062E\u064A\u0631."
    result = tts.split_sentences(text)
    assert len(result) >= 2


def test_split_sentences_single():
    tts = TextToSpeech()
    result = tts.split_sentences("Hello world")
    assert len(result) == 1
    assert result[0] == "Hello world"


def test_split_sentences_empty():
    tts = TextToSpeech()
    assert tts.split_sentences("") == []
    assert tts.split_sentences("   ") == []


def test_split_sentences_arabic_comma():
    tts = TextToSpeech()
    text = "\u0627\u0630\u0647\u0628 \u0625\u0644\u0649 \u0627\u0644\u0645\u0643\u062A\u0628\u060C \u062B\u0645 \u0627\u0631\u062C\u0639."
    result = tts.split_sentences(text)
    assert len(result) >= 1


def test_split_sentences_newline():
    tts = TextToSpeech()
    result = tts.split_sentences("Line one.\n\nLine two.")
    assert len(result) >= 2


def test_cache_key_generation():
    tts = TextToSpeech()
    key1 = tts._get_cache_key("Hello world", "ar-EG-ShakirNeural")
    key2 = tts._get_cache_key("Hello world!", "ar-EG-ShakirNeural")
    assert key1 != key2


def test_synthesize_returns_none_when_not_available():
    tts = TextToSpeech()
    tts._available = False
    import asyncio
    result = asyncio.run(tts.synthesize("Hello"))
    assert result is None


def test_synthesize_returns_none_empty_text():
    tts = TextToSpeech()
    import asyncio
    result = asyncio.run(tts.synthesize(""))
    assert result is None


def test_stream_synthesize_not_available():
    tts = TextToSpeech()
    tts._available = False
    import asyncio
    async def test():
        chunks = []
        async for chunk in tts.stream_synthesize("Hello"):
            chunks.append(chunk)
        assert len(chunks) == 0
    asyncio.run(test())


async def test_a_missing_package_is_recorded_as_a_reason():
    """Silence with a cause must leave a trace the transport can read."""
    tts = TextToSpeech()
    tts._available = False
    [c async for c in tts.stream_synthesize("Hello")]
    assert tts.last_stream_error == "edge-tts is not installed"


async def test_a_swallowed_stream_failure_leaves_a_readable_reason(monkeypatch):
    """`stream_synthesize` never raises — so silence must still be traceable.

    One dead sentence must not eat a reply that already streamed as text,
    which also makes the failure invisible from the transport's side. The
    recorded class name is the seam that lets the transport name it; the
    detail stays in the redacted log line.
    """
    import edge_tts

    class Boom:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            raise RuntimeError("refused")
            yield b""  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(edge_tts, "Communicate", Boom)
    tts = TextToSpeech()
    tts._available = True

    chunks = [c async for c in tts.stream_synthesize("hello there")]

    assert chunks == []
    assert tts.last_stream_error == "RuntimeError"
    assert "refused" not in (tts.last_stream_error or "")


async def test_a_successful_stream_clears_a_previous_failure(monkeypatch):
    """A recovered service must re-arm the transport's one-shot notice."""
    import edge_tts

    class Refuser:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            raise RuntimeError("refused")
            yield b""  # pragma: no cover

    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"\x00\x01"}

    monkeypatch.setattr(edge_tts, "Communicate", Refuser)
    tts = TextToSpeech()
    tts._available = True
    [c async for c in tts.stream_synthesize("hello there")]
    assert tts.last_stream_error == "RuntimeError"

    monkeypatch.setattr(edge_tts, "Communicate", Worker)
    chunks = [c async for c in tts.stream_synthesize("hello again")]

    assert chunks == [b"\x00\x01"]
    assert tts.last_stream_error is None


async def test_empty_text_is_nothing_to_say_not_a_failure():
    tts = TextToSpeech()
    tts._available = True
    [c async for c in tts.stream_synthesize("   ")]
    assert tts.last_stream_error is None
