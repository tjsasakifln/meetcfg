"""Provider: a user-supplied shell command.

From live-meeting-assistant's llm/custom_cmd.py (MIT, Ben Linford); the
fast/deep tier split was removed since there is only one tier here.

CUSTOM_LLM_CMD reads the user prompt on stdin and prints the reply on stdout.
The system prompt arrives in $LLM_SYSTEM_PROMPT. Default: bin/codex_llm.sh.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..config import settings
from . import LLMError

log = logging.getLogger("meetcfg.llm")

REPO_ROOT = Path(__file__).resolve().parents[3]


async def generate(system_prompt: str, user_prompt: str, *, timeout: float | None = None):
    cmd = settings.custom_llm_cmd
    if not cmd:
        raise LLMError("CUSTOM_LLM_CMD is not set")
    env = dict(os.environ)
    env["LLM_SYSTEM_PROMPT"] = system_prompt
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(REPO_ROOT),   # so a relative CUSTOM_LLM_CMD resolves
    )
    eff_timeout = timeout or settings.suggest_timeout_s
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(user_prompt.encode()), timeout=eff_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError(f"LLM command timed out after {eff_timeout}s")
    stderr = stderr_b.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise LLMError(f"LLM command exited {proc.returncode}: {stderr[:300]}")
    if stderr:
        log.debug("llm stderr: %.500s", stderr)
    return stdout_b.decode(errors="replace").strip(), {"provider": "custom-command"}
