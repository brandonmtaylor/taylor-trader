"""Ask a language model for one trading decision, in a fixed JSON shape.

Three connectors cover nearly every model:
- "anthropic": Claude through the official SDK, with structured outputs (the reply is
  guaranteed to match the schema) and server-side fallback if a request is declined.
- "openai": anything that speaks the OpenAI chat API: OpenAI, OpenRouter, Groq, Together,
  and local models through Ollama or LM Studio (point `endpoint` at them; no key needed locally).
- "claude-code": Claude through the Claude Code CLI (`claude -p`), using the subscription it's
  logged in with. No API key; calls count toward that plan's usage limits.

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

PROVIDERS = ("anthropic", "openai", "claude-code")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = {"anthropic": "claude-opus-5-5", "openai": "gpt-5", "claude-code": "sonnet"}
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


class ClaudeCodeLLM:
    """Claude through the Claude Code CLI, on the subscription it's logged in with.

    Each call is a one-shot `claude -p` with no tools, no MCP servers, no settings and no saved
    session, run in an empty folder, so nothing from your own Claude Code setup leaks in and the
    model can't do anything but answer. The CLI validates the reply against SCHEMA.
    """

    SEARCH = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")

    def __init__(self, model: str = "sonnet", effort: str = "medium", timeout: float = 120, binary: str | None = None):
        self.model, self.effort, self.timeout = model, effort, timeout
        self.binary = binary or self.find()

    @classmethod
    def find(cls) -> str:
        """launchd runs the service with a bare PATH, so look in the usual install places too."""
        import os
        import shutil

        found = shutil.which("claude") or next(
            (p for p in map(os.path.expanduser, cls.SEARCH) if os.access(p, os.X_OK)), None)
        if not found:
            raise LLMError("Claude Code isn't installed (no `claude` command). Install it from "
                           "claude.com/claude-code, or pick another AI provider.")
        return found

    def ask(self, system: str, user: str, allowed: tuple[str, ...]) -> Decision:
        import os
        import subprocess
        import tempfile

        cmd = [self.binary, "-p", "--system-prompt", system, "--tools", "", "--strict-mcp-config",
               "--setting-sources", "", "--no-session-persistence", "--output-format", "json",
               "--json-schema", json.dumps(SCHEMA), "--model", self.model, "--effort", self.effort]
        # Use the subscription login: an API key in the environment would take precedence over it.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDECODE")}
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="taylortrader-ai-") as empty:
            try:
                p = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=empty, env=env, check=False,
                                   timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise LLMError(f"Claude Code didn't answer within {self.timeout:g}s") from None
        try:
            out = json.loads(p.stdout)
            if not isinstance(out, dict):
                raise json.JSONDecodeError("not an object", p.stdout, 0)
        except json.JSONDecodeError:
            detail = (p.stderr or p.stdout or "").strip().splitlines()
            hint = detail[-1][:200] if detail else f"exit code {p.returncode}"
            if any(w in hint.lower() for w in ("log in", "login", "/login", "not logged", "authenticat")):
                raise LLMError("Claude Code isn't logged in. Run `claude` in Terminal once and log in.") from None
            raise LLMError(f"Claude Code failed: {hint}") from None
        if out.get("is_error"):
            msg = str(out.get("result") or out.get("subtype") or "unknown error")[:200]
            if any(w in msg.lower() for w in ("limit", "usage", "quota")):
                raise LLMError(f"Claude Code hit your plan's usage limit; will try again later ({msg})")
            raise LLMError(f"Claude Code error: {msg}")
        raw = out.get("structured_output")
        if raw is None:
            raw = _parse_json_text(str(out.get("result") or ""))
        d = validate(raw, allowed)
        d.latency_ms = int((time.monotonic() - t0) * 1000)
        used = out.get("modelUsage") or {}
        d.model = next(iter(used), self.model) + " (Claude Code)"
        u = out.get("usage") or {}
        d.input_tokens = sum(int(u.get(k) or 0) for k in
                             ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")) or None
        d.output_tokens = u.get("output_tokens")
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
    if provider == "claude-code":
        return ClaudeCodeLLM(model or "sonnet", effort, timeout)
    raise LLMError(f"unknown AI provider {provider!r}")
