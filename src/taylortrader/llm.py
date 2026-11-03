"""Ask a language model for one trading decision, in a fixed JSON shape.

Two connectors cover nearly every model:
- "anthropic": Claude through the official SDK, with structured outputs (the reply is
  guaranteed to match the schema) and server-side fallback if a request is declined.
- "openai": anything that speaks the OpenAI chat API: OpenAI, OpenRouter, Groq, Together,
  and local models through Ollama or LM Studio (point `endpoint` at them; no key needed locally).

Whatever comes back is validated again here. A malformed or off-menu answer is an error,
never a trade.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = {"anthropic": "claude-opus-5-5", "openai": "gpt-5"}
# Models that accept fallbacks: "default" (server-side retry on another model after a decline).
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
        "confidence": {"type": "number", "description": "0 to 1"},
        "summary": {"type": "string", "description": "one sentence"},
        "pros": {"type": "array", "items": {"type": "string"}},
        "cons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "confidence", "summary", "pros", "cons"],
    "additionalProperties": False,
}


class LLMError(RuntimeError):
    pass


@dataclass
class Decision:
    action: str
    confidence: float
    summary: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0


def validate(raw: object, allowed: tuple[str, ...]) -> Decision:
    """Turn whatever the model sent into a Decision, or raise. Never guesses an action."""
    if not isinstance(raw, dict):
        raise LLMError("the model's answer wasn't a JSON object")
    action = str(raw.get("action", "")).strip().lower()
    if action not in allowed:
        raise LLMError(f"the model answered {action!r}; only {' or '.join(allowed)} make sense here")
    try:
        conf = float(raw.get("confidence"))
    except (TypeError, ValueError):
        raise LLMError("the model's confidence wasn't a number") from None
    if conf > 1 and conf <= 100:  # some models answer in percent
        conf /= 100
    conf = min(1.0, max(0.0, conf))

    def strings(key: str) -> list[str]:
        v = raw.get(key) or []
        return [str(x)[:400] for x in v if str(x).strip()][:8] if isinstance(v, list) else [str(v)[:400]]

    return Decision(action, conf, str(raw.get("summary", "")).strip()[:400], strings("pros"), strings("cons"))


def _parse_json_text(text: str) -> object:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise LLMError("the model's answer wasn't valid JSON") from None


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None, effort: str = "medium", timeout: float = 120):
        import anthropic

        self._anthropic = anthropic
        # No key given: the SDK falls back to ANTHROPIC_API_KEY or an `ant auth login` profile.
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2) if api_key \
            else anthropic.Anthropic(timeout=timeout, max_retries=2)
        self.model, self.effort = model, effort

    def ask(self, system: str, user: str, allowed: tuple[str, ...]) -> Decision:
        a = self._anthropic
        kwargs = {}
        if self.model in _FALLBACK_MODELS:
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        t0 = time.monotonic()
        try:
            r = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                **kwargs,
            )
        except a.AuthenticationError:
            raise LLMError("Anthropic didn't accept the API key (Settings → AI)") from None
        except a.NotFoundError:
            raise LLMError(f"Anthropic doesn't know the model {self.model!r}") from None
        except a.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request: {e.message}") from None
        except a.RateLimitError:
            raise LLMError("Anthropic rate limit hit; will try again on a later bar") from None
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic error {e.status_code}: {e.message}") from None
        except a.APIConnectionError:
            raise LLMError("couldn't reach Anthropic (network)") from None
        except TypeError as e:
            if "authentication" in str(e).lower():
                raise LLMError("No Anthropic API key found. Add one in Settings → AI.") from None
            raise
        if r.stop_reason == "refusal":
            cat = getattr(r.stop_details, "category", None) if r.stop_details else None
            raise LLMError(f"the model declined to answer ({cat or 'no category'})")
        if r.stop_reason == "max_tokens":
            raise LLMError("the model ran out of room before answering")
        text = next((b.text for b in r.content if b.type == "text"), "")
        d = validate(_parse_json_text(text), allowed)
        d.model, d.latency_ms = r.model, int((time.monotonic() - t0) * 1000)
        d.input_tokens, d.output_tokens = r.usage.input_tokens, r.usage.output_tokens
        return d


class OpenAICompatLLM:
    def __init__(self, model: str, api_key: str | None, endpoint: str = "", timeout: float = 120):
        import openai

        self._openai = openai
        self.client = openai.OpenAI(api_key=api_key or "not-needed", base_url=endpoint or None,
                                    timeout=timeout, max_retries=2)
        self.model = model

    def _call(self, messages, response_format):
        return self.client.chat.completions.create(model=self.model, messages=messages,
                                                   response_format=response_format)

    def ask(self, system: str, user: str, allowed: tuple[str, ...]) -> Decision:
        o = self._openai
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        strict = {"type": "json_schema", "json_schema": {"name": "decision", "schema": SCHEMA, "strict": True}}
        t0 = time.monotonic()
        try:
            try:
                r = self._call(messages, strict)
            except o.BadRequestError:
                # Some OpenAI-compatible servers only support plain JSON mode: describe the shape instead.
                hint = ("\n\nReply with only a JSON object with keys action, confidence (0-1), summary, "
                        f"pros (list), cons (list). action must be one of: {', '.join(allowed)}.")
                messages[1]["content"] += hint
                r = self._call(messages, {"type": "json_object"})
        except o.AuthenticationError:
            raise LLMError("the AI provider didn't accept the API key (Settings → AI)") from None
        except o.NotFoundError:
            raise LLMError(f"the AI provider doesn't know the model {self.model!r}") from None
        except o.RateLimitError:
            raise LLMError("AI provider rate limit hit; will try again on a later bar") from None
        except o.APIStatusError as e:
            raise LLMError(f"AI provider error {e.status_code}: {str(e)[:200]}") from None
        except o.APIConnectionError:
            raise LLMError("couldn't reach the AI provider (network, or is the local server running?)") from None
        choice = r.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMError("the model declined to answer")
        d = validate(_parse_json_text(choice.message.content or ""), allowed)
        d.model, d.latency_ms = r.model or self.model, int((time.monotonic() - t0) * 1000)
        if r.usage:
            d.input_tokens, d.output_tokens = r.usage.prompt_tokens, r.usage.completion_tokens
        return d


def build(provider: str, model: str, endpoint: str, effort: str, timeout: float, dotenv_path=None):
    """The configured model client. Keys come from the Keychain/.env/environment, never config."""
    from taylortrader.secrets import get_secret

    if provider == "anthropic":
        key = get_secret("ANTHROPIC_API_KEY", dotenv_path)
        return AnthropicLLM(model, key.get_secret_value() if key else None, effort, timeout)
    if provider == "openai":
        key = get_secret("OPENAI_API_KEY", dotenv_path)
        if key is None and not endpoint:
            raise LLMError("Add an OpenAI API key, or set an endpoint for a local model (Settings → AI)")
        return OpenAICompatLLM(model, key.get_secret_value() if key else None, endpoint, timeout)
    raise LLMError(f"unknown AI provider {provider!r}")
