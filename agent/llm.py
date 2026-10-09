"""Shared plumbing for the Anthropic calls: client, retries, pause_turn,
refusal fallback, and a running cost tally so every run reports what it spent.
"""
from __future__ import annotations

import threading

import anthropic

# Refusal fallback: if a safety classifier declines a request, the API reroutes
# it to a suitable model instead of returning an empty answer. Resume and
# company research are unlikely to trip one, but a silent empty packet is the
# failure mode we'd otherwise get.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# $ per million tokens (input, output). Used only for the end-of-run estimate.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
}
WEB_SEARCH_PRICE = 0.01  # $10 per 1,000 searches

_lock = threading.Lock()
_spend: dict[str, float] = {}


def client() -> anthropic.Anthropic:
    # The SDK already retries 408/409/429/5xx and connection errors with
    # exponential backoff (default 2 retries). Research and generation are the
    # expensive calls, so losing one to a transient overload is worth 2 more
    # tries. Generation can run several minutes; the timeout covers it.
    return anthropic.Anthropic(max_retries=4, timeout=900)


def record(stage: str, model: str, usage) -> None:
    if usage is None:
        return
    base = model.split("-2025")[0].split("-2026")[0]
    pin, pout = PRICES.get(base, (0.0, 0.0))
    cost = (getattr(usage, "input_tokens", 0) or 0) * pin / 1e6
    cost += (getattr(usage, "cache_creation_input_tokens", 0) or 0) * pin * 1.25 / 1e6
    cost += (getattr(usage, "cache_read_input_tokens", 0) or 0) * pin * 0.1 / 1e6
    cost += (getattr(usage, "output_tokens", 0) or 0) * pout / 1e6
    stu = getattr(usage, "server_tool_use", None)
    cost += (getattr(stu, "web_search_requests", 0) or 0) * WEB_SEARCH_PRICE
    with _lock:
        _spend[stage] = _spend.get(stage, 0.0) + cost


def spend() -> dict[str, float]:
    with _lock:
        return dict(_spend)


def run_turn(c: anthropic.Anthropic, stage: str, *, model: str, messages: list,
             max_continuations: int = 6, betas: list[str] | None = None, **kw):
    """One logical request. Streams (long outputs would otherwise hit HTTP
    timeouts), resumes server-tool `pause_turn` stops, and returns the final
    message plus every content block produced along the way."""
    messages = list(messages)
    blocks: list = []
    msg = None
    for _ in range(max_continuations + 1):
        with c.beta.messages.stream(
            model=model, messages=messages,
            betas=[FALLBACK_BETA, *(betas or [])], fallbacks="default", **kw,
        ) as stream:
            msg = stream.get_final_message()
        record(stage, model, msg.usage)
        blocks.extend(msg.content)
        if msg.stop_reason != "pause_turn":
            break
        # Long server-tool work pauses mid-turn; send it back as-is to continue.
        messages.append({"role": "assistant", "content": msg.content})
        if getattr(msg, "container", None) is not None and isinstance(kw.get("container"), dict):
            kw["container"] = {**kw["container"], "id": msg.container.id}
    if msg is not None and msg.stop_reason == "refusal":
        raise RuntimeError(f"{stage}: request declined ({getattr(msg, 'stop_details', None)})")
    return msg, blocks


def text_of(blocks) -> str:
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")
