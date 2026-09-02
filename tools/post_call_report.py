"""Relatório do pós-chamada: pede ao backend e imprime no stdout.

A transcrição só existe na memória do backend, então o relatório é gerado lá
(uma chamada ao Codex, depois que a reunião acabou — nunca durante). Este script
só dispara e mostra. Nada é gravado nem enviado a lugar nenhum: se quiser
guardar, redirecione a saída.

Uso:
    ./backend/run.sh                                        # noutro terminal
    .venv/bin/python tools/post_call_report.py --meeting test
    .venv/bin/python tools/post_call_report.py > relatorio.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

LABELS = [
    ("desfecho", "DESFECHO"),
    ("objecoes", "OBJEÇÕES"),
    ("compromissos", "COMPROMISSOS"),
    ("proxima_acao", "PRÓXIMA AÇÃO"),
    ("fatos_novos", "FATOS NOVOS"),
    ("follow_up", "FOLLOW-UP"),
]


def post(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def render(report: dict) -> str:
    out = []
    for key, label in LABELS:
        value = (report.get(key) or "").strip()
        out.append(label)
        for line in (value or "(nada)").splitlines():
            out.append(f"  {line.strip()}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:5005")
    ap.add_argument("--meeting", default="default")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="segundos de espera pelo Codex (padrão 180)")
    ap.add_argument("--json", action="store_true", help="imprime o JSON cru")
    args = ap.parse_args()

    try:
        report = post(f"{args.url}/api/postcall",
                      {"meeting": args.meeting}, args.timeout)
    except urllib.error.URLError as e:
        print(f"backend não respondeu em {args.url} ({e}). Rode ./backend/run.sh primeiro.",
              file=sys.stderr)
        return 1

    if report.get("error"):
        print(report["error"], file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"reunião {report.get('meeting', args.meeting)} — "
          f"{report.get('lines', 0)} falas\n")
    sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
