import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from aion.settings import Settings


class ProviderUnavailable(Exception):
    pass


@dataclass
class Completion:
    document: dict[str, Any]
    input_tokens: int
    output_tokens: int


async def complete(settings: Settings, messages: list[dict[str, str]]) -> Completion:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not settings.llm_enabled or not settings.llm_model:
        raise ProviderUnavailable("LLM is not configured")
    headers = {}
    if key := settings.llm_api_key.get_secret_value():
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        response = await client.post(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            json={
                "model": settings.llm_model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_completion_tokens": settings.llm_max_output_tokens,
            },
        )
        response.raise_for_status()
        if len(response.content) > 200000:
            raise ValueError("Provider response too large")
        payload = response.json()
        usage = payload.get("usage", {})
        return Completion(
            json.loads(payload["choices"][0]["message"]["content"]),
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )
