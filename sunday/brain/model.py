"""Provider-agnostic chat model with a scripted fake for tests and the simulator."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from sunday.config import Settings
from sunday.logging_setup import get_logger

log = get_logger("brain.model")
T = TypeVar("T", bound=BaseModel)


@dataclass
class Message:
    role: str
    content: str


@dataclass
class Reply:
    text: str
    model: str


class ChatModel(Protocol):
    name: str

    def chat(
        self, messages: Sequence[Message], *, temperature: float = 0.6, max_tokens: int = 300
    ) -> Reply: ...


class ModelError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenAIChat:
    def __init__(self, api_key: str, model: str, *, timeout_s: float = 20.0) -> None:
        import openai

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout_s, max_retries=2)
        self.name = f"openai:{model}"
        self._model = model

    def chat(
        self, messages: Sequence[Message], *, temperature: float = 0.6, max_tokens: int = 300
    ) -> Reply:
        try:
            r = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except (
            self._openai.AuthenticationError,
            self._openai.PermissionDeniedError,
            self._openai.BadRequestError,
        ) as exc:
            raise ModelError(f"OpenAI rejected the request: {exc}", retryable=False) from exc
        except self._openai.OpenAIError as exc:
            raise ModelError(f"OpenAI error: {exc}", retryable=True) from exc
        return Reply(text=(r.choices[0].message.content or "").strip(), model=self.name)


@dataclass
class ScriptedChat:
    """Replays a script; steps may be strings, exceptions, or callables over the messages."""

    script: list[Any]
    name: str = "scripted"
    calls: list[list[Message]] = field(default_factory=list)

    def chat(
        self, messages: Sequence[Message], *, temperature: float = 0.6, max_tokens: int = 300
    ) -> Reply:
        self.calls.append(list(messages))
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return Reply(text=step(list(messages)) if callable(step) else str(step), model=self.name)


def build_model(settings: Settings) -> ChatModel:
    if settings.provider == "openai":
        return OpenAIChat(
            settings.require_openai(), settings.openai_model, timeout_s=settings.model_timeout_s
        )
    return ScriptedChat(script=["(fake model)"])


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> str:
    cleaned = _FENCE.sub("", text.strip())
    start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON found in model reply")
    depth, in_str, esc = 0, False, False
    closer = "}" if cleaned[start] == "{" else "]"
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0 and ch == closer:
                return cleaned[start : i + 1]
    raise ValueError("unterminated JSON in model reply")


def complete_json(
    model: ChatModel,
    messages: Sequence[Message],
    schema: type[T],
    *,
    temperature: float = 0.2,
    max_tokens: int = 900,
) -> T:
    """Schema-valid JSON with one repair attempt. Schema goes in the system turn."""
    convo = list(messages)
    instruction = (
        "Reply with ONLY a JSON object matching this JSON Schema, no prose:\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    if convo and convo[0].role == "system":
        convo[0] = Message("system", f"{convo[0].content}\n\n{instruction}")
    else:
        convo.insert(0, Message("system", instruction))
    last_error = ""
    for attempt in (1, 2):
        reply = model.chat(convo, temperature=temperature, max_tokens=max_tokens)
        try:
            return schema.model_validate_json(extract_json(reply.text))
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)[:500]
            log.warning("model JSON invalid (attempt %d): %s", attempt, last_error[:120])
            convo = [
                *convo,
                Message("assistant", reply.text),
                Message("user", f"That was not valid: {last_error}\nReturn ONLY corrected JSON."),
            ]
    raise ModelError(f"model returned invalid JSON twice: {last_error}", retryable=False)
