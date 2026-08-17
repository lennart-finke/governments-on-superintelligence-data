"""LLM client: OpenRouter chat-completions. Thread-safe; model IDs pinned in
config/tiers.yaml and recorded per adjudication."""

from __future__ import annotations

import json
import random
import time

import httpx

from .. import config

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMClient:
    """OpenRouter client for the two adjudication roles.

    `bulk` is the judge that decides every candidate; it resolves to one of the
    two interchangeable judges in tiers.yaml (`gemini` or `glm`), chosen at
    construction. `confirm` is the opt-in second judge. Any judge-config key other
    than `model` (e.g. `provider` routing for GLM->Novita fp8, or `reasoning` to
    disable GLM's thinking) is attached verbatim to every request for that model.
    """

    provider = "openrouter"

    def __init__(self, judge: str | None = None):
        tiers = config.load_yaml("tiers.yaml")["openrouter"]
        judges = tiers["judges"]
        name = judge or tiers.get("default_judge", "gemini")
        if name not in judges:
            raise ValueError(f"unknown judge {name!r}; known: {', '.join(sorted(judges))}")
        self.judge = name
        judge_cfg = judges[name]
        # tier -> model id; bulk is the selected judge, confirm the second judge
        self.models = {"bulk": judge_cfg["model"], "confirm": tiers["confirm"]}
        # model id -> extra request-body params (provider routing, reasoning
        # control, ...): every judge-config key except `model`, merged verbatim
        self._request_extra = {
            judge_cfg["model"]: {k: v for k, v in judge_cfg.items() if k != "model"}
        }
        if not config.openrouter_api_key():
            raise RuntimeError("OPENROUTER_API_KEY is not set (.env)")
        self._http = httpx.Client(
            timeout=180,
            limits=httpx.Limits(max_connections=600, max_keepalive_connections=64),
        )

    def model_for(self, tier: str) -> str:
        return self.models[tier]

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        retries: int = 6,
    ) -> str:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # provider routing / reasoning control / any other per-model request params
        body.update(self._request_extra.get(model) or {})
        for attempt in range(retries):
            try:
                resp = self._http.post(
                    OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {config.openrouter_api_key()}"},
                    json=body,
                )
                if resp.status_code in (429, 500, 502, 503):
                    raise ConnectionError(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    # 402/403 = credits/key limit: surface as ConnectionError so
                    # callers treat it as transport failure (candidate stays
                    # pending), not an unhandled crash
                    raise ConnectionError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                if "error" in data:
                    raise ConnectionError(str(data["error"])[:200])
                content = data["choices"][0]["message"]["content"]
                if not content:
                    # e.g. a reasoning model that spent the whole budget thinking
                    # (finish_reason=length, content=null). Retry rather than hand
                    # None to the parser; leaves the candidate resumable if it sticks.
                    fr = data["choices"][0].get("finish_reason")
                    raise ConnectionError(f"empty content (finish_reason={fr})")
                return content
            except (ConnectionError, httpx.HTTPError):
                if attempt == retries - 1:
                    raise
                # jittered backoff — 429 storms are expected at high concurrency
                time.sleep(min(60, 2**attempt * 2) * (0.5 + random.random()))
        raise RuntimeError("unreachable")


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model response (tolerates code fences/preamble)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    return json.loads(text[start : end + 1])
