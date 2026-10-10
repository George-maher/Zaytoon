import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# A portable install keeps its configuration beside the application rather than
# inside it, so `Config/.env` is loaded too. `override=False` throughout: a
# variable already in the environment was set deliberately (the launchers set
# ZAYTOON_HOME, a shell profile may set a key) and the file must not overwrite it.
try:
    from core import paths as _core_paths

    _config_dir = _core_paths.config_dir()
    if _config_dir is not None and (_config_dir / ".env").is_file():
        load_dotenv(_config_dir / ".env", override=False)
except Exception:  # pragma: no cover - a missing layout is not an error
    pass


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


def _default_workspace_root() -> Path:
    """Where the workspace lives when nothing says otherwise.

    Delegates to ``core.paths`` so there is exactly one answer to "where does
    Zaytoon keep the user's files" — the same answer the database, the skills
    and the secrets file get. Importing it here (rather than inlining the
    ``ZAYTOON_HOME`` check) is deliberate: a second copy is how two modules end
    up disagreeing, which is the bug ``tools/sandbox`` already had to be
    rewritten to fix.

    The import is guarded because ``config`` is imported by nearly everything,
    including code paths that run before the package is on ``sys.path``. A
    failure to resolve the layout must not stop the backend from starting.
    """
    try:
        from core import paths as _paths

        portable = _paths.workspaces_dir()
        if portable is not None:
            return portable
    except Exception:  # pragma: no cover - defensive
        pass
    return Path.home() / "ZaytoonWorkspace"


# ---------------------------------------------------------------------------
# Free-tier model policy (zero-cost guard)
# ---------------------------------------------------------------------------
# The pool, the capability registry and the selection rules all live in
# ``services/model_registry.py`` so they can be reasoned about — and tested —
# without importing the environment. Only the wiring is here.
#
# ``openrouter/free`` is recognised *in order to refuse it*. It is an
# uncontrolled auto-router: it resolves to a different arbitrary free model
# depending on upstream load, which makes a bug unreproducible and a fix
# unverifiable, and it answered a ``get_time`` request with the literal string
# "User Safety: safe" which the loop presented to the user as the answer. It is
# no longer in the pool.
from services.model_registry import (  # noqa: F401
    ALL_CAPABILITIES,
    CAP_STRUCTURED_OUTPUT,
    CAP_TOOLS,
    FREE_MODEL_SUFFIX,
    FREE_ROUTER_ENDPOINT,
    MODEL_REGISTRY,
    ModelSpec,
    is_allowlisted,
    is_free_form,
    is_free_router,
    missing_capabilities,
    select_models,
    spec_for,
)

#: The ONLY models this application will ever call, derived from the registry so
#: the two cannot drift. Hard-coded by design: a free model is still a model
#: that can fail to follow the tool protocol, and adding one is a decision.
ALLOWED_FREE_MODELS = frozenset(
    spec.id for spec in MODEL_REGISTRY.values() if spec.free
)


def is_free_model(model: str) -> bool:
    """True when ``model`` is in a guaranteed zero-cost form.

    Only ``<vendor>/<model>:free`` qualifies. ``openrouter/free`` no longer
    does: it is free, but it is not *controlled*, and a system that promises a
    FREE-only router is more useful if it never delegates the choice.

    Anything else (including an untagged model such as ``openai/gpt-4o``) is
    treated as billable and is rejected before a request is ever sent.
    """
    return is_free_form(model)


def is_allowed_model(model: str) -> bool:
    """True only for an allow-listed model that is also in a free form.

    This is the check the request path uses. Being free is necessary but not
    sufficient — an un-allow-listed ``:free`` model is still refused, so a
    typo or an injected model id cannot reach the network.
    """
    return is_allowlisted(model)


#: The supported providers. **Ollama is the default** (local, no key); OpenRouter
#: is the optional cloud path. Which one runs is decided once, here, and no path
#: ever falls back into the other implicitly — a local-only install must not
#: start making cloud calls because a daemon is down, and a cloud install must
#: not start running weights on the user's machine because a request was rate
#: limited. (This comment used to claim local execution "has been removed", with
#: `AI_PROVIDER` defaulting to `ollama` four lines below it. The README and a CI
#: guard repeated the claim. Whichever way the product moves, the code, the docs
#: and the guard have to say the same thing — that is what the guard below is
#: for now.)
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_OLLAMA = "ollama"
VALID_PROVIDERS = (PROVIDER_OPENROUTER, PROVIDER_OLLAMA)


# ---------------------------------------------------------------------------
# AI Provider Configuration
# ---------------------------------------------------------------------------
# Default to Ollama for local-first operation
AI_PROVIDER: str = _env_str("AI_PROVIDER", "ollama")

# Ollama configuration
OLLAMA_BASE_URL: str = _env_str("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_TIMEOUT: float = _env_float("OLLAMA_TIMEOUT", 120.0)

# Ollama model roles (user can override with any valid model tags)
OLLAMA_GENERAL_MODEL: str = _env_str("OLLAMA_GENERAL_MODEL", "qwen3.5:9b")
OLLAMA_CODING_MODEL: str = _env_str("OLLAMA_CODING_MODEL", "qwen2.5-coder:7b")
OLLAMA_FAST_MODEL: str = _env_str("OLLAMA_FAST_MODEL", "qwen2.5-coder:3b")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def configure_logging() -> None:
    """Install a root handler at the configured level. Idempotent.

    Without this, Python's "last resort" handler is WARNING-only, which means
    every *successful* model call went unrecorded. Failures looked fine and the
    happy path was invisible — the opposite of what observability is for.
    """
    import logging

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        root.setLevel(level)

    # httpx logs every request at INFO, which is noise next to our own records.
    if config.LOG_QUIET_THIRD_PARTY:
        for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


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


#: A control failure, raised when no configured model can do the job.
class NoCapableModelError(RuntimeError):
    """No allow-listed free model satisfies the request's capabilities."""

# Model tiers used to build the fallback chain.
TIER_FAST = "fast"
TIER_REASONING = "reasoning"
TIER_FALLBACK = "fallback"


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
        "OPENROUTER_REASONING_MODEL", "cohere/north-mini-code:free"
    )
    #: Retained for compatibility with existing .env files and with
    #: ``model_for_tier(TIER_FALLBACK)``, but it is now just another *named*
    #: member of the explicit free pool. It is no longer an auto-router.
    OPENROUTER_FALLBACK_MODEL: str = _env_str(
        "OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"
    )

    #: The explicit fallback pool, tried in order after the primary. Every entry
    #: is subject to the same two gates as the primary: it must be in a free
    #: form *and* allow-listed. ``openrouter/free`` is refused even here.
    #:
    #: Every id below was verified against OpenRouter's live catalogue. Run
    #: ``python scripts/check_free_models.py`` after editing: a stale id here
    #: fails as a 404, which reads like an outage rather than a config error.
    OPENROUTER_FREE_FALLBACK_MODELS: str = _env_str(
        "OPENROUTER_FREE_FALLBACK_MODELS",
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "nvidia/nemotron-3.5-lightning:free,"
        "cohere/north-mini-code:free,"
        "poolside/laguna-s-2.1:free",
    )

    #: Hard ceiling on how many model calls one logical request may make, across
    #: the whole chain. This is what makes an infinite fallback loop impossible.
    #: Note the distinction from ``LLM_MAX_RETRIES``: that is per-model
    #: transport retries, this is the total across every model.
    OPENROUTER_MAX_ATTEMPTS: int = _env_int("OPENROUTER_MAX_ATTEMPTS", 3)

    #: Minimum seconds to wait on a 429 that advertises no ``Retry-After``.
    #: Deliberately short: with a contended free tier the goal is to *move to a
    #: different free model*, not to sit on one and retry it.
    OPENROUTER_RATE_LIMIT_BACKOFF: float = _env_float(
        "OPENROUTER_RATE_LIMIT_BACKOFF", 0.75
    )

    # OpenRouter attribution headers (required for their dashboard/ranking).
    OPENROUTER_SITE_URL: str = _env_str(
        "OPENROUTER_SITE_URL", "https://github.com/zaytoon-ai"
    )
    OPENROUTER_APP_TITLE: str = _env_str(
        "OPENROUTER_APP_TITLE", "Zaytoon AI Assistant"
    )

    #: Active provider: "ollama" or "openrouter"
    PROVIDER: str = AI_PROVIDER

    # Ollama settings
    OLLAMA_BASE_URL: str = OLLAMA_BASE_URL
    OLLAMA_TIMEOUT: float = OLLAMA_TIMEOUT
    OLLAMA_GENERAL_MODEL: str = OLLAMA_GENERAL_MODEL
    OLLAMA_CODING_MODEL: str = OLLAMA_CODING_MODEL
    OLLAMA_FAST_MODEL: str = OLLAMA_FAST_MODEL

    LLM_TIMEOUT: float = _env_float("LLM_TIMEOUT", 120.0)
    #: Per-model transport retries. ``OPENROUTER_MAX_ATTEMPTS`` is the separate,
    #: global budget across all models.
    LLM_MAX_RETRIES: int = _env_int("LLM_MAX_RETRIES", 1)
    # A model that just rate-limited or 5xx'd is benched for this long instead
    # of being retried on every single request.
    LLM_PROVIDER_COOLDOWN: float = _env_float("LLM_PROVIDER_COOLDOWN", 60.0)
    #: How much longer a 429 benches a model than a 5xx does. 429 is a signal
    #: about *this model right now*, so it deserves a longer rest.
    LLM_RATE_LIMIT_COOLDOWN: float = _env_float("LLM_RATE_LIMIT_COOLDOWN", 90.0)
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

    #: Model calls allowed to run at the same time, process-wide. This is not a
    #: throughput knob: a local runtime getting *more* concurrent generations
    #: gives every one of them a worse answer, or takes the daemon down. Raise it
    #: only on a machine that provably has the memory for another context.
    LLM_MAX_CONCURRENCY: int = _env_int("LLM_MAX_CONCURRENCY", 2)
    #: How long a request waits for a slot before it is refused with an
    #: explanation. Queueing is the normal case (two questions in one second is
    #: ordinary use); refusing is the exception, and the ceiling exists so a wedged
    #: generation cannot hold the whole assistant behind it forever.
    LLM_QUEUE_TIMEOUT_S: float = _env_float("LLM_QUEUE_TIMEOUT_S", 45.0)

    #: Answer a closed set of one-tool requests ("what time is it", "open spotify")
    #: without a model call. Off, every such turn goes through the LLM exactly as
    #: it always did — the path only removes a round trip, so the flag is a
    #: behaviour switch for debugging, not a safety switch: the registry tier and
    #: the confirmation gate are re-checked before anything runs.
    FAST_PATH_ENABLED: bool = _env_bool("FAST_PATH_ENABLED", True)

    #: How stale the cached provider probe in ``services/provider_health`` may
    #: be before ``/health`` re-probes. The UI and the desktop supervisor poll this
    #: endpoint, so a short TTL bounds staleness while keeping the daemon off the
    #: critical path; 0 forces a probe per call.
    HEALTH_PROBE_TTL_S: float = _env_float("HEALTH_PROBE_TTL_S", 5.0)
    #: The probe's own timeout, deliberately nothing like ``OLLAMA_TIMEOUT`` (a
    #: generation may run for minutes; a version handshake must not).
    HEALTH_PROBE_TIMEOUT_S: float = _env_float("HEALTH_PROBE_TIMEOUT_S", 2.0)
    #: Stop tracked background processes (`process_start`) when the backend
    #: shuts down. The registry is in-memory, so leaving them running orphans
    #: them: a dev server still holding its port, invisible to the next session.
    #: Off, and `process_stop` becomes a tool the user must remember to drive.
    PROCESS_STOP_ON_SHUTDOWN: bool = _env_bool("PROCESS_STOP_ON_SHUTDOWN", True)

    #: Also ask ``/api/tags`` and report which tier models are absent. One extra
    #: local GET, and it is what turns "provider unreachable" into the specific
    #: "`ollama pull qwen3.5:9b`" the user can act on.
    HEALTH_PROBE_MODELS: bool = _env_bool("HEALTH_PROBE_MODELS", True)

    @property
    def provider_ready(self) -> bool:
        """True when the *active* provider is configured well enough to try.

        This is a **configuration** predicate, not a reachability one, and the two
        must not be confused: ``/health`` used to treat this as the second, so an
        Ollama install with a stopped daemon read ``status: "ok"`` while the same
        process logged ``AI provider not connected`` two seconds earlier. Reach
        is ``services/provider_health``, which actually asks.
        """
        if self.PROVIDER == PROVIDER_OLLAMA:
            return bool(self.OLLAMA_BASE_URL)
        return bool(self.OPENROUTER_API_KEY)

    def model_for_tier(self, tier: str) -> str:
        """Resolve the OpenRouter model id for a given capability tier."""
        if tier == TIER_REASONING:
            return self.OPENROUTER_REASONING_MODEL
        if tier == TIER_FALLBACK:
            return self.OPENROUTER_FALLBACK_MODEL
        return self.OPENROUTER_PRIMARY_MODEL

    def ollama_model_for_tier(self, tier: str) -> str:
        """Resolve the local Ollama model for a given capability tier.

        ``reasoning``/``fallback`` both map to the coding model: there is no
        separate local reasoning model unless the operator configures one.
        """
        if tier == TIER_FAST:
            return self.OLLAMA_FAST_MODEL
        if tier in (TIER_REASONING, TIER_FALLBACK):
            return self.OLLAMA_CODING_MODEL
        return self.OLLAMA_GENERAL_MODEL

    @property
    def free_fallback_models(self) -> list[str]:
        """The configured fallback pool, in declaration order, de-duplicated."""
        raw = self.OPENROUTER_FREE_FALLBACK_MODELS or ""
        seen: list[str] = []
        for chunk in raw.split(","):
            name = chunk.strip()
            if name and name not in seen:
                seen.append(name)
        return seen

    def model_pool(self, tier: str = TIER_FAST) -> list[str]:
        """Every model this tier may use, most-preferred first.

        The tier's head comes first because it is the deliberate choice, then the
        explicit fallback pool, then whatever the pool still has. The explicit
        list is honoured over the registry's own ordering: an operator who
        wrote down a fallback order meant it.

        No member of this list is ever returned without passing the free-form and
        allow-list gates downstream in ``select_models``.
        """
        ordered: list[str] = []
        for name in (
            self.model_for_tier(tier),
            *self.free_fallback_models,
            self.OPENROUTER_FALLBACK_MODEL,
        ):
            cleaned = (name or "").strip()
            if cleaned and cleaned not in ordered:
                ordered.append(cleaned)
        return ordered

    def validate_free_models(self) -> list[str]:
        """Configured models that violate the free-model policy.

        A model is acceptable only if it is BOTH in the hard-coded allow-list
        AND in a free form (``<vendor>/<model>:free``). An empty list means the
        configuration honours the zero-cost policy.

        The whole pool is checked, not just the three named models, because the
        fallback list is the easiest place for a paid id to be introduced by
        accident.
        """
        checked = {
            self.OPENROUTER_PRIMARY_MODEL,
            self.OPENROUTER_REASONING_MODEL,
            self.OPENROUTER_FALLBACK_MODEL,
            *self.free_fallback_models,
        }
        return sorted(name for name in checked if not is_allowed_model(name))

    @property
    def free_models_enforced(self) -> bool:
        return self.LLM_REQUIRE_FREE_MODELS

    def assert_free_model(self, model: str) -> str:
        """Guard used on the request path. Returns ``model`` if acceptable."""
        if is_free_router(model):
            raise NonFreeModelError(
                f"{model!r} is an uncontrolled auto-router and is not used as a "
                "fallback: it can resolve to a different arbitrary free model on "
                "every call. Configure explicit free models instead."
            )
        if not self.LLM_REQUIRE_FREE_MODELS:
            return model
        if not is_allowed_model(model):
            raise NonFreeModelError(model)
        return model

    def describe_llm(self) -> dict:
        """Small dict for /health and startup logs.

        Never includes the API key, in whole or in part.
        """
        base = {
            "provider": self.PROVIDER,
            "free_only": self.LLM_REQUIRE_FREE_MODELS,
            "configured": self.provider_ready,
        }
        
        if self.PROVIDER == PROVIDER_OLLAMA:
            base.update({
                "base_url": self.OLLAMA_BASE_URL,
                "general_model": self.OLLAMA_GENERAL_MODEL,
                "coding_model": self.OLLAMA_CODING_MODEL,
                "fast_model": self.OLLAMA_FAST_MODEL,
                "timeout": self.OLLAMA_TIMEOUT,
            })
        else:
            base.update({
                "base_url": self.LLM_API_URL,
                "primary_model": self.OPENROUTER_PRIMARY_MODEL,
                "reasoning_model": self.OPENROUTER_REASONING_MODEL,
                "fallback_model": self.OPENROUTER_FALLBACK_MODEL,
                "fallback_pool": self.free_fallback_models,
                "max_attempts": self.OPENROUTER_MAX_ATTEMPTS,
                "allowed_models": sorted(ALLOWED_FREE_MODELS),
                "rejected_models": self.validate_free_models(),
                "excluded_by_policy": [FREE_ROUTER_ENDPOINT],
                "capabilities": {
                    model: spec_for(model).to_dict()
                    for model in sorted(ALLOWED_FREE_MODELS)
                    if spec_for(model)
                },
            })
        return base

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
        # A portable install has a Workspaces/ directory beside Application/ and
        # uses it. A development checkout does not, and keeps the dedicated home
        # folder so the assistant is never granted its own source tree.
        str(_default_workspace_root()),
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
    # Logging
    # ------------------------------------------------------------------
    #: Root log level. This matters for more than tidiness: the per-attempt
    #: record in `LLMClient._log_attempt` logs a *successful* attempt at INFO,
    #: and without an explicit level Python's root handler defaults to WARNING,
    #: so every successful model call was silently dropped. The requirement is
    #: to record every attempt, not only the failures.
    LOG_LEVEL: str = _env_str("LOG_LEVEL", "INFO").upper()

    #: Loggers that are allowed to emit. `Zaytoon.LLMClient` is in the list
    #: specifically so a free-model failover is visible in the console without
    #: turning on third-party debug output.
    LOG_QUIET_THIRD_PARTY: bool = _env_bool("LOG_QUIET_THIRD_PARTY", True)

    # ------------------------------------------------------------------
    # Rate limiting / misc
    # ------------------------------------------------------------------
    # REST API rate limit (per IP)
    RATE_LIMIT_MAX: int = _env_int("RATE_LIMIT_MAX", 120)
    RATE_LIMIT_WINDOW: int = _env_int("RATE_LIMIT_WINDOW", 60)

    # WebSocket rate limit (per session) - more generous for interactive use
    WS_RATE_LIMIT_MAX: int = _env_int("WS_RATE_LIMIT_MAX", 60)
    WS_RATE_LIMIT_WINDOW: int = _env_int("WS_RATE_LIMIT_WINDOW", 60)

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



