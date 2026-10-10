"""A truthful, *bounded* answer to "can the model actually answer?".

``GET /health`` used to report ``status: "ok"`` while the server's own startup log
said ``AI provider not connected: Connection failed``. ``config.provider_ready``
answers a configuration question — "is an endpoint or a key set?" — and the
Ollama branch answered it with an unconditional ``True``, so a stopped daemon was
indistinguishable from a healthy one. The desktop supervisor polls this endpoint,
the HUD shows it, and both were being told everything was fine.

Three constraints shaped this module, and each one is the reason it is not simply
``await provider.health_check()`` inside the route:

* **Bounded.** ``OllamaClient`` carries a 120 s request timeout because a *generation*
  may legitimately take minutes; a health probe may not. A hung daemon would have
  held every ``/health`` caller — including the desktop's "is it alive?" check —
  for two minutes. The probe gets its own short timeout, and the whole thing is
  wrapped so even a bug in the transport cannot exceed it.
* **Cached.** The UI polls, and several clients poll at once. Re-probing on every
  request turns a status page into a load generator against a local daemon that is
  already the bottleneck. ``HEALTH_PROBE_TTL_S`` says how stale an answer may be.
* **Single-flight.** Six simultaneous polls must produce one probe, not six — and
  they must not stampede the daemon the moment the cache expires.

For OpenRouter there is deliberately **no synthetic request**: a metered API is
not something to ping on a timer, and a key-validity call would be a network
dependency inside the endpoint that tells you about network failures. That
provider's verdict comes from what real traffic already showed — the client's
circuit-breaker state and its counters — and the response says so in ``source``.
``never_probed`` is an honest answer; ``ok`` when nothing has been tried is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from config import PROVIDER_OLLAMA, config

logger = logging.getLogger("Zaytoon.Services.ProviderHealth")

#: Verdicts, so a caller can branch on a word rather than on a sentence.
REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNCONFIGURED = "unconfigured"
UNKNOWN = "never_probed"


class ProviderHealth:
    """A cached probe of the active provider. One instance per process."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._result: Optional[Dict[str, Any]] = None
        self._at: float = 0.0
        #: The endpoint string the cached verdict describes. A config change to
        #: ``OLLAMA_BASE_URL`` must not be reported against the old answer.
        self._for_endpoint: str = ""

    def _fresh(self) -> bool:
        return (
            self._result is not None
            and (time.monotonic() - self._at) < config.HEALTH_PROBE_TTL_S
            and self._for_endpoint == self._endpoint()
        )

    @staticmethod
    def _endpoint() -> str:
        return (
            config.OLLAMA_BASE_URL
            if config.PROVIDER == PROVIDER_OLLAMA
            else config.OPENROUTER_BASE_URL
        )

    async def status(self, force: bool = False) -> Dict[str, Any]:
        """The current verdict. Probes only when the cache is stale.

        Never raises: this feeds a health endpoint whose whole job is to report
        failure, and an exception here would replace a useful ``unreachable``
        with a 500 that says nothing about why.
        """
        if self._fresh() and not force:
            return {**self._result, "cached": True}  # type: ignore[misc]

        async with self._lock:
            # Re-check inside the lock: the first waiter in a stampede already
            # produced the answer the others were waiting for.
            if self._fresh() and not force:
                return {**self._result, "cached": True}  # type: ignore[misc]
            started = time.monotonic()
            try:
                # The bound is *ours*, not the transport's: httpx is asked for
                # HEALTH_PROBE_TIMEOUT_S, but a client that ignored it (or a
                # non-HTTP provider added later) would still not get to decide
                # how long `/health` blocks. The outer budget is a little larger
                # so the transport normally wins and reports the precise error.
                verdict = await asyncio.wait_for(
                    self._probe(),
                    timeout=config.HEALTH_PROBE_TIMEOUT_S + 0.5,
                )
            except asyncio.TimeoutError:
                verdict = {
                    "state": UNREACHABLE,
                    "error": (
                        "the probe did not return within "
                        f"{config.HEALTH_PROBE_TIMEOUT_S + 0.5:.1f}s"
                    ),
                    "missing_models": [],
                    "source": "probe",
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - the probe is defensive
                logger.debug("health probe failed", exc_info=True)
                verdict = {
                    "state": UNREACHABLE,
                    "error": f"{type(exc).__name__}: {exc}",
                    "missing_models": [],
                }
            elapsed = int((time.monotonic() - started) * 1000)
            self._result = {
                "provider": config.PROVIDER,
                "endpoint": self._endpoint(),
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "age_s": 0.0,
                "latency_ms": elapsed,
                # Overridden below whenever the answer did not come from asking.
                "source": "probe",
                **verdict,
            }
            self._at = started
            self._for_endpoint = self._endpoint()
            return {**self._result, "cached": False}

    async def _probe(self) -> Dict[str, Any]:
        if config.PROVIDER != PROVIDER_OLLAMA:
            return self._observed_verdict()

        url = f"{config.OLLAMA_BASE_URL}/api/version"
        try:
            async with httpx.AsyncClient(
                timeout=config.HEALTH_PROBE_TIMEOUT_S
            ) as client:
                version_resp = await client.get(url)
                version_resp.raise_for_status()
                version = str(version_resp.json().get("version", "unknown"))
                tags: Dict[str, Any] = {}
                if config.HEALTH_PROBE_MODELS:
                    tags_resp = await client.get(
                        f"{config.OLLAMA_BASE_URL}/api/tags"
                    )
                    tags_resp.raise_for_status()
                    tags = tags_resp.json() or {}
        except (httpx.HTTPError, ValueError) as exc:
            # `httpx.HTTPError` is the base of ConnectError/TimeoutException/
            # HTTPStatusError, so a refused socket, a hung daemon and a 500 all
            # land here with the distinction preserved in the type name.
            return {
                "state": UNREACHABLE,
                "error": f"{type(exc).__name__}: {exc}",
                "missing_models": [],
            }

        installed = {
            str(m.get("name", "")) for m in (tags.get("models") or [])
        }
        missing = []
        if config.HEALTH_PROBE_MODELS:
            wanted = {
                config.ollama_model_for_tier(tier)
                for tier in ("general", "reasoning", "fast")
            }
            missing = sorted(
                model
                for model in wanted
                # `qwen3.5:9b` and `qwen3.5:9b` match exactly; ollama reports
                # `:latest` for a bare name, so accept that spelling too.
                if model not in installed and f"{model}:latest" not in installed
            )
        return {
            "state": REACHABLE,
            "version": version,
            "installed_models": len(installed) if installed else None,
            "missing_models": missing,
            "error": "",
        }

    @staticmethod
    def _observed_verdict() -> Dict[str, Any]:
        """Cloud path: judge from real traffic, without adding a request."""
        if not config.llm_configured:
            return {
                "state": UNCONFIGURED,
                "error": "OPENROUTER_API_KEY is not set",
                "missing_models": [],
                # Neither a probe nor an observation: this was read from the
                # environment, which is exactly why it cannot prove reachability.
                "source": "config",
            }
        try:
            from services.llm_client import get_llm_client

            client = get_llm_client()
            health = client.health_snapshot()
            metrics = client.metrics.to_dict()
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "state": UNKNOWN,
                "error": f"observations unavailable: {type(exc).__name__}",
                "missing_models": [],
                "source": "observations",
            }

        blocked = [m for m, h in health.items() if h.get("open")]
        requests = int(metrics.get("requests") or 0)
        successes = int(metrics.get("success") or 0)
        if requests == 0 or (successes == 0 and not blocked):
            state, error = UNKNOWN, "no request has been made yet"
        elif blocked and successes == 0:
            state, error = UNREACHABLE, f"every model benched: {', '.join(blocked)}"
        elif blocked:
            state, error = REACHABLE, f"{len(blocked)} model(s) benched, others serving"
        else:
            state, error = REACHABLE, ""
        return {
            "state": state,
            "error": error,
            "missing_models": [],
            "benched_models": blocked,
            "source": "observations",
        }

    def cached(self) -> Dict[str, Any]:
        """The last verdict without probing. For callers that must not await."""
        if self._result is None:
            return {"state": UNKNOWN, "error": "no probe has run", "missing_models": []}
        age = time.monotonic() - self._at
        return {**self._result, "age_s": round(age, 3), "cached": True,
                "stale": age >= config.HEALTH_PROBE_TTL_S}

    def invalidate(self) -> None:
        """Drop the cache. Call after a settings change so /health re-reads."""
        self._result = None
        self._at = 0.0


#: The process-wide prober. A cache only earns its keep if it is shared; two
#: instances would halve the effective TTL rather than bound the load.
provider_health = ProviderHealth()


__all__ = ["ProviderHealth", "REACHABLE", "UNREACHABLE", "UNCONFIGURED",
           "UNKNOWN", "provider_health"]
