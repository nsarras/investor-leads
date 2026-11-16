"""Thin wrapper around the Anthropic API that returns schema-validated JSON."""

from __future__ import annotations

import json
import threading
from typing import Any

# Upper bound for one reply, including the model's own reasoning.
MAX_OUTPUT_TOKENS = 64000


class LLMError(RuntimeError):
    """Raised when the model call fails or does not return usable JSON."""


class AnthropicJSON:
    """Calls Claude with a JSON schema and returns the parsed object.

    Uses structured outputs (`output_config.format`), so the reply is guaranteed to match
    the schema. Sampling parameters such as temperature are left unset because current
    Claude models reject non-default values. Requests are streamed, which the SDK requires
    for large token limits.
    """

    def __init__(self, api_key: str, model: str, client: Any | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self._client = client
        self.model = model
        # Running totals for the cost summary. Calls may come from several threads.
        self.call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()

    def json(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8000) -> dict[str, Any]:
        """Return the model's JSON reply. If the reply is cut off, retry once with twice the room."""
        limit = min(max_tokens, MAX_OUTPUT_TOKENS)
        response = self._call(system, user, schema, limit)
        if getattr(response, "stop_reason", None) == "max_tokens" and limit < MAX_OUTPUT_TOKENS:
            limit = min(limit * 2, MAX_OUTPUT_TOKENS)
            response = self._call(system, user, schema, limit)

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            raise LLMError(f"The model's reply was cut off at {limit} tokens. Ask for fewer items per call.")
        if stop_reason == "refusal":
            raise LLMError("Model stopped early (stop_reason=refusal).")

        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
        if not text:
            raise LLMError("Model returned no text content.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMError("Model returned JSON that is not an object.")
        return parsed

    def _call(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> Any:
        try:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            ) as stream:
                response = stream.get_final_message()
        except Exception as exc:  # anthropic raises several error types; report them uniformly
            raise LLMError(f"Anthropic API call failed: {exc}") from exc
        usage = getattr(response, "usage", None)
        with self._lock:
            self.call_count += 1
            for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                self.input_tokens += getattr(usage, name, 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return response
