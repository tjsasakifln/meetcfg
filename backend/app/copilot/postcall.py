"""Relatório mínimo do pós-chamada — uma chamada ao Codex, sob demanda.

Fica fora do caminho de latência ao vivo de propósito: roda uma única vez,
depois que a reunião acabou, por ação explícita de quem conduziu (POST
/api/postcall). Nada dispara isto sozinho.

Nada é persistido. O relatório volta na resposta HTTP e morre ali — quem quiser
guardar, guarda (tools/post_call_report.py imprime no stdout).
"""
from __future__ import annotations

import logging
import re

from ..llm import LLMError, generate
from .engine import load_sales_context, load_structured_context
from .context import render_for_prompt

log = logging.getLogger("meetcfg.postcall")

# Generous, but bounded: a two-hour call would otherwise blow the prompt. The
# tail is what survives, because desfecho e compromissos moram no fim.
TRANSCRIPT_CHARS = 20000

INSTRUCTION = """Você está resumindo uma reunião comercial da CONFENGE que acabou de \
terminar, para quem a conduziu. Seja factual e curto: relate o que aconteceu, não o \
que deveria ter acontecido, e não invente nada que não esteja na transcrição.

A transcrição é automática: espere palavras trocadas e pontuação errada, e infira a \
intenção. "Tiago" é quem conduziu; "Lead" é o outro lado.

FORMATO DE RESPOSTA — obrigatório, uma linha por campo, nesta ordem:
DESFECHO: <o que de fato ficou decidido ou não, no máximo 25 palavras>
OBJEÇÕES: <as objeções que o lead levantou; "nenhuma" se não houve>
COMPROMISSOS: <o que cada lado se comprometeu a fazer; "nenhum" se não houve>
PRÓXIMA AÇÃO: <a próxima ação e o prazo, só se foram ditos; senão "não definida">
FATOS NOVOS: <o que o lead afirmou e ainda NÃO está verificado, para não virar \
premissa; "nenhum" se não houve>
FOLLOW-UP: <duas ou três frases que dá para colar num email de retomada>

Um campo pode ocupar mais de uma linha se precisar listar itens. Sem markdown, sem \
preâmbulo, sem recomendação de coaching, sem alternativas."""

FIELDS = {
    "desfecho": r"^\W*DESFECHO\s*:\s*(.*)$",
    "objecoes": r"^\W*OBJE[ÇC][ÕO]ES\s*:\s*(.*)$",
    "compromissos": r"^\W*COMPROMISSOS\s*:\s*(.*)$",
    "proxima_acao": r"^\W*PR[ÓO]XIMA\s+A[ÇC][ÃA]O\s*:\s*(.*)$",
    "fatos_novos": r"^\W*FATOS\s+NOVOS\s*:\s*(.*)$",
    "follow_up": r"^\W*FOLLOW[\s-]?UP\s*:\s*(.*)$",
}


def parse_report(text: str) -> dict:
    """Labeled lines into a dict, tolerantly — same spirit as parse_advice.

    An unlabeled line belongs to the label above it, so a model that lists
    objections one per line doesn't lose them. Anything before the first label
    is dropped: that is preamble, not report.
    """
    t = (text or "").strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t, flags=re.S).strip()
    out = {k: "" for k in FIELDS}
    current: str | None = None
    for line in t.splitlines():
        line = line.strip().replace("**", "")
        if not line:
            continue
        matched = False
        for key, pat in FIELDS.items():
            m = re.match(pat, line, flags=re.I)
            if m:
                current = key
                if not out[key]:
                    out[key] = m.group(1).strip()
                matched = True
                break
        if not matched and current and out[current] is not None:
            out[current] = (out[current] + "\n" + line).strip()
    if not any(out.values()):
        # Unparseable but non-empty: surface it instead of returning six blanks.
        log.warning("unparseable post-call reply, showing raw: %.120s", t)
        out["desfecho"] = t[:600]
    return out


def build_prompt(session) -> str:
    parts: list[str] = []
    context = load_sales_context()
    if context:
        parts.append("CONTEXTO COMERCIAL (fixo):")
        parts.append(context)
    lead = load_structured_context()
    if lead is not None:
        parts.append("\nCONTEXTO ESTRUTURADO DESTE LEAD (CONFENGE_SALES_CONTEXT/1.0):")
        parts.append(render_for_prompt(lead))
        parts.append("\nO que já era sabido acima não conta como fato novo.")

    used = 0
    tail: list[str] = []
    for line in reversed(session.lines):
        s = f"{line.speaker()}: {line.text}"
        if used + len(s) > TRANSCRIPT_CHARS:
            break
        tail.append(s)
        used += len(s)
    tail.reverse()
    parts.append("\nTRANSCRIÇÃO DA REUNIÃO (mais recente por último):")
    parts.append("\n".join(tail))
    parts.append("\nGere o relatório agora.")
    return "\n".join(parts)


async def generate_post_call_report(session, timeout: float | None = None) -> dict:
    """One Codex call over the finished transcript. Returns the parsed report,
    or {"error": ...} — a failed report must not 500 on the caller."""
    if not session.lines:
        return {"error": "transcrição vazia: nada a relatar nesta reunião"}
    prompt = build_prompt(session)
    try:
        result, _meta = await generate(INSTRUCTION, prompt, timeout=timeout)
    except LLMError as e:
        log.error("post-call LLM call failed: %s", e)
        return {"error": str(e)[:300]}
    report = parse_report(result)
    log.info("post-call report for %s over %d lines", session.meeting_id, len(session.lines))
    return {"meeting": session.meeting_id, "lines": len(session.lines), **report}
