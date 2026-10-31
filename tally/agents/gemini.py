"""Thin wrapper over the google-genai Interactions API.

Every Tally agent call goes through ``ask_json``: content in, schema-checked
JSON out. No sampling parameters are sent (``temperature``, ``top_p`` and
friends break ``gemini-4-argon``); Argon calls set ``thinking_level`` only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

ROUTER = "gemini-3.5-flash-lite"
READER = "gemini-3.8-flash"
ARGON = "gemini-4-argon"

# Background runs are polled while queued or running (builder guide, section 2).
# Anything else, including requires_action, is final for Tally's purposes.
RUNNING = {"in_progress", "queued"}
POLL_SECONDS = 5.0  # documented interval
MAX_PARALLEL = 12
TIMEOUT_MS = {ROUTER: 60_000, READER: 180_000, ARGON: 900_000}

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
DOCUMENT_TYPES = {"application/pdf", "text/csv"}


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class FilePart:
    """A document to show the model, sent inline as base64."""

    data: bytes
    mime_type: str
    name: str = ""

    @classmethod
    def from_path(cls, path: Path) -> FilePart:
        return cls(path.read_bytes(), guess_mime(path), path.name)

    def content(self) -> dict[str, Any]:
        kind = "image" if self.mime_type in IMAGE_TYPES else "document"
        if self.mime_type not in IMAGE_TYPES | DOCUMENT_TYPES:
            raise AgentError(f"{self.name}: can't send {self.mime_type} to the model")
        return {"type": kind, "data": base64.b64encode(self.data).decode(), "mime_type": self.mime_type}


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if path.suffix.lower() in {".heic", ".heif"}:
        return f"image/{path.suffix.lower()[1:]}"
    return mime or "application/octet-stream"


class Asker(Protocol):
    """What the agents need from a model. Tests pass a fake."""

    async def __call__(
        self,
        model: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        files: list[FilePart] = ...,
        system: str | None = ...,
        thinking: str | None = ...,
        background: bool = ...,
    ) -> Any: ...


class Gemini:
    def __init__(self, api_key: str | None = None):
        load_dotenv()
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_KEY")
        if not key:
            raise AgentError("Set GEMINI_KEY (or GEMINI_API_KEY) in .env")
        from google import genai

        self._clients = {
            model: genai.Client(api_key=key, http_options={"timeout": ms})
            for model, ms in TIMEOUT_MS.items()
        }
        self._slots = asyncio.Semaphore(MAX_PARALLEL)

    async def __call__(
        self,
        model: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        files: list[FilePart] = (),
        system: str | None = None,
        thinking: str | None = None,
        background: bool = False,
    ) -> Any:
        body: dict[str, Any] = {
            "model": model,
            "input": [{"type": "text", "text": prompt}] + [f.content() for f in files],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": schema},
            "response_mime_type": "application/json",
        }
        if system:
            body["system_instruction"] = system
        if thinking:
            body["generation_config"] = {"thinking_level": thinking}
        if background:
            body["background"] = True  # requires store=True, the default
        else:
            body["store"] = False  # don't keep customers' tickets on the server

        api = self._clients.get(model, self._clients[READER]).aio.interactions
        async with self._slots:
            interaction = await api.create(**body)
        while interaction.status in RUNNING:
            await asyncio.sleep(POLL_SECONDS)
            interaction = await api.get(interaction.id)
        if interaction.status != "completed":
            raise AgentError(f"{model} interaction {interaction.id} ended {interaction.status}")
        try:
            return json.loads(interaction.output_text or "")
        except json.JSONDecodeError as e:
            raise AgentError(f"{model} returned invalid JSON: {e}") from e


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """JSON-schema object helper; every property is required unless listed otherwise."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def nullable(type_: str, **extra: Any) -> dict[str, Any]:
    return {"type": [type_, "null"], **extra}
