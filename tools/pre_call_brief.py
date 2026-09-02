"""Briefing de 30 segundos antes da chamada, a partir do contexto estruturado.

Lê um documento CONFENGE_SALES_CONTEXT/1.0, valida, e imprime uma tela com o que
você precisa saber antes de discar. Determinístico: não chama o Codex, não chama
o backend, não precisa de nada rodando.

Contexto inválido não vira briefing pela metade — imprime o motivo e sai com 1.

Uso:
    .venv/bin/python tools/pre_call_brief.py --file fixtures/sales_context_inbound.json
    cat contexto.json | .venv/bin/python tools/pre_call_brief.py -
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.copilot.context import (  # noqa: E402
    CHANNEL_LABELS, engagement_type, load_sales_context_v1, never_assert_list,
)

# The loader logs its rejection reason; here that reason is printed to stderr by
# main(), so silence the logger to avoid saying it twice.
logging.getLogger("meetcfg").addHandler(logging.NullHandler())


def _strs(value) -> list[str]:
    return [s.strip() for s in value if isinstance(s, str) and s.strip()] \
        if isinstance(value, list) else []


def render_brief(ctx: dict) -> str:
    """As 8 seções, sempre todas, mesmo com contexto magro."""
    out: list[str] = []

    def section(title: str, lines: list[str]) -> None:
        out.append(title)
        out.extend(f"  {ln}" for ln in lines)
        out.append("")

    company = ctx.get("company") or {}
    quem = [company.get("name", "")]
    cnpj = company.get("cnpj")
    if isinstance(cnpj, str) and cnpj.strip():
        quem.append(f"CNPJ {cnpj.strip()}")
    engagement = ctx.get("engagement") or {}
    eng = [t for t in [engagement_type(ctx)] if t]
    detail = engagement.get("detail")
    if isinstance(detail, str) and detail.strip():
        eng.append(detail.strip())
    if eng:
        quem.append("Engajamento: " + " — ".join(eng))
    section("QUEM É", quem)

    channel = ctx.get("acquisition_channel", "")
    label = CHANNEL_LABELS.get(channel, "")
    section("DE ONDE VEIO", [f"{channel}" + (f" — {label}" if label else ""),
                             f"Contexto levantado em {ctx.get('source_as_of', '?')} "
                             f"({ctx.get('provenance', 'origem não declarada')})"])

    intent = ctx.get("intent") or {}
    agora = [intent.get("kind", "")]
    reason = intent.get("reply_reason")
    if isinstance(reason, str) and reason.strip():
        agora.append(reason.strip())
    section("POR QUE AGORA", agora)

    seen: list[str] = []
    for tp in (ctx.get("touchpoints") or []):
        if not isinstance(tp, dict):
            continue
        summary = tp.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        head = " · ".join(b.strip() for b in (tp.get("at"), tp.get("channel"))
                          if isinstance(b, str) and b.strip())
        item = f"{head} — {summary.strip()}" if head else summary.strip()
        delivered = tp.get("delivered")
        if isinstance(delivered, str) and delivered.strip():
            item += f"\n     JÁ RECEBEU: {delivered.strip()} (não venda isso de novo)"
        seen.append(item)
    section("O QUE JÁ VIU/RECEBEU", seen or ["nada registrado — trate como primeiro contato"])

    facts = _strs(ctx.get("public_facts")) + _strs(ctx.get("opportunities"))
    fortes = facts[:3]
    while len(fortes) < 3:
        fortes.append("(sem terceiro fato levantado — pergunte em vez de afirmar)")
    section("3 FATOS FORTES", fortes)

    section("2 PERGUNTAS", _questions(ctx))

    limits = never_assert_list(ctx)
    section("1 LIMITE QUE NÃO PODE SER AFIRMADO",
            [limits[0] if limits
             else "nenhum limite declarado — trate tudo como não verificado"])

    offer = ctx.get("offer") or {}
    passo = [offer.get("next_state", "")]
    current = offer.get("current")
    if isinstance(current, str) and current.strip():
        passo.insert(0, f"Oferta em jogo: {current.strip()}")
    section("PRÓXIMO PASSO RECOMENDADO", passo)

    return "\n".join(out).rstrip() + "\n"


def _questions(ctx: dict) -> list[str]:
    """Duas perguntas de descoberta, por template — sem LLM.

    Genéricas quando o contexto é magro, e tudo bem: o valor está em sair da
    chamada tendo perguntado, não em a pergunta ser brilhante.
    """
    qs: list[str] = []
    opportunities = _strs(ctx.get("opportunities"))
    if opportunities:
        qs.append(f'"{opportunities[0]}" — isso bate com o que vocês vivem hoje, '
                  "ou eu entendi errado?")
    engagement = ctx.get("engagement") or {}
    detail = engagement.get("detail")
    if len(opportunities) > 1:
        qs.append(f'Sobre "{opportunities[1]}": o que já tentaram aí e não funcionou?')
    elif isinstance(detail, str) and detail.strip():
        qs.append(f'Sobre "{detail.strip()}": o que precisa acontecer para isso virar '
                  "prioridade?")
    while len(qs) < 2:
        qs.append("Como isso funciona hoje de ponta a ponta, e onde trava?"
                  if not qs else
                  "Se nada mudar até o fim do ano, qual é o custo disso para vocês?")
    return qs[:2]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="documento CONFENGE_SALES_CONTEXT/1.0 (JSON); "
                                   "use - para ler do STDIN")
    ap.add_argument("path", nargs="?", help="idem, posicional")
    args = ap.parse_args()

    src = args.file or args.path
    if not src:
        print("informe o contexto: --file caminho.json (ou - para STDIN)", file=sys.stderr)
        return 1
    if src == "-":
        # The loader takes a path (fail-closed on unreadable files), so STDIN
        # gets a temp file instead of a second, looser parsing path.
        raw = sys.stdin.read()
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8",
                                         delete=False) as fh:
            fh.write(raw)
            tmp = fh.name
        try:
            ctx, reason = load_sales_context_v1(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)
    else:
        ctx, reason = load_sales_context_v1(src)

    if ctx is None:
        print(reason, file=sys.stderr)
        return 1
    sys.stdout.write(render_brief(ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
