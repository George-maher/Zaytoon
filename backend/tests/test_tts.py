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
