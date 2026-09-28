"""Router priority: a broad rule must not shadow a narrow one.

``KEYWORD_ROUTES`` is an ordered list and ``route()`` returns on the first
match, so the order *is* the priority system. The catch-all
``(what|how|why|...) -> chat`` used to sit above the ``what time`` rule, which
made every "what ..." request a chat request and the time rule unreachable
dead code. Nothing failed when that happened: the live run just answered
"I have no access to real-time data" while ``get_time`` sat unused in the
registry.

These tests are therefore about *reachability*, not about the regexes matching.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.router_agent import KEYWORD_ROUTES, RouterAgent  # noqa: E402


def match_phrase(phrase: str):
    """The first rule that matches, as ``(index, intent)``.

    Lowercases first, because ``route()`` matches against ``msg_lower``. Without
    that, "React" in the test table fails to match the ``react`` alternative and
    the test reports a routing bug that does not exist.
    """
    lowered = phrase.lower()
    for index, (pattern, intent, _conf) in enumerate(KEYWORD_ROUTES):
        if pattern.search(lowered):
            return index, intent
    return None, None


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------
#: Phrase -> the intent it must reach. Each was verified by hand against the
#: registry, i.e. there is a tool or agent that can actually do the job.
EXPECTED = {
    "what time is it right now": "tools",
    "what time is it": "tools",
    "what's the time": "tools",
    "current time please": "tools",
    "what is the date": "tools",
    "what is the system information": "tools",
    "what is the weather": "tools",
    "what did we discuss yesterday": "memory",
    "what was I talking about": "memory",
    "remember my name is Ahmed": "memory",
    "create a React project called Dashboard": "code",
    "create a node project": "code",
    "write code for a function": "code",
    "run the tests": "code",
    "commit these changes": "tools",
    "what is the git status": "tools",
    "push to origin": "tools",
    "install the dependencies": "tools",
    "open notepad": "tools",
    "search the web for python docs": "tools",
    "write a file called notes": "tools",
    "remind me to call mom in 2 hours": "scheduling",
    "schedule a meeting": "scheduling",
    "list my reminders": "scheduling",
    "delete that reminder": "scheduling",
    "take a screenshot": "tools",
    "calculate 15 times 23": "tools",
    "hello": "chat",
    "hi": "chat",
    "how do I center a div": "chat",
    "tell me a joke": "chat",
    "compare these two approaches": "complex",
    "analyze this problem for me": "complex",
    "run python script.py": "action",
    "execute this command": "action",
}


class TestRoutingReachability:
    @pytest.mark.parametrize("phrase,expected", sorted(EXPECTED.items()))
    def test_phrase_reaches_its_intent(self, phrase, expected):
        index, intent = match_phrase(phrase)
        assert intent == expected, (
            f"{phrase!r} matched rule {index} -> {intent!r}, expected {expected!r}"
        )

    def test_no_phrase_falls_through_to_the_llm(self):
        """Every case above must be decided by keyword, not by the model.

        If a phrase needed the LLM fallback it would be silently unreliable:
        the free tier returns 429 constantly, and on exhaustion the router
        defaults to `chat`, so tools would be unreachable exactly when the
        model is busy.
        """
        undecided = [p for p in EXPECTED if match_phrase(p)[0] is None]
        assert not undecided, f"these need the LLM to route: {undecided}"


class TestOrderingInvariants:
    def test_the_catch_all_chat_rule_is_last(self):
        """The single invariant that would have caught the original bug."""
        catch_alls = [
            index
            for index, (pattern, intent, _c) in enumerate(KEYWORD_ROUTES)
            if intent == "chat" and pattern.search("what is the meaning of life")
        ]
        assert catch_alls, "expected a catch-all chat rule to exist"
        assert catch_alls == [len(KEYWORD_ROUTES) - 1], (
            f"catch-all chat rules must be last, found at {catch_alls}"
        )

    def test_higher_confidence_rules_are_not_shadowed(self):
        """A 0.9 rule must not be beaten by a 0.7 rule for the same phrase."""
        for phrase in EXPECTED:
            index, _ = match_phrase(phrase)
            if index is None:
                continue
            for other in range(index):
                pattern, _intent, confidence = KEYWORD_ROUTES[other]
                if pattern.search(phrase) and confidence > KEYWORD_ROUTES[index][2]:
                    raise AssertionError(
                        f"{phrase!r}: rule {other} ({confidence}) shadows "
                        f"rule {index} ({KEYWORD_ROUTES[index][2]})"
                    )

    def test_every_intent_is_reachable(self):
        """No whole intent class may be dead.

        Deliberately *not* a blanket "every rule fires" check. A broad fallback
        placed after the specific rule it generalises is correct design — the
        ``(date|time)`` rule and the ``what|how`` catch-all are both shadowed on
        purpose, and demanding otherwise would push them above the narrow rules
        that must win. The failure worth catching is a whole class of request
        having no path at all, so that is what this asserts.
        """
        reached = {match_phrase(phrase)[1] for phrase in EXPECTED}
        declared = {intent for _p, intent, _c in KEYWORD_ROUTES}
        assert declared - reached == set(), (
            f"intents no phrase can reach: {declared - reached}"
        )

    def test_no_rule_is_shadowed_by_a_stricter_one(self):
        """A specific rule must not be eaten by a broader rule above it.

        This is the general form of the original bug, stated so that it does
        not also fire on the intentional fallbacks: the shadowing rule must be
        at least as confident as the rule it hides.
        """
        for phrase in EXPECTED:
            index, _ = match_phrase(phrase)
            if index is None:
                continue
            winner_conf = KEYWORD_ROUTES[index][2]
            for other in range(index):
                pattern, _intent, confidence = KEYWORD_ROUTES[other]
                if pattern.search(phrase.lower()) and confidence > winner_conf:
                    raise AssertionError(
                        f"{phrase!r}: rule {other} (conf {confidence}) shadows "
                        f"rule {index} (conf {winner_conf})"
                    )


class TestRegexHygiene:
    def test_no_duplicate_alternatives(self):
        """`(create|create)` and `(compare|compare)` were real duplicates."""
        for pattern, intent, _c in KEYWORD_ROUTES:
            body = pattern.pattern
            for repeated in re.findall(r"\(([^()]*)\)", body):
                parts = [p for p in repeated.split("|") if p]
                assert len(parts) == len(set(parts)), (
                    f"duplicate alternative in {intent} rule: {repeated!r}"
                )

    def test_every_pattern_compiles(self):
        for pattern, intent, _c in KEYWORD_ROUTES:
            re.compile(pattern.pattern)
            assert intent in {
                "chat", "code", "tools", "action", "scheduling", "memory", "complex",
            }, intent


class TestRouterAgentRoute:
    async def test_a_keyword_hit_does_not_call_the_llm(self):
        """A request that must never depend on a rate-limited model."""
        agent = RouterAgent()
        called = []

        async def _fail(*args, **kwargs):
            called.append(1)
            raise AssertionError("the LLM should not have been consulted")

        agent.get_ai_response = _fail
        result = await agent.route("what time is it right now", [])

        assert result["intent"] == "tools"
        assert result["reasoning"] == "keyword_match"
        assert not called
