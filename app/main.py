import os
import time
import asyncio
import logging
import hashlib
import json

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("alexa-hermes-bridge")
logging.basicConfig(level=logging.INFO)

# --- Required environment ---------------------------------------------------
HERMES_API_URL = os.environ["HERMES_API_URL"]          # e.g. http://192.168.1.17:8642/v1
HERMES_API_KEY = os.environ["HERMES_API_KEY"]           # matches API_SERVER_KEY in Hermes .env
ALEXA_SKILL_ID = os.environ.get("ALEXA_SKILL_ID", "")   # optional strict application.applicationId check

# Model pin for the Hermes call. The API server honors an explicit provider
# per-request, so Alexa can stay on a fast/cheap model regardless of the
# global Hermes default (which the user tunes elsewhere).
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-chat")
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "deepseek")

# Optional Discord delivery for async completions: uses the Hermes Discord
# bot to post the finished result to a channel when a request outlives the
# fast path.
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

# How long we let Hermes think before we tell Alexa "I'm working on it"
# and detach. Alexa's own hard limit is ~8s; we leave headroom for network
# + Alexa's own TTS/response packaging.
FAST_PATH_TIMEOUT_SECONDS = float(os.environ.get("FAST_PATH_TIMEOUT_SECONDS", "6.0"))

DEFAULT_ASYNC_NOTE = os.environ.get(
    "ASYNC_NOTE",
    "Let me work on that — I'll message you on Discord when I'm done.",
)

app = FastAPI(
    title="Alexa-Hermes Bridge",
    version="1.1.0",
    description="Accepts Alexa Custom Skill requests and forwards them to Hermes Agent's OpenAI-compatible API.",
)


@app.get("/health")
def health():
    return {"status": "ok"}


def _session_key(alexa_user_id: str) -> str:
    """Stable per-Alexa-user Hermes session/memory scope.

    Alexa user IDs (amzn1.ask.account.*) can be several hundred characters
    long, which Hermes rejects ("Session key too long"). Hash to a short,
    stable key: same user always maps to the same key.
    """
    digest = hashlib.sha256(alexa_user_id.encode("utf-8")).hexdigest()[:32]
    return f"alexa:{digest}"


def _speech_response(text: str, *, end_session: bool = True) -> dict:
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end_session,
        },
    }


def _unwrap_structured_reply(text: str) -> str:
    """Extract spoken text when the model wrapped its answer in a JSON envelope.

    Local models (e.g. qwen3-8b) often answer as structured JSON like
    {"status": "success", "value": "Hello!"} — which Alexa would otherwise
    read aloud as raw JSON. If the reply parses as JSON, pull the first
    human-readable string field (value/text/message/response/answer/content,
    then any string values); otherwise return the text unchanged.
    """
    if not text:
        return text
    t = text.strip()
    if not (t.startswith("{") or t.startswith("[")):
        return text
    try:
        data = json.loads(t)
    except Exception:
        return text
    if isinstance(data, dict):
        for key in ("value", "text", "message", "response", "answer", "content", "output"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        strs = [str(v) for v in data.values() if isinstance(v, str) and v.strip()]
        if strs:
            return " ".join(strs)
    elif isinstance(data, list):
        strs = [str(v) for v in data if isinstance(v, str) and v.strip()]
        if strs:
            return " ".join(strs)
    return text


async def _ask_hermes(query: str, session_key: str, *, timeout: float) -> str:
    """Call Hermes's OpenAI-compatible endpoint and return the reply text."""
    headers = {
        "Authorization": f"Bearer {HERMES_API_KEY}",
        "Content-Type": "application/json",
        "X-Hermes-Session-Key": session_key,
    }
    payload = {
        "model": MODEL_NAME,
        "provider": MODEL_PROVIDER,
        "messages": [{"role": "user", "content": query}],
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{HERMES_API_URL}/chat/completions", json=payload, headers=headers
        )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def _post_to_discord(text: str) -> None:
    """Best-effort async completion delivery via the Hermes Discord bot."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                json={"content": f"🤖 **Hermes via Alexa** — {text[:1900]}"},
            )
    except Exception:
        logger.exception("Discord delivery failed")


async def _fire_and_forget_hermes(query: str, session_key: str) -> None:
    """Let a long-running Hermes turn finish after we've already answered Alexa.

    Hermes enforces its own safety/approval policy on the far side regardless
    of which channel triggered the turn -- the bridge does not attempt to
    bypass or pre-authorize anything. The completed reply is delivered to
    Discord (best effort).
    """
    try:
        reply = await _ask_hermes(query, session_key, timeout=600.0)
        await _post_to_discord(_unwrap_structured_reply(reply))
    except Exception:
        logger.exception("Detached Hermes turn failed for session %s", session_key)


class AlexaRequest(BaseModel):
    version: str | None = None
    session: dict | None = None
    context: dict | None = None
    request: dict


@app.post("/alexa")
async def alexa_endpoint(body: AlexaRequest, req: Request):
    app_id = (
        (body.context or {}).get("System", {}).get("application", {}).get("applicationId")
        or (body.session or {}).get("application", {}).get("applicationId")
    )
    logger.info("Received Alexa request from appId: %s (expected: %s)", app_id, ALEXA_SKILL_ID)
    if ALEXA_SKILL_ID and app_id and app_id != ALEXA_SKILL_ID:
        logger.warning("Skill ID mismatch: got %s, expected %s (proceeding anyway during development)", app_id, ALEXA_SKILL_ID)

    req_type = body.request.get("type")
    user_id = (
        (body.context or {}).get("System", {}).get("user", {}).get("userId")
        or (body.session or {}).get("user", {}).get("userId")
        or "unknown"
    )
    session_key = _session_key(user_id)

    if req_type == "LaunchRequest":
        return _speech_response(
            "Hermes is listening. What do you need?", end_session=False
        )

    if req_type == "SessionEndedRequest":
        return {"version": "1.0", "response": {}}

    if req_type != "IntentRequest":
        return _speech_response("I didn't understand that request.")

    intent = body.request.get("intent", {})
    intent_name = intent.get("name", "")

    if intent_name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return _speech_response("Goodbye.")

    if intent_name in ("AMAZON.HelpIntent",):
        return _speech_response(
            "Ask me anything, like homelab status, Kubernetes health, "
            "or to control a smart home device.",
            end_session=False,
        )

    slots = intent.get("slots", {})
    query = (slots.get("Query") or {}).get("value") or ""
    if not query:
        return _speech_response(
            "I didn't catch what you wanted to ask Hermes. Try again?",
            end_session=False,
        )

    start = time.monotonic()
    try:
        reply = await asyncio.wait_for(
            _ask_hermes(query, session_key, timeout=FAST_PATH_TIMEOUT_SECONDS),
            timeout=FAST_PATH_TIMEOUT_SECONDS,
        )
        return _speech_response(_unwrap_structured_reply(reply))
    except (asyncio.TimeoutError, httpx.TimeoutException):
        elapsed = time.monotonic() - start
        logger.info(
            "Fast path timed out after %.1fs for session %s; detaching", elapsed, session_key
        )
        asyncio.create_task(_fire_and_forget_hermes(query, session_key))
        return _speech_response(
            f"I'm working on that — {DEFAULT_ASYNC_NOTE}"
        )
    except httpx.HTTPStatusError as exc:
        logger.error("Hermes API error %s: %s", exc.response.status_code, exc.response.text)
        return _speech_response(
            "I ran into a problem reaching Hermes. Please try again shortly."
        )
    except Exception:
        logger.exception("Unexpected error handling Alexa request")
        return _speech_response("Something went wrong on my end. Please try again.")
