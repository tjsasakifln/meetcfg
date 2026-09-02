"""Turns the rolling transcript into ONE short commercial orientation.

Rewritten from live-meeting-assistant's suggestions/engine.py (MIT, Ben
Linford). What was kept is the trigger discipline, which is the hard part:

- debounced: a run starts only after SUGGEST_MIN_NEW_CHARS of fresh speech
  AND SUGGEST_MIN_INTERVAL_S since the last run started
- single-flight: at most one LLM call alive per meeting; speech arriving
  mid-run is picked up by an immediate follow-up run, never queued behind more

What changed:
- only the LEAD's speech counts as "fresh" (requirement: intervene only on new
  relevant speech from the other side, never because Tiago is talking)
- the JSON card array became one SINAL / FAÇA / DIGA block, or "--" for silence
- "--" is not broadcast: the previously shown orientation stays on screen
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from pathlib import Path

from ..config import settings
from ..llm import LLMError, generate, provider_name

log = logging.getLogger("meetcfg.copilot")

REPO_ROOT = Path(__file__).resolve().parents[3]

INSTRUCTION = """Você está auxiliando {name} durante uma reunião comercial da CONFENGE. \
Ele domina tecnicamente o assunto. Intervenha apenas quando houver algo comercialmente \
relevante. Nunca escreva discursos, nunca dê mais de uma intervenção, priorize perguntas \
sobre argumentação e não repita o que {name} acabou de dizer.

A transcrição é automática: espere palavras trocadas e pontuação errada, e infira a intenção.

FORMATO DE RESPOSTA — obrigatório, exatamente 3 linhas:
SINAL: <o que está acontecendo, no máximo 12 palavras>
FAÇA: <uma orientação curta, no máximo 20 palavras>
DIGA: "<uma frase ou pergunta curta que {name} pode falar quase literalmente>"

Se {name} já está conduzindo bem, se nada mudou, ou se não há nada comercialmente \
relevante agora, responda APENAS com dois traços:
--

Nunca escreva nada fora desse formato. Sem markdown, sem preâmbulo, sem explicação, \
sem alternativas. Não invente fatos sobre o lead ou a empresa dele."""


def load_sales_context() -> str:
    """Read the versioned commercial context file (re-read each run so edits
    land without a restart). Missing file is not fatal."""
    path = Path(settings.sales_context_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("sales context unreadable (%s): %s", path, e)
        return ""


def build_system_prompt() -> str:
    return INSTRUCTION.format(name=settings.user_name)


def parse_advice(text: str) -> dict | None:
    """Parse the model reply into {sinal, faca, diga}, or None for silence.

    Tolerant on purpose: accents, bold, and a stray preamble line happen, and
    dropping a good orientation over formatting would be worse than showing it.
    """
    t = (text or "").strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t, flags=re.S).strip()
    if not t:
        return None
    # Silence: "--", "—", "- -", possibly with surrounding whitespace/quotes.
    if re.fullmatch(r"[\"'`]*[-–—\s]{1,8}[\"'`]*", t):
        return None

    fields = {"sinal": "", "faca": "", "diga": ""}
    patterns = {
        "sinal": r"^\W*SINAL\s*:\s*(.+)$",
        "faca": r"^\W*FA[ÇC]A\s*:\s*(.+)$",
        "diga": r"^\W*DIGA\s*:\s*(.+)$",
    }
    for line in t.splitlines():
        line = line.strip().replace("**", "")
        for key, pat in patterns.items():
            m = re.match(pat, line, flags=re.I)
            if m and not fields[key]:
                fields[key] = m.group(1).strip()
    fields["diga"] = fields["diga"].strip('"“”\'')
    if not any(fields.values()):
        # Unparseable but non-empty: better to show it than to swallow it.
        log.warning("unparseable copilot reply, showing raw: %.120s", t)
        return {"sinal": "", "faca": t[:200], "diga": ""}
    return fields


class CopilotEngine:
    def __init__(self, session):
        self.session = session
        self._consumed = 0          # lead chars already covered by a run
        self._last_run_t = 0.0      # monotonic time the last run STARTED
        self._force = False
        self._runner: asyncio.Task | None = None
        self._last_advice_text = ""
        self._stopped = False

    # -- triggers ------------------------------------------------------------
    def poke(self, force: bool = False) -> None:
        if self._stopped or not settings.suggest_enabled:
            return
        if force:
            self._force = True
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run_when_ready())

    def stop(self) -> None:
        self._stopped = True
        if self._runner and not self._runner.done():
            self._runner.cancel()

    def _new_chars(self) -> int:
        """Fresh characters spoken by the LEAD since the last run."""
        return self.session.lead_chars() - self._consumed

    # -- the single-flight loop ----------------------------------------------
    async def _run_when_ready(self) -> None:
        try:
            while not self._stopped:
                if not self._force:
                    since = time.monotonic() - self._last_run_t
                    wait_interval = settings.suggest_min_interval_s - since
                    if self._new_chars() < settings.suggest_min_new_chars and wait_interval <= 0:
                        return  # not enough new material; a future poke restarts us
                    if wait_interval > 0:
                        await asyncio.sleep(min(wait_interval, 2.0))
                        continue
                    if self._new_chars() < settings.suggest_min_new_chars:
                        return
                self._force = False
                ok = await self._run_once()
                if not ok:
                    return  # LLM failed; _consumed is intact, next poke retries
                if self._new_chars() < settings.suggest_min_new_chars and not self._force:
                    return
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - engine must never take the app down
            log.exception("copilot loop crashed; will restart on next poke")

    async def _run_once(self) -> bool:
        """Run one round. Returns False on LLM failure (caller stops the loop
        without touching _consumed, so the backlog survives for the next poke)."""
        self._last_run_t = time.monotonic()
        snapshot = list(self.session.lines)
        prompt = self._build_prompt(snapshot)
        await self.session.broadcast({"type": "copilot_status", "state": "thinking"})
        t0 = time.monotonic()
        try:
            result, meta = await generate(build_system_prompt(), prompt)
        except LLMError as e:
            log.error("LLM call failed (%s): %s", provider_name(), e)
            await self.session.broadcast({
                "type": "copilot_status", "state": "error", "msg": str(e)[:200],
            })
            return False
        self._consumed = sum(len(l.text) for l in snapshot if l.source == "system")
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        advice = parse_advice(result)
        if advice is None:
            log.info("copilot: silent (-- ) in %dms", elapsed_ms)
            await self.session.broadcast({
                "type": "copilot_status", "state": "silent", "last_ms": elapsed_ms,
                "at": dt.datetime.now().strftime("%H:%M:%S"),
            })
            return True
        self._last_advice_text = " / ".join(v for v in advice.values() if v)
        payload = {
            "type": "advice",
            **advice,
            "at": dt.datetime.now().strftime("%H:%M:%S"),
            "elapsed_ms": elapsed_ms,
        }
        self.session.last_advice = payload
        log.info("copilot: advice in %dms — %.80s", elapsed_ms, self._last_advice_text)
        await self.session.broadcast(payload)
        await self.session.broadcast({
            "type": "copilot_status", "state": "idle", "last_ms": elapsed_ms,
        })
        return True

    # -- prompt --------------------------------------------------------------
    def _build_prompt(self, lines) -> str:
        parts = []
        context = load_sales_context()
        if context:
            parts.append("CONTEXTO COMERCIAL (fixo):")
            parts.append(context)
        # Rolling tail, most recent last, bounded by char budget (~60-120s).
        budget = settings.suggest_transcript_chars
        used = 0
        tail: list[str] = []
        for line in reversed(lines):
            s = f"{line.speaker()}: {line.text}"
            if used + len(s) > budget:
                break
            tail.append(s)
            used += len(s)
        tail.reverse()
        parts.append("\nTRECHO RECENTE DA CONVERSA (mais recente por último):")
        parts.append("\n".join(tail) if tail else "(nada capturado ainda)")
        if self._last_advice_text:
            parts.append("\nÚLTIMA ORIENTAÇÃO JÁ DADA (não repita):")
            parts.append(self._last_advice_text)
        parts.append("\nGere a orientação agora, ou responda -- se não houver nada a fazer.")
        return "\n".join(parts)
