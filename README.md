# meetcfg — copiloto comercial em tempo real da CONFENGE

Ouve uma reunião no Google Meet, transcreve localmente e, quando o lead diz algo
que pede intervenção, mostra **uma** orientação curta na tela:

```
SINAL: Lead revela objeção de orçamento e ceticismo com consultoria
FAÇA:  Investigue o fracasso anterior antes de defender preço
DIGA:  "O que a consultoria anterior prometeu e não entregou?"
```

Quando não há nada a fazer, o copiloto fica em silêncio e a orientação anterior
permanece na tela, apagada.

```
Google Meet (Chrome no Windows)
   └─ áudio da aba + microfone ──WebSocket PCM 16 kHz──►  backend no WSL
                                                            ├─ faster-whisper (local)
                                                            ├─ contexto recente (~60–120 s)
                                                            └─ codex exec  ──►  SINAL / FAÇA / DIGA
```

**Nada é pago e nada sai de máquinas suas exceto a chamada ao Codex.** Sem
OpenAI API key: a autenticação é a do Codex CLI já logado pela sua assinatura.
Sem STT na nuvem: `faster-whisper` roda local. Sem banco de dados: transcrição e
áudio vivem só na memória do processo e morrem com ele.

---

## Pré-requisitos

| O quê | Onde | Como conferir |
| --- | --- | --- |
| Python 3.11+ | WSL | `python3 --version` |
| Codex CLI logado | WSL | `codex --version` e `codex exec --skip-git-repo-check "diga ok"` |
| Chrome ou Edge | Windows | — |
| ~1 GB livre | WSL | o modelo whisper baixa no primeiro boot |

GPU NVIDIA é opcional. O padrão é **CPU**.

## Instalação (uma vez)

No terminal do WSL:

```bash
cd ~/code/meetcfg
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cp meetcfg.env.example meetcfg.env
cp sales_context.example.md sales_context.md
```

Edite `meetcfg.env` se quiser (o padrão já funciona) e escreva o seu
`sales_context.md` — quem é a empresa, qual o objetivo da reunião, como orientar
e o que detectar. Ele é relido a cada rodada, então dá para ajustar durante a
reunião sem reiniciar nada, e é **ignorado pelo git**: é estratégia comercial,
não pertence a um repositório público. O modelo está em
[`sales_context.example.md`](sales_context.example.md).

## Rodar

```bash
cd ~/code/meetcfg
./backend/run.sh
```

O primeiro boot baixa o modelo whisper (`small`, ~500 MB) e demora alguns
minutos; os próximos são rápidos. Quando aparecer `Application startup complete`,
abra no **Chrome do Windows**:

```
http://localhost:5005
```

Na página:

1. Deixe marcados **Meu microfone** e **Áudio da reunião (lead)**.
2. Clique **Iniciar**.
3. Autorize o microfone.
4. Na janela de compartilhamento, escolha a **aba do Google Meet** e
   **marque “Compartilhar áudio da guia”** — sem isso o lead não é capturado.
5. Use fones de ouvido.

A partir daí o copiloto roda sozinho: a cada ~15 s, **se e somente se o lead
falou algo novo**, ele consulta o Codex e atualiza a orientação. Medido nesta
máquina, o Codex responde em 6–10 s com `CODEX_EFFORT=low`, então a cadência
real fica em ~15–25 s por orientação. Você nunca
precisa clicar em nada. O botão **Orientar agora** força uma rodada.

## Modo de teste (sem Google Meet)

Dois modos, ambos com o backend rodando noutro terminal.

**Só texto** — valida transcrição-pronta → contexto → Codex → orientação → tela.
Não precisa de áudio nem navegador:

```bash
.venv/bin/python tools/inject_transcript.py
```

Injeta uma conversa de exemplo (objeção de orçamento + ceticismo com
consultoria) no ritmo de uma conversa real. Abra
`http://localhost:5005/?meeting=test` para ver a orientação aparecer.
Para usar sua própria conversa:

```bash
.venv/bin/python tools/inject_transcript.py --file minha_conversa.txt
```

com uma fala por linha, no formato `lead: ...` / `tiago: ...`.

**Com áudio real** — valida o pipeline inteiro, incluindo VAD e whisper:

```bash
.venv/bin/python tools/feed_wav.py conversa.wav
```

Manda um WAV pelo mesmo WebSocket que o navegador usa, em tempo real. Serve
qualquer WAV PCM 16-bit (mono/estéreo, qualquer taxa) — grave um no **Gravador
de Voz do Windows** e salve/converta para `.wav`. Não precisa de ffmpeg.

## Testes

```bash
.venv/bin/python backend/tools/parse_selftest.py     # formato SINAL/FAÇA/DIGA e "--"
.venv/bin/python backend/tools/echo_selftest.py      # supressão de eco mic↔lead
.venv/bin/python backend/tools/watchdog_selftest.py  # watchdog de transcrição travada
```

Nenhum deles chama o Codex nem carrega o whisper — rodam em segundos.

## Configuração

Tudo em `meetcfg.env` (veja `meetcfg.env.example`). Os que importam:

| Variável | Padrão | Para quê |
| --- | --- | --- |
| `SUGGEST_MIN_INTERVAL_S` | `15` | intervalo mínimo entre chamadas ao Codex |
| `SUGGEST_MIN_NEW_CHARS` | `80` | quanta fala **nova do lead** dispara uma rodada |
| `SUGGEST_TRANSCRIPT_CHARS` | `2500` | tamanho do trecho recente enviado (~60–120 s) |
| `WHISPER_MODEL` | `small` | `base` é mais rápido e pior; `medium` o inverso |
| `WHISPER_DEVICE` | `cpu` | `cuda` se você tiver GPU NVIDIA no WSL |
| `CODEX_EFFORT` | `low` | esforço de raciocínio do Codex |
| `VAD_THRESHOLD` | `0.008` | sensibilidade do detector de fala |

## Troubleshooting

**“Iniciar” não pede permissão / `navigator.mediaDevices` indefinido**
O navegador só libera captura em contexto seguro. `http://localhost:5005` **é**
um contexto seguro; `http://172.x.x.x:5005` (IP do WSL) **não é**. Se o
`localhost` do Windows não alcançar o WSL, o conserto é o encaminhamento de
porta do WSL2 (`wsl --shutdown` no PowerShell costuma resolver), não trocar o
endereço.

**A transcrição do lead não aparece**
Você compartilhou a aba sem marcar “Compartilhar áudio da guia”. Clique
**Parar**, **Iniciar** de novo e marque a caixa.

**Aparece a fala do lead duas vezes, uma como “Tiago”**
É o som do alto-falante voltando pelo microfone. Use fones. A supressão de eco
já cobre a maior parte disso sozinha.

**Nenhuma orientação aparece**
Confira o log do backend. Se o Codex falhar, o erro aparece lá e no canto da
página. Teste o bridge isolado:

```bash
echo "Lead: o orçamento está apertado esse ano." | LLM_SYSTEM_PROMPT="Responda apenas: OK" bin/codex_llm.sh
```

**Orientações demais / de menos**
Suba `SUGGEST_MIN_NEW_CHARS` (menos) ou baixe (mais) em `meetcfg.env`.

**Whisper lento demais na CPU**
Troque para `WHISPER_MODEL=base` em `meetcfg.env` e reinicie.

## Privacidade e segurança

Não há autenticação: rode só na sua máquina. Áudio, transcrição e orientações
ficam na memória do processo e somem quando ele para — o backend não grava
nada em disco, e o Codex roda com `--ephemeral` (sem arquivo de sessão).
A única saída de rede é a chamada ao Codex, que recebe o `sales_context.md` mais
o trecho recente da conversa.

## Créditos e licença

MIT — veja [LICENSE](LICENSE).

Este projeto é derivado de
**[tfp24601/live-meeting-assistant](https://github.com/tfp24601/live-meeting-assistant)**
(MIT, © 2026 Ben Linford), do qual reaproveita a captura de áudio no navegador,
o worklet PCM, o motor de transcrição sobre faster-whisper, a supressão de eco,
o watchdog e a disciplina de disparo (debounce + single-flight). Os arquivos
mantidos ou adaptados trazem a atribuição no cabeçalho. Foram removidos: RAG /
qdrant / embeddings, ingestão de documentos, verificação de locutor e
enrollment de voz, deep dive, tela de configurações, exportação e todos os
demais provedores de LLM.
