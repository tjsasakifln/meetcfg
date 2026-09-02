"""LLM access — one provider: a shell command.

Trimmed from live-meeting-assistant's llm package (MIT, Ben Linford) down to
the `custom-command` escape hatch, which is what wires Codex CLI in without
any API key (see bin/codex_llm.sh).
"""
from __future__ import annotations


class LLMError(RuntimeError):
    pass


def provider_name() -> str:
    return "custom-command"


async def generate(system_prompt: str, user_prompt: str, *, timeout: float | None = None):
    from . import custom_cmd
    return await custom_cmd.generate(system_prompt, user_prompt, timeout=timeout)
