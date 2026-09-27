import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip() or default


# ---------------------------------------------------------------------------
# Free-tier model policy (zero-cost guard)
# ---------------------------------------------------------------------------
#: OpenRouter's auto-router that only ever selects free models.
FREE_ROUTER_ENDPOINT = "openrouter/free"

#: Suffix OpenRouter uses to mark zero-cost variants.
FREE_MODEL_SUFFIX = ":free"

#: The ONLY models this application will ever call. Hard-coded by design: the
#: allow-list is the primary guard, the ``:free`` shape check is the second.
#: To adopt a different free model, edit this set and the ``.env`` together.
ALLOWED_FREE_MODELS = frozenset({
    # Primary: chat, tools, routing.
    "qwen/qwen3.8-27b:free",
    # Reasoning: code, planning, scheduling.
    "thinkingmachines/inkling-small:free",
    # Fallback: OpenRouter's free-only auto-router.
    "openrouter/free",
})


def is_free_model(model: str) -> bool:
    """True when ``model`` is in a guaranteed zero-cost form.

    Two forms qualify:

    * the free router endpoint, e.g. ``openrouter/free``
    * any model id whose variant tag is ``:free``, e.g. ``qwen/qwen3.8-27b:free``

    Anything else (including an untagged model such as ``openai/gpt-4o``) is
    treated as billable and is rejected before a request is ever sent.
    """
    if not model or not isinstance(model, str):
        return False
    candidate = model.strip()
    if not candidate:
        return False
    if candidate.lower() == FREE_ROUTER_ENDPOINT:
        return True
    return candidate.lower().endswith(FREE_MODEL_SUFFIX)


def is_allowed_model(model: str) -> bool:
    """True only for an allow-listed model that is also in a free form.

    This is the check the request path uses. Being free is necessary but not
    sufficient — an un-allow-listed ``:free`` model is still refused, so a
    typo or an injected model id cannot reach the network.
    """
    if not is_free_model(model):
        return False
    return model.strip() in ALLOWED_FREE_MODELS


# The single supported provider. Local model execution has been removed.
PROVIDER_OPENROUTER = "openrouter"
VALID_PROVIDERS = (PROVIDER_OPENROUTER,)


class NonFreeModelError(ValueError):
    """Raised when a billable or non-allow-listed model is requested.

    The zero-cost policy is enforced locally so an accidental typo
    (``OPENROUTER_PRIMARY_MODEL=openai/gpt-4o``) can never produce a charge.
    """

    def __init__(self, model: str):
        self.model = model
        allowed = ", ".join(sorted(ALLOWED_FREE_MODELS))
        super().__init__(
            f"Refusing model {model!r}: Zaytoon may only call allow-listed "
            f"free models ({allowed})."
        )

# Model tiers used to build the fallback chain.
TIER_FAST = "fast"
TIER_REASONING = "reasoning"
TIER_FALLBACK = "fallback"
VALID_TIERS = (TIER_FAST, TIER_REASONING, TIER_FALLBACK)


class Config:
    # ------------------------------------------------------------------
    # LLM providers
    # ------------------------------------------------------------------
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
    OPENROUTER_BASE_URL: str = _env_str(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    ).rstrip("/")

    # OPENROUTER_PRIMARY_MODEL supersedes the legacy OPENROUTER_MODEL var.
    OPENROUTER_PRIMARY_MODEL: str = _env_str(
        "OPENROUTER_PRIMARY_MODEL",
        _env_str("OPENROUTER_MODEL", "qwen/qwen3.8-27b:free"),
    )
    OPENROUTER_REASONING_MODEL: str = _env_str(
        "OPENROUTER_REASONING_MODEL", "thinkingmachines/inkling-small:free"
    )
    OPENROUTER_FALLBACK_MODEL: str = _env_str(
        "OPENROUTER_FALLBACK_MODEL", "openrouter/free"
    )

    # OpenRouter attribution headers (required for their dashboard/ranking).
    OPENROUTER_SITE_URL: str = _env_str(
        "OPENROUTER_SITE_URL", "https://github.com/zaytoon-ai"
    )
    OPENROUTER_APP_TITLE: str = _env_str(
        "OPENROUTER_APP_TITLE", "Zaytoon AI Assistant"
    )

    #: The only provider. Local model execution (Ollama/Llama) has been removed.
    PROVIDER: str = PROVIDER_OPENROUTER

    LLM_TIMEOUT: float = _env_float("LLM_TIMEOUT", 120.0)
    LLM_MAX_RETRIES: int = _env_int("LLM_MAX_RETRIES", 2)
    # A model that just rate-limited or 5xx'd is benched for this long instead
    # of being retried on every single request.
    LLM_PROVIDER_COOLDOWN: float = _env_float("LLM_PROVIDER_COOLDOWN", 60.0)
    LLM_MAX_TOKENS_CAP: int = _env_int("LLM_MAX_TOKENS_CAP", 8192)

    # Second line of defence; the allow-list below is the primary one.
    LLM_REQUIRE_FREE_MODELS: bool = _env_bool("LLM_REQUIRE_FREE_MODELS", True)

    # ------------------------------------------------------------------
    # Derived / legacy accessors
    # ------------------------------------------------------------------
    # Legacy alias kept so older code and tests keep working.
    OPENROUTER_MODEL: str = OPENROUTER_PRIMARY_MODEL

    @property
    def LLM_API_URL(self) -> str:
        return f"{self.OPENROUTER_BASE_URL}/chat/completions"

    @property
    def LLM_HEADERS(self) -> dict:
        return {
            "Authorization": f"Bearer {self.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.OPENROUTER_SITE_URL,
            "X-Title": self.OPENROUTER_APP_TITLE,
        }

    @property
    def llm_configured(self) -> bool:
        """True when we have what we need to call OpenRouter at all."""
        return bool(self.OPENROUTER_API_KEY)

    def model_for_tier(self, tier: str) -> str:
        """Resolve the model id for a given capability tier."""
        if tier == TIER_REASONING:
            return self.OPENROUTER_REASONING_MODEL
        if tier == TIER_FALLBACK:
            return self.OPENROUTER_FALLBACK_MODEL
        return self.OPENROUTER_PRIMARY_MODEL

    def validate_free_models(self) -> list[str]:
        """Configured models that violate the free-model policy.

        A model is acceptable only if it is BOTH in the hard-coded allow-list
        AND in a free form (``<vendor>/<model>:free`` or ``openrouter/free``).
        An empty list means the configuration honours the zero-cost policy.
        """
        return [
            name
            for name in (
                self.OPENROUTER_PRIMARY_MODEL,
                self.OPENROUTER_REASONING_MODEL,
                self.OPENROUTER_FALLBACK_MODEL,
            )
            if not is_allowed_model(name)
        ]

    @property
    def free_models_enforced(self) -> bool:
        return self.LLM_REQUIRE_FREE_MODELS

    def assert_free_model(self, model: str) -> str:
        """Guard used on the request path. Returns ``model`` if acceptable."""
        if not self.LLM_REQUIRE_FREE_MODELS:
            return model
        if not is_allowed_model(model):
            raise NonFreeModelError(model)
        return model

    def describe_llm(self) -> dict:
        """Small dict for /health and startup logs."""
        return {
            "provider": self.PROVIDER,
            "free_only": self.LLM_REQUIRE_FREE_MODELS,
            "configured": self.llm_configured,
            "base_url": self.LLM_API_URL,
            "primary_model": self.OPENROUTER_PRIMARY_MODEL,
            "reasoning_model": self.OPENROUTER_REASONING_MODEL,
            "fallback_model": self.OPENROUTER_FALLBACK_MODEL,
            "allowed_models": sorted(ALLOWED_FREE_MODELS),
            "rejected_models": self.validate_free_models(),
        }

    # ------------------------------------------------------------------
    # Server / storage
    # ------------------------------------------------------------------
    BACKEND_HOST: str = os.getenv("BACKEND_HOST", "127.0.0.1")
    BACKEND_PORT: int = _env_int("BACKEND_PORT", 8000)

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/zaytoon.db")

    # ------------------------------------------------------------------
    # Workspace (the root every file tool is confined to)
    # ------------------------------------------------------------------
    # Previously `tools.sandbox.is_safe_path()` defaulted to `os.getcwd()`,
    # which meant the assistant could only ever touch `backend/` — because the
    # backend must be started from there. "Create a folder called AI Projects",
    # inspecting a repository, and any git operation were therefore impossible.
    #
    # The workspace is an explicit, user-chosen directory. It defaults to a
    # dedicated folder under the user's home rather than the repository root, so
    # the assistant is never silently granted write access to the checkout.
    DEFAULT_WORKSPACE_DIRNAME: str = "ZaytoonWorkspace"

    WORKSPACE_ROOT: str = _env_str(
        "WORKSPACE_ROOT",
        str(Path.home() / DEFAULT_WORKSPACE_DIRNAME),
    )

    #: Confine every file tool to the workspace even if a caller forgets to
    #: pass a root. Always on; there is no supported way to disable it.
    WORKSPACE_ENFORCED: bool = _env_bool("WORKSPACE_ENFORCED", True)

    @property
    def workspace_path(self) -> Path:
        """The resolved workspace root.

        Created on first access so a fresh install works with no setup. An
        unresolvable override falls back to the default rather than raising, so
        a typo degrades to a safe location instead of breaking the backend.
        """
        for candidate in (
            Path(os.path.expandvars(str(self.WORKSPACE_ROOT))).expanduser(),
            Path.home() / self.DEFAULT_WORKSPACE_DIRNAME,
        ):
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
        raise RuntimeError(
            f"Cannot create a workspace directory (tried {self.WORKSPACE_ROOT!r} "
            f"and {Path.home() / self.DEFAULT_WORKSPACE_DIRNAME!r})."
        )

    def describe_workspace(self) -> dict:
        """Small dict for /health and the settings endpoint."""
        try:
            root = str(self.workspace_path)
            exists = Path(root).is_dir()
        except Exception:  # pragma: no cover - defensive
            root, exists = str(self.WORKSPACE_ROOT), False
        return {
            "root": root,
            "enforced": self.WORKSPACE_ENFORCED,
            "available": exists,
        }

    # ------------------------------------------------------------------
    # Voice
    # ------------------------------------------------------------------
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "tiny")
    WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "cpu")
    WHISPER_COMPUTE: str = os.getenv("WHISPER_COMPUTE", "int8")

    TTS_VOICE_AR: str = os.getenv("TTS_VOICE_AR", "ar-EG-ShakirNeural")
    TTS_VOICE_EN: str = os.getenv("TTS_VOICE_EN", "en-GB-RyanNeural")
    TTS_RATE: str = os.getenv("TTS_RATE", "+0%")
    TTS_VOLUME: str = os.getenv("TTS_VOLUME", "+0%")

    TTS_STREAM_EARLY: bool = _env_bool("TTS_STREAM_EARLY", True)
    TTS_SENTENCE_TIMEOUT: float = 1.5

    # ------------------------------------------------------------------
    # Tool safety sandbox (human-in-the-loop)
    # ------------------------------------------------------------------
    TOOL_SAFETY_ENABLED: bool = _env_bool("TOOL_SAFETY_ENABLED", True)
    # How long a high-risk action waits for the user's approve/deny reply.
    TOOL_CONFIRM_TIMEOUT: float = _env_float("TOOL_CONFIRM_TIMEOUT", 120.0)
    # Actions that always require confirmation, regardless of their parameters.
    # `system_command` is intentionally NOT here: its content is inspected by
    # HIGH_RISK_COMMAND_PATTERNS, so `rm -rf /` is gated while `echo hi` is not.
    # Add it back here if you want to gate every shell invocation.
    TOOL_ALWAYS_CONFIRM: str = _env_str(
        "TOOL_ALWAYS_CONFIRM", "run_code,file_delete,registry_write"
    )

    # ------------------------------------------------------------------
    # Rate limiting / misc
    # ------------------------------------------------------------------
    RATE_LIMIT_MAX: int = _env_int("RATE_LIMIT_MAX", 60)
    RATE_LIMIT_WINDOW: int = _env_int("RATE_LIMIT_WINDOW", 60)

    MAX_MESSAGE_LENGTH: int = 8000
    MAX_CONTEXT_MESSAGES: int = 50
    REMINDER_CHECK_INTERVAL: int = 15

    ASSISTANT_NAME: str = os.getenv("ASSISTANT_NAME", "Zaytoon")
    WAKE_SESSION_TIMEOUT: float = _env_float("WAKE_SESSION_TIMEOUT", 10.0)
    # Master switch for the wake-word gate. With the gate ON, an utterance that
    # does not start with "Zaytoon" is discarded with only a "listening" status,
    # which reads to the user as "the app is ignoring me". Turning it off makes
    # every transcript actionable, which is what you want for desk testing or
    # when the mic is already an explicit, deliberate action.
    WAKE_WORD_REQUIRED: bool = _env_bool("WAKE_WORD_REQUIRED", True)
    # When an utterance is dropped by the gate, tell the client it was dropped
    # instead of silently replying "listening".
    WAKE_WORD_HINT: bool = _env_bool("WAKE_WORD_HINT", True)


config = Config()
