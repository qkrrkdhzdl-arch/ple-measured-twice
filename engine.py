"""Model loop for the measured-twice harness.

One asynchronous engine drives every role (survey, planner, worker, verifier).
It owns the things that decide both score and cost on PLE:

* the reasoning effort and output cap sent to the model,
* clipping of tool results (a 40 KB spreadsheet dump costs more than the
  engineering it describes),
* context compaction once the conversation grows past a share of the window,
* retry/backoff for 429/5xx/timeouts and graceful degradation when the clock
  runs out,
* per-call usage reporting to the evaluator, which becomes the cost axis of
  the Stage 2 performance-cost Pareto frontier.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

import openai

MODEL = os.environ.get("MODEL_NAME", "claude-opus-5-5")
# Reasoning effort per model family. Google's gateway defaults to the highest
# setting; we keep it explicit so ablations are reproducible.
MAX_EFFORT = ("claude", "glm", "gpt-6", "deepseek", "opus")
EFFORT = os.environ.get("REASONING_EFFORT") or (
    "max" if any(m in MODEL.lower() for m in MAX_EFFORT) else "high"
)
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
# Characters of a tool result the model sees before the middle is cut.
RESULT_CHARS = int(os.environ.get("RESULT_CHARS", "16000"))
# Compact the conversation once it passes this many characters.
COMPACT_CHARS = int(os.environ.get("COMPACT_CHARS", "300000"))
# After compaction, keep this many of the newest messages.
KEEP_RECENT = 10
WRAP_UP_SEC = 240
RETRY_WAITS = (5, 15, 30, 60, 120)
MAX_NUDGES = 3
CONTEXT_FULL = re.compile(
    r"context.?(length|window)|prompt is too long|maximum prompt length"
    r"|exceed[^.]{0,40}(token|context)",
    re.I,
)
CACHE_MARK = "claude" in MODEL.lower()

WRAP_UP = {
    "role": "user",
    "content": (
        "The clock is nearly out. Stop investigating and write your answer "
        "with what you have. Do not start a new long tool sequence."
    ),
}


def _tool(name: str, description: str, **params: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": v} for k, v in params.items()},
                "required": list(params),
            },
        },
    }


def _marked(messages: list[dict]) -> list[dict]:
    """Marks the system prompt and newest message for Claude prompt caching."""
    if not CACHE_MARK:
        return messages
    out = list(messages)
    for i in {0, len(out) - 1}:
        content = out[i].get("content")
        if isinstance(content, str) and content:
            out[i] = {
                **out[i],
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        else:
            out[i] = {**out[i], "cache_control": {"type": "ephemeral"}}
    return out


def clip(text: str, limit: int | None = None) -> str:
    """Cuts the middle of a long tool result, keeping head and tail."""
    limit = limit or RESULT_CHARS
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        f"{text[:half]}\n[... {len(text) - limit} characters cut; ask for a "
        f"narrower slice (offset/limit, grep, or head/tail) if you need the "
        f"middle ...]\n{text[-half:]}"
    )


def _chars(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            total += sum(len(str(p.get("text", ""))) for p in c if isinstance(p, dict))
        for call in m.get("tool_calls") or []:
            total += len(json.dumps(call.get("function", {})))
    return total


def _digest(messages: list[dict]) -> str:
    """A compact digest of the dropped part of a compacted conversation."""
    lines = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        content = (content or "").strip().replace("\n", " ")
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            lines.append(f"- {role} called {fn.get('name')} {str(fn.get('arguments'))[:160]}")
        if content:
            lines.append(f"- {role}: {content[:400]}")
    return "\n".join(lines[-60:])


class Engine:
    """Runs one role's conversation against the sponsored model."""

    def __init__(self, session):
        self.session = session
        self.client = openai.AsyncOpenAI(max_retries=5, timeout=1800)
        # `reasoning_effort` is only passed for families that accept it.
        self.send_effort = True
        self.meter = {"calls": 0, "input": 0, "output": 0, "cached": 0}
        self.calls = 0

    # ---------------------------------------------------------------- model
    async def chat(self, messages, tools, deadline_sec, tool_choice="auto"):
        """One model call with retries, context recovery and wrap-up logic."""
        left = self.session.time_left()
        wrap_up = left is not None and left < deadline_sec
        timeout = None if left is None else max(1.0, left - deadline_sec)
        if wrap_up:
            timeout = min(WRAP_UP_SEC, max(1.0, left or WRAP_UP_SEC))
        kwargs = {
            "model": MODEL,
            "messages": _marked(messages),
            "tools": tools,
            "tool_choice": "none" if wrap_up else tool_choice,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
        }
        if self.send_effort:
            kwargs["reasoning_effort"] = "low" if wrap_up else EFFORT
        try:
            reply = await asyncio.wait_for(
                self.client.chat.completions.create(**kwargs), timeout
            )
        except openai.APIError as exc:
            if not self.send_effort and "reasoning_effort" in str(exc):
                self.send_effort = False
                return await self.chat(messages, tools, deadline_sec, tool_choice)
            if CONTEXT_FULL.search(str(exc)):
                self.compact(messages, hard=True)
                return await self.chat(messages, tools, deadline_sec, tool_choice)
            raise
        self.calls += 1
        self.meter["calls"] += 1
        usage = getattr(reply, "usage", None)
        if usage:
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) or 0
            self.meter["input"] += usage.prompt_tokens or 0
            self.meter["output"] += usage.completion_tokens or 0
            self.meter["cached"] += cached
            await self.session.usage(
                MODEL,
                usage.prompt_tokens or 0,
                usage.completion_tokens or 0,
                cached,
            )
        return reply, wrap_up

    # ------------------------------------------------------------ compaction
    def compact(self, messages, hard=False):
        """Folds older turns into a digest so the conversation keeps fitting."""
        if len(messages) <= KEEP_RECENT + 2:
            return
        system, rest = messages[0], messages[1:]
        dropped, kept = rest[:-KEEP_RECENT], rest[-KEEP_RECENT:]
        digest = _digest(dropped)
        note = {
            "role": "user",
            "content": (
                "## Earlier steps of this task (compacted)\n"
                f"{digest}\n\nContinue from here. The files on disk are the "
                "source of truth; re-read only what you need."
            ),
        }
        messages[:] = [system, note, *kept]
        if hard:
            for m in messages[-4:]:
                if isinstance(m.get("content"), str) and len(m["content"]) > 4000:
                    m["content"] = clip(m["content"], 4000)

    # ------------------------------------------------------------------ role
    async def run_role(
        self,
        *,
        name,
        system,
        user,
        tools,
        dispatch,
        deadline_sec,
        final=False,
        max_steps=200,
        reminder=None,
    ):
        """Drive one role until it answers without a tool call."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        nudges, head = 0, ""
        for _ in range(max_steps):
            left = self.session.time_left()
            if left is not None and left <= 1:
                break
            if _chars(messages) > COMPACT_CHARS:
                self.compact(messages)
            if left is not None and left < deadline_sec and messages[-1] is not WRAP_UP:
                messages.append(dict(WRAP_UP))
            try:
                reply, wrap_up = await self.chat(messages, tools, deadline_sec)
            except asyncio.TimeoutError:
                if left is not None and left < deadline_sec:
                    break
                continue
            except openai.APIError as exc:
                await self.session.log(f"[{name}] model error: {exc}")
                if await self._recover(exc):
                    continue
                raise
            choice = reply.choices[0]
            message = choice.message
            cut = choice.finish_reason == "length"
            text = head + (message.content or "")
            if final and text:
                self.session.final_message = text
            turn = message.model_dump(exclude_none=True)
            if wrap_up or not message.tool_calls:
                turn.pop("tool_calls", None)
                if message.content:
                    messages.append(turn)
                if wrap_up or (message.content and not cut) or nudges >= MAX_NUDGES:
                    await self.session.log(
                        f"[{name}] done after {self.calls} calls "
                        f"({self.meter['input']}in/{self.meter['output']}out)"
                    )
                    return text
                nudges, head = nudges + 1, text
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your reply was cut off; continue where it stopped."
                            if cut
                            else "Continue, and finish with your answer."
                        ),
                    }
                )
                continue
            nudges, head = 0, ""
            messages.append(turn)
            for call in turn["tool_calls"]:
                fn = call["function"]
                await self.session.log(f"[{name}] {fn['name']} {fn['arguments'][:160]}")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    out = "Error: arguments were not valid JSON. Retry the call."
                else:
                    try:
                        out = await dispatch(fn["name"], args)
                    except Exception as exc:  # a tool failure is data, not a stop
                        out = f"Error: {type(exc).__name__}: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": clip(str(out)),
                    }
                )
            if reminder:
                messages.append({"role": "user", "content": reminder(messages)})
        return head or self.session.final_message or ""

    async def _recover(self, exc) -> bool:
        """Backs off and returns True when the same call may be retried."""
        wait = RETRY_WAITS[min(self.calls, len(RETRY_WAITS) - 1)]
        await asyncio.sleep(wait)
        return True


def elapsed(start: float) -> str:
    return f"{time.monotonic() - start:.0f}s"
