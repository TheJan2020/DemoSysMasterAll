"""
Provider-agnostic vision analyzer for the Kitchen AI-Camera + AI-Rules
features. Both call `analyze()` with raw JPEG bytes + a system prompt
+ a user prompt and get back a normalized result, regardless of which
provider sits behind the model:

    provider = "gemini"   → google-genai SDK, generate_content with
                            response_schema → text (JSON or free-form)
    provider = "deepseek" → OpenAI-compatible POST to
                            <base>/v1/chat/completions with the image
                            inlined as a data URI, response_format =
                            json_object when a schema is requested

The return shape is always:

    {"ok": bool, "text": str, "error": Optional[str], "latency_ms": int}

Callers do their own JSON parsing on `text` (we don't enforce a schema
here — both the AI-Camera analyze endpoint and the rules engine want
slightly different shapes, and both already handle bad JSON defensively).
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any, Optional

import httpx

from ...core.state import state

logger = logging.getLogger("demo_restaurant.kitchen_vision")


# DeepSeek's hosted API (api.deepseek.com) currently serves these models:
#   - `deepseek-v4-flash` (current)
#   - `deepseek-v4-pro`   (current)
#   - `deepseek-chat`     (deprecated — retires 2026/07/24)
#   - `deepseek-reasoner` (deprecated — retires 2026/07/24)
#
# The official feature matrix for v4-flash / v4-pro lists JSON Output,
# Tool Calls, Chat Prefix Completion, and FIM Completion — NO vision.
# Image input is rejected by the chat schema at parse time with a 400
# "unknown variant `image_url`, expected `text`".
#
# DeepSeek's web product (chat.deepseek.com) does support image analysis
# but is powered by their open-source DeepSeek-VL2 / Janus-Pro weights,
# which are NOT exposed through api.deepseek.com. To get DeepSeek vision
# via API you'd self-host the open weights (DeepInfra, Hugging Face TGI,
# vLLM, etc.).
#
# `vision: False` keeps these out of vision dropdowns automatically until
# DeepSeek ships a hosted VL endpoint.
DEEPSEEK_MODELS: list[dict] = [
    {"name": "deepseek-v4-flash",
      "display": "DeepSeek V4 Flash", "vision": False},
    {"name": "deepseek-v4-pro",
      "display": "DeepSeek V4 Pro",   "vision": False},
]

# Human-readable note surfaced by `list_vision_models()` when DeepSeek is
# configured. The SPA shows it inline so the operator understands the
# gap between chat.deepseek.com (which has vision) and api.deepseek.com
# (which doesn't).
DEEPSEEK_VISION_NOTE = (
    "DeepSeek's developer API (api.deepseek.com) only serves text-only "
    "models — deepseek-v4-flash and deepseek-v4-pro. The image analysis "
    "you see at chat.deepseek.com runs on their open-source DeepSeek-VL2 "
    "weights, which aren't exposed through the developer API. Pick a "
    "Gemini model for image questions here."
)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

async def analyze(
    *,
    image_bytes: bytes,
    system_prompt: str,
    user_prompt: str,
    provider: str = "gemini",
    model: Optional[str] = None,
    response_schema: Optional[dict] = None,
    expect_json: bool = True,
    temperature: float = 0.1,
) -> dict:
    started = time.time()
    prov = (provider or "gemini").strip().lower()
    if prov not in ("gemini", "deepseek", "openrouter"):
        prov = "gemini"

    if prov == "gemini":
        out = await _gemini_analyze(
            image_bytes=image_bytes,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            response_schema=response_schema,
            expect_json=expect_json,
            temperature=temperature,
        )
    elif prov == "openrouter":
        out = await _openrouter_analyze(
            image_bytes=image_bytes,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            expect_json=expect_json,
            temperature=temperature,
        )
    else:
        out = await _deepseek_analyze(
            image_bytes=image_bytes,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            expect_json=expect_json,
            temperature=temperature,
        )
    out["latency_ms"] = int((time.time() - started) * 1000)
    out["provider"]   = prov
    return out


# ----------------------------------------------------------------------
# Gemini
# ----------------------------------------------------------------------

def _gemini_default_model() -> str:
    # The non-Live default the AI-Camera Playground uses too.
    return "gemini-2.5-flash"


async def _gemini_analyze(
    *,
    image_bytes: bytes,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str],
    response_schema: Optional[dict],
    expect_json: bool,
    temperature: float,
) -> dict:
    if not state.gemini_api_key:
        return {"ok": False, "text": "", "error": "gemini_api_key_unset"}
    try:
        from google import genai           # type: ignore
        from google.genai import types     # type: ignore
    except Exception as e:
        return {"ok": False, "text": "",
                "error": f"google_genai_missing: {e}"}

    model_name = (model or "").strip() or _gemini_default_model()
    try:
        client = genai.Client(api_key=state.gemini_api_key)
        parts = [
            types.Part(inline_data=types.Blob(data=image_bytes, mime_type="image/jpeg")),
            types.Part(text=user_prompt),
        ]
        cfg_kw: dict[str, Any] = {
            "system_instruction": system_prompt,
            "temperature":        temperature,
        }
        if expect_json:
            cfg_kw["response_mime_type"] = "application/json"
            if response_schema:
                cfg_kw["response_schema"] = response_schema
        cfg_gen = types.GenerateContentConfig(**cfg_kw)
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=[types.Content(role="user", parts=parts)],
            config=cfg_gen,
        )
        text = (response.text or "").strip()
        return {"ok": True, "text": text, "error": None, "model": model_name}
    except Exception as e:
        logger.warning("gemini analyze failed: %s", e)
        return {"ok": False, "text": "", "error": f"{type(e).__name__}: {e}",
                "model": model_name}


# ----------------------------------------------------------------------
# DeepSeek (OpenAI-compatible /v1/chat/completions with vision)
# ----------------------------------------------------------------------

def _deepseek_default_model() -> str:
    return "deepseek-v4-flash"


async def _deepseek_analyze(
    *,
    image_bytes: bytes,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str],
    expect_json: bool,
    temperature: float,
) -> dict:
    if not state.deepseek_api_key:
        return {"ok": False, "text": "", "error": "deepseek_api_key_unset"}

    base = (state.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    model_name = (model or "").strip() or _deepseek_default_model()
    img_b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:image/jpeg;base64,{img_b64}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            # OpenAI vision format: a list of content parts, each with a
            # `type`. DeepSeek's OpenAI-compatible endpoint accepts the
            # same schema where supported.
            {"type": "text",      "text": user_prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]},
    ]
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
    }
    if expect_json:
        # DeepSeek-Chat supports OpenAI's `response_format: json_object`.
        payload["response_format"] = {"type": "json_object"}

    url = f"{base}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {state.deepseek_api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
        if r.status_code != 200:
            # Surface DeepSeek's own error message — usually auth issues
            # or the JSON-parse 400 we get when the chat endpoint rejects
            # an `image_url` content part (DeepSeek's hosted models are
            # text-only). The latter is the most common operator-hit
            # error, so we rewrite it into something human-readable.
            try:
                err = r.json()
                err_msg = (err.get("error") or {}).get("message") or r.text
            except Exception:
                err_msg = r.text
            err_lc = (err_msg or "").lower()
            if "image_url" in err_lc and "unknown variant" in err_lc:
                err_msg = (
                    "DeepSeek's hosted API doesn't accept image input — "
                    "deepseek-chat and deepseek-reasoner are text-only "
                    "models. Switch to a Gemini model for image analysis."
                )
            return {"ok": False, "text": "",
                    "error": f"deepseek_status_{r.status_code}: {err_msg[:300]}",
                    "model": model_name}
        body = r.json()
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        return {"ok": True, "text": str(text).strip(), "error": None,
                "model": model_name}
    except httpx.HTTPError as e:
        logger.warning("deepseek analyze HTTP failed: %s", e)
        return {"ok": False, "text": "",
                "error": f"{type(e).__name__}: {e}", "model": model_name}
    except Exception as e:
        logger.warning("deepseek analyze unexpected: %s", e)
        return {"ok": False, "text": "",
                "error": f"{type(e).__name__}: {e}", "model": model_name}


# ----------------------------------------------------------------------
# OpenRouter (OpenAI-compatible aggregator at openrouter.ai)
# ----------------------------------------------------------------------

def _openrouter_default_model() -> str:
    # Qwen3-VL-8B-Instruct is currently the cheapest vision-capable model
    # on OpenRouter ($0.08 / $0.50 per 1M) and outperforms the older
    # Qwen2.5-VL-7B at roughly half the price. Solid default for kitchen
    # monitoring; operators can pick anything else from the dropdown.
    return "qwen/qwen3-vl-8b-instruct"


def _openrouter_headers() -> dict:
    """OpenRouter recommends app-identifying headers so usage shows up
    cleanly in the operator's dashboard — neither is strictly required."""
    return {
        "Authorization": f"Bearer {state.openrouter_api_key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://primewave.local/pwdemo",
        "X-Title":       "PW Demo Master",
    }


async def _openrouter_analyze(
    *,
    image_bytes: bytes,
    system_prompt: str,
    user_prompt: str,
    model: Optional[str],
    expect_json: bool,
    temperature: float,
) -> dict:
    if not state.openrouter_api_key:
        return {"ok": False, "text": "", "error": "openrouter_api_key_unset"}

    base = (state.openrouter_base_url or "https://openrouter.ai/api/v1").rstrip("/")
    model_name = (model or "").strip() or _openrouter_default_model()
    img_b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:image/jpeg;base64,{img_b64}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text",      "text": user_prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]},
    ]
    payload: dict[str, Any] = {
        "model":       model_name,
        "messages":    messages,
        "temperature": temperature,
    }
    if expect_json:
        # Most modern vision models on OpenRouter honour json_object; if a
        # particular backend doesn't, OpenRouter returns a clear error and
        # the operator can pick a different model.
        payload["response_format"] = {"type": "json_object"}

    url = f"{base}/chat/completions"
    # Tight timeout (was 60s) — when OpenRouter routes a request to a
    # slow backend it can hang on the slowest one. With the AI-Rules
    # engine running scans every 30s+, a 60s hang means the rule's
    # countdown shows "scanning…" for a whole minute. 20s is more than
    # enough for a single vision turn against any reasonable backend
    # and lets us fail fast + re-tick.
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, headers=_openrouter_headers(), json=payload)
        elapsed = time.time() - started
        if elapsed > 5.0:
            logger.info("openrouter slow call model=%s elapsed=%.1fs",
                         model_name, elapsed)
        if r.status_code != 200:
            try:
                err = r.json()
                err_msg = (err.get("error") or {}).get("message") or r.text
            except Exception:
                err_msg = r.text
            return {"ok": False, "text": "",
                    "error": f"openrouter_status_{r.status_code}: {err_msg[:300]}",
                    "model": model_name}
        body = r.json()
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        return {"ok": True, "text": str(text).strip(), "error": None,
                "model": model_name}
    except httpx.ReadTimeout:
        elapsed = time.time() - started
        logger.warning("openrouter analyze timed out after %.1fs model=%s",
                        elapsed, model_name)
        return {"ok": False, "text": "",
                "error": f"openrouter_timeout: {model_name} took >{elapsed:.0f}s",
                "model": model_name}
    except httpx.HTTPError as e:
        logger.warning("openrouter analyze HTTP failed: %s", e)
        return {"ok": False, "text": "",
                "error": f"{type(e).__name__}: {e}", "model": model_name}
    except Exception as e:
        logger.warning("openrouter analyze unexpected: %s", e)
        return {"ok": False, "text": "",
                "error": f"{type(e).__name__}: {e}", "model": model_name}


# ----------------------------------------------------------------------
# Model listing — used by the SPA dropdowns.
# ----------------------------------------------------------------------

async def list_vision_models() -> dict:
    """Return the union of available vision-capable models grouped by
    provider. Gemini's list is pulled from the user's account; DeepSeek's
    is the hard-coded catalog above — filtered to vision-capable entries,
    which is currently empty (see DEEPSEEK_MODELS comment)."""
    out: dict[str, dict] = {
        "gemini":     {"configured": bool(state.gemini_api_key),     "models": []},
        "deepseek":   {"configured": bool(state.deepseek_api_key),   "models": [],
                       "note": DEEPSEEK_VISION_NOTE},
        "openrouter": {"configured": bool(state.openrouter_api_key), "models": []},
    }

    # Gemini — same filter the global Playground uses (generateContent
    # capability, drop -live variants).
    if state.gemini_api_key:
        try:
            from google import genai   # type: ignore
            client = genai.Client(api_key=state.gemini_api_key)
            async for m in await client.aio.models.list():
                actions = list(getattr(m, "supported_actions", None) or [])
                if "generateContent" not in actions:
                    continue
                name = (getattr(m, "name", "") or "").split("/")[-1]
                if not name or "-live" in name:
                    continue
                out["gemini"]["models"].append({
                    "name":    name,
                    "display": getattr(m, "display_name", None) or name,
                })
            out["gemini"]["models"].sort(key=lambda x: x["name"], reverse=True)
        except Exception as e:
            logger.warning("gemini list_models failed: %s", e)

    # DeepSeek — filter the static catalog to vision-capable entries. As
    # of now that yields an empty list (see DEEPSEEK_MODELS). We still
    # report the provider as configured + return the `note` above so the
    # SPA can explain *why* no DeepSeek models show in vision dropdowns.
    if state.deepseek_api_key:
        out["deepseek"]["models"] = [
            {"name": m["name"], "display": m["display"]}
            for m in DEEPSEEK_MODELS if m.get("vision")
        ]

    # OpenRouter — pull the live model catalog and keep only the ones that
    # declare `image` as an input modality. OpenRouter exposes ~200 models
    # but only ~30-50 are vision-capable.
    if state.openrouter_api_key:
        try:
            base = (state.openrouter_base_url or
                    "https://openrouter.ai/api/v1").rstrip("/")
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(f"{base}/models",
                                      headers=_openrouter_headers())
            if r.status_code == 200:
                data = r.json()
                for m in (data.get("data") or []):
                    arch = m.get("architecture") or {}
                    modalities = arch.get("input_modalities") or []
                    if "image" not in modalities:
                        continue
                    mid = m.get("id") or ""
                    if not mid:
                        continue
                    out["openrouter"]["models"].append({
                        "name":    mid,
                        "display": m.get("name") or mid,
                    })
                out["openrouter"]["models"].sort(key=lambda x: x["name"])
            else:
                logger.warning("openrouter list_models HTTP %s: %s",
                                r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("openrouter list_models failed: %s", e)

    return out
