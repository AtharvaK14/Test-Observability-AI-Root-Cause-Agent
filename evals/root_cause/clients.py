"""Model clients for the eval: live (recording) and replay.

Both plug into ``RootCauseAnalysisAgent(client=...)``, the agent's own seam, so
the eval drives the production agent loop rather than a re-implementation.

- ``RecordingClient`` wraps the real SDK client. It owns retries (jittered
  backoff, attempt count recorded), asserts the model that served each call is
  the one requested, and keeps every request/response pair. Those pairs are the
  transcript and the cassette.
- ``ReplayClient`` serves a cassette back in order. Replaying a recorded live
  run offline re-exercises everything after the model call — tool dispatch,
  verdict parsing, persistence, grading — for free and deterministically, which
  is what the CI replay job checks. It is not a fresh measurement of the model.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

import anthropic
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

# The SDK defers building response-model schemas until first use. Cases run
# under freezegun, which swaps the datetime class; a schema first built inside
# a frozen clock cannot recognise datetime fields. Build them now, at import.
Message.model_rebuild(force=True)
BetaMessage.model_rebuild(force=True)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class ServedModelMismatchError(RuntimeError):
    """A response came from a different model than requested, with no fallback recorded."""


class ReplayError(RuntimeError):
    """The agent asked for a call the cassette does not have."""


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    return value


def _has_fallback(response: dict[str, Any]) -> bool:
    if any(block.get("type") == "fallback" for block in response.get("content", [])):
        return True
    iterations = (response.get("usage") or {}).get("iterations") or []
    return any(it.get("type") == "fallback_message" for it in iterations)


class _Messages:
    def __init__(self, owner: RecordingClient | ReplayClient, kind: str) -> None:
        self._owner = owner
        self._kind = kind

    def create(self, **params: Any) -> Any:
        return self._owner.call(self._kind, params)


class _Beta:
    def __init__(self, owner: RecordingClient | ReplayClient) -> None:
        self.messages = _Messages(owner, "beta")


class RecordingClient:
    """Live calls through the SDK, with retries and a full record of each call."""

    def __init__(
        self,
        sdk_client: Any,
        max_attempts: int = 5,
        base_delay_s: float = 2.0,
        max_delay_s: float = 60.0,
    ) -> None:
        self._sdk = sdk_client
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.messages = _Messages(self, "messages")
        self.beta = _Beta(self)
        self.calls: list[dict[str, Any]] = []
        self.retries = 0
        self.model_mismatch: str | None = None

    @classmethod
    def from_environment(cls, timeout_s: float) -> RecordingClient:
        # Credentials resolve the SDK's usual way (ANTHROPIC_API_KEY or an
        # `ant auth login` profile); this code never reads or stores a key.
        # SDK retries off: this wrapper retries so the count is observable.
        return cls(anthropic.Anthropic(max_retries=0, timeout=timeout_s))

    def call(self, kind: str, params: dict[str, Any]) -> Any:
        target = self._sdk.beta.messages if kind == "beta" else self._sdk.messages
        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                response = target.create(**params)
                break
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                retryable = status is None or status in RETRYABLE_STATUS
                if not retryable or attempt >= self.max_attempts:
                    raise
                self.retries += 1
                delay = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
                time.sleep(delay * random.uniform(0.5, 1.0))  # noqa: S311 — jitter, not crypto
        latency_s = time.perf_counter() - started

        record = {
            "kind": kind,
            "request": to_jsonable({k: v for k, v in params.items() if k != "betas"}),
            "betas": params.get("betas"),
            "response": to_jsonable(response),
            "latency_s": round(latency_s, 3),
            "attempts": attempt,
        }
        self.calls.append(record)

        served = record["response"].get("model", "")
        requested = params.get("model", "")
        mismatched = served and requested and not served.startswith(requested)
        if mismatched and not _has_fallback(record["response"]):
            self.model_mismatch = f"requested {requested}, served {served}"
            raise ServedModelMismatchError(self.model_mismatch)
        return response


class ReplayClient:
    """Serves recorded responses in order; flags requests that drifted."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls
        self._next = 0
        self.messages = _Messages(self, "messages")
        self.beta = _Beta(self)
        self.calls: list[dict[str, Any]] = []
        self.retries = 0
        self.model_mismatch: str | None = None
        self.request_drift = 0

    def call(self, kind: str, params: dict[str, Any]) -> Any:
        if self._next >= len(self._calls):
            raise ReplayError(f"cassette exhausted after {len(self._calls)} calls")
        recorded = self._calls[self._next]
        self._next += 1
        if recorded["kind"] != kind:
            raise ReplayError(f"call {self._next}: recorded {recorded['kind']}, asked {kind}")

        request = to_jsonable({k: v for k, v in params.items() if k != "betas"})
        # Tool results carry timestamps and ids from the rebuilt world; a drift
        # here means the harness or world changed since recording, which the
        # replay job reports rather than hides.
        if json.dumps(request, sort_keys=True) != json.dumps(recorded["request"], sort_keys=True):
            self.request_drift += 1
        self.calls.append({**recorded, "request": request})
        return BetaMessage.model_validate(recorded["response"])


# --- Transcript ------------------------------------------------------------


def _render_blocks(content: Any, turns: list[dict[str, Any]], role: str) -> None:
    if isinstance(content, str):
        turns.append({"role": role, "content": content})
        return
    pending_thinking: str | None = None
    for block in content:
        btype = block.get("type")
        if btype == "thinking":
            pending_thinking = (pending_thinking or "") + (block.get("thinking") or "")
        elif btype == "text" and block.get("text"):
            turn = {"role": role, "content": block["text"]}
            if pending_thinking:
                turn["thinking"], pending_thinking = pending_thinking, None
            turns.append(turn)
        elif btype == "tool_use":
            turn = {
                "role": "tool_call",
                "name": block.get("name", ""),
                "content": json.dumps(block.get("input", {}), indent=2),
            }
            if pending_thinking:
                turn["thinking"], pending_thinking = pending_thinking, None
            turns.append(turn)
        elif btype == "tool_result":
            body = block.get("content")
            if not isinstance(body, str):
                body = json.dumps(body, indent=2)
            turns.append({"role": "tool_result", "content": body})


def transcript_from_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The full conversation of one agent run, in the report's trace format."""
    if not calls:
        return []
    last = calls[-1]
    turns: list[dict[str, Any]] = []
    system = last["request"].get("system")
    if system:
        turns.append({"role": "system", "content": system if isinstance(system, str)
                      else json.dumps(system)})
    for message in last["request"].get("messages", []):
        _render_blocks(message["content"], turns, message["role"])
    _render_blocks(last["response"].get("content", []), turns, "assistant")
    return turns
