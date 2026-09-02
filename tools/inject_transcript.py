"""Modo de teste SEM Google Meet: injeta uma conversa de exemplo no backend.

Exercita transcrição-já-pronta -> contexto -> Codex CLI -> SINAL/FAÇA/DIGA -> UI.
Não precisa de áudio, microfone, navegador nem GPU.

Uso:
    ./backend/run.sh                                  # noutro terminal
    .venv/bin/python tools/inject_transcript.py       # conversa de exemplo
    .venv/bin/python tools/inject_transcript.py --file minha_conversa.txt

Abra http://localhost:5005/?meeting=test para ver a orientação aparecer.
O arquivo de conversa tem uma linha por fala, no formato "lead: ..." ou
"tiago: ...".
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# Uma objeção clássica: orçamento apertado + ceticismo com consultoria.
SAMPLE = [
    ("tiago", "Obrigado pelo tempo. Antes de eu falar qualquer coisa, me conta como está a operação de vocês hoje."),
    ("lead", "A gente é uma construtora de médio porte, faz uns doze anos. Hoje quase tudo é obra privada."),
    ("tiago", "E hoje vocês já participam de licitação de alguma forma?"),
    ("lead", "Olha, a gente até tentou duas vezes o ano passado, mas foi bem frustrante. Perdemos as duas."),
    ("tiago", "Entendi. E o que vocês acham que aconteceu?"),
    ("lead", "Sinceramente não sei, acho que foi a documentação. Mas eu preciso ser honesto com você, o orçamento aqui está bem apertado esse ano. E a diretoria está meio cética com consultoria, já contratamos uma antes e não deu em nada."),
    ("tiago", "Faz sentido. Deixa eu entender melhor o que a consultoria anterior entregou."),
    ("lead", "Eles fizeram um diagnóstico bonito, um relatório enorme, e depois sumiram. Ficou tudo no papel. Por isso quando você falou em contrato anual eu já fiquei com o pé atrás, sabe."),
]


def parse_file(path: str) -> list[tuple[str, str]]:
    lines = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            who, _, text = raw.partition(":")
            if not text.strip():
                continue
            lines.append((who.strip().lower(), text.strip()))
    return lines


def post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:5005")
    ap.add_argument("--meeting", default="test")
    ap.add_argument("--file", help="conversa em texto (uma fala por linha)")
    ap.add_argument("--delay", type=float, default=4.0,
                    help="segundos entre falas (padrão 4, ritmo de conversa real)")
    args = ap.parse_args()

    convo = parse_file(args.file) if args.file else SAMPLE
    if not convo:
        print("nada para injetar", file=sys.stderr)
        return 1

    try:
        with urllib.request.urlopen(f"{args.url}/health", timeout=10) as r:
            health = json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"backend não respondeu em {args.url} ({e}). Rode ./backend/run.sh primeiro.",
              file=sys.stderr)
        return 1
    print(f"backend ok — provider={health['provider']} cmd={health['llm_cmd']}")
    print(f"abra {args.url}/?meeting={args.meeting} para ver a orientação\n")

    for who, text in convo:
        source = "mic" if who.startswith(("tiago", "eu", "you", "mic")) else "system"
        post(f"{args.url}/api/inject",
             {"meeting": args.meeting, "source": source, "text": text})
        label = "Tiago" if source == "mic" else "Lead "
        print(f"  {label} | {text[:90]}")
        time.sleep(args.delay)

    print("\nfalas injetadas. O copiloto responde em alguns segundos — "
          "veja o log do backend e a página.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
