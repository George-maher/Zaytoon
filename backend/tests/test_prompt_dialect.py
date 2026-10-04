"""The tool-call dialect, as the model actually receives it.

Regression cover for a live bug: the system templates were written with
``str.format``-style ``{{...}}`` escaping but are rendered with
``str.replace``, so the model was *shown* doubled braces, copied them verbatim
(``{{"action": "get_time"}}``), ``json.loads`` rejected them, and raw tool
JSON reached the user as the assistant's answer — on the Arabic path, where
the copy was most literal.
"""

from agents.engine import (
    build_system_prompt,
    extract_tool_calls,
    looks_like_tool_call,
    SYSTEM_AR,
    SYSTEM_EN,
)


class TestPromptShowsTheDialectItExpects:
    def test_english_template_has_no_double_braces(self):
        prompt = build_system_prompt("- get_time() [safe] now", "en")
        assert "{{" not in prompt
        assert '{"action": "tool_name", "params": {...}}' in prompt
        assert "- get_time() [safe] now" in prompt

    def test_arabic_template_has_no_double_braces_or_chinese(self):
        prompt = build_system_prompt("- get_time() [safe] الآن", "ar")
        assert "{{" not in prompt
        assert '{"action": "اسم_الأداة", "params": {...}}' in prompt
        assert "JSON对象" not in prompt  # stray Chinese from an old edit
        assert "JSON object" in prompt

    def test_catalogue_is_substituted_not_escaped(self):
        prompt = build_system_prompt("CATALOGUE_MARKER", "en")
        assert "CATALOGUE_MARKER" in prompt
        assert "{catalogue}" not in prompt

    def test_both_languages_forbid_answering_from_memory(self):
        """A live run answered an Arabic *time* question from memory (and was
        ~4 minutes wrong). Both templates must now force the tool."""
        en = build_system_prompt("x", "en")
        ar = build_system_prompt("x", "ar")
        assert "must come from the tool" in en
        assert "call the time tool first" in en
        assert "لازم تيجي من الأداة" in ar
        assert "نادِ أداة الوقت" in ar

    def test_arabic_path_mandates_an_arabic_reply(self):
        """AGENTS.md: agent prompts must carry the Arabic-only mandate.

        A live Arabic tool turn called the right tool and then answered in
        English. The mandate now sits at the *end* of the template — a 3B
        model attends to the last instruction far more reliably than the
        first — and must promise to translate tool output rather than echo it.
        """
        ar = build_system_prompt("TOOLS_HERE", "ar")
        assert "بالعربي المصري فقط" in ar
        assert "ممنوع الرد" in ar
        assert "ترجم النتيجة" in ar
        # Last thing in the prompt, after the rules.
        assert ar.rindex("بالعربي المصري فقط") > ar.index("قواعد:")
        assert ar.rstrip().endswith("بالعربي.")


class TestDoubledBraceOutput:
    """A model that copied ``{{...}}`` still becomes a real tool call."""

    def test_doubled_braces_are_unwrapped(self):
        calls, prose = extract_tool_calls('{{"action": "get_time"}}')
        assert [c.name for c in calls] == ["get_time"]
        assert not prose.strip()

    def test_doubled_braces_with_params(self):
        calls, _ = extract_tool_calls(
            '{{"action": "calculator", "params": {"expression": "2+2"}}}'
        )
        assert len(calls) == 1
        assert calls[0].name == "calculator"
        assert calls[0].params == {"expression": "2+2"}

    def test_single_braces_still_work(self):
        calls, _ = extract_tool_calls('{"action": "get_time"}')
        assert [c.name for c in calls] == ["get_time"]


class TestUnparsedJsonIsNeverShownToTheUser:
    def test_a_whole_json_reply_is_tool_shaped(self):
        # If the scanners could not use it, it must degrade to an apology
        # rather than reaching the transcript verbatim.
        assert looks_like_tool_call('{"action": "get_time"}') is True
        assert looks_like_tool_call('{{"action": "nope", "x": }') is True

    def test_plain_prose_is_not(self):
        assert looks_like_tool_call("The current time is 12:00.") is False
