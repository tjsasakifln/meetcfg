"""Parser test for the SINAL/FAÇA/DIGA reply format (no Codex call needed).

Usage:
    python3 backend/tools/parse_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.copilot.engine import parse_advice  # noqa: E402

FAILS = 0


def check(name, got, want):
    global FAILS
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    FAILS += 0 if ok else 1


def main() -> None:
    print("well-formed reply")
    a = parse_advice(
        'SINAL: Lead revela objeção de orçamento\n'
        'FAÇA: Investigue o fracasso anterior antes de defender preço\n'
        'DIGA: "O que a consultoria anterior prometeu e não entregou?"'
    )
    check("sinal", a["sinal"], "Lead revela objeção de orçamento")
    check("faca", a["faca"], "Investigue o fracasso anterior antes de defender preço")
    check("diga (quotes stripped)", a["diga"],
          "O que a consultoria anterior prometeu e não entregou?")

    print("silence markers -> None (previous advice must stay on screen)")
    for raw in ("--", "  --  ", "—", '"--"', "```\n--\n```"):
        check(f"silence {raw!r}", parse_advice(raw), None)
    check("empty -> None", parse_advice(""), None)

    print("tolerates markdown bold, no cedilla, fenced block")
    a = parse_advice("```\n**SINAL:** hesitação\n**FACA:** pergunte o prazo\n"
                     "**DIGA:** “Quando vocês decidem?”\n```")
    check("bold sinal", a["sinal"], "hesitação")
    check("no-cedilla FACA", a["faca"], "pergunte o prazo")
    check("smart quotes stripped", a["diga"], "Quando vocês decidem?")

    print("partial reply still shows what came through")
    a = parse_advice("DIGA: Pergunte o orçamento disponível")
    check("diga only", a["diga"], "Pergunte o orçamento disponível")
    check("sinal empty", a["sinal"], "")

    print("unparseable non-empty text is surfaced, not swallowed")
    a = parse_advice("Acho que você deveria perguntar sobre o prazo.")
    check("fallback keeps text", a["faca"], "Acho que você deveria perguntar sobre o prazo.")

    if FAILS:
        print(f"\n{FAILS} FAILURES")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
