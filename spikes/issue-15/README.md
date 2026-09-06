# Spike #15 — captura dos dois lados de ligação celular no Windows

## Veredito

`NO_GO_INSUFFICIENT_REAL_WINDOWS_EVIDENCE`

Este resultado é um `NO_GO` do gate, não uma prova de inviabilidade técnica. O
host real foi inspecionado e os componentes locais foram exercitados, mas não
houve uma chamada celular/SIM consentida com seleção interativa da fonte de
captura. Por isso não há evidência de dois streams reais, transcrição dos dois
lados, eco real, conselho real nem três cold starts.

Nenhuma chamada foi iniciada por este spike. Nenhum áudio, transcrição, nome,
telefone ou conteúdo de conversa foi gravado ou versionado.

## Ambiente observado

- Windows 11 Home Single Language `10.0.26200` (build `26200`), via terminal
  WSL2.
- Phone Link `1.26071.164.0`; processo aberto durante o preflight.
- Edge `152.0.4191.62`; Chrome `152.0.7977.76`.
- O número de endpoints ativos mudou de `2` para `4` após abrir o Phone Link,
  com perfil Hands-Free presente, e voltou a `2` sem uma chamada ativa. Nomes e
  IDs de dispositivos não foram registrados.
- O backend subiu com `faster-whisper tiny`, CPU/int8, em memória. O modelo
  configurado `small` não estava em cache e seu primeiro download não concluiu
  durante o intervalo observado; isso é preparação de ambiente, não resultado
  de captura.

Os valores brutos sem conteúdo estão em [metrics.json](metrics.json).

## Experimentos, na ordem exigida

### 1. Browser / Edge / Chrome

O probe em `localhost` confirmou, nos dois navegadores, contexto seguro,
`getUserMedia`, `getDisplayMedia`, `AudioWorkletNode`, `CaptureController` e as
constraints usuais de áudio. O backend local ficou saudável e uma página real
de captura foi aberta.

Não foi observada a conclusão do seletor de tela/áudio nem um par de tracks de
áudio entregue ao MeetCFG. A especificação exige escolha do usuário a cada
chamada de `getDisplayMedia`; o pedido de áudio é apenas uma preferência e o
navegador pode retornar nenhum track de áudio. Portanto presença da API não foi
contada como captura.

Resultado: `BROWSER_CAPTURE_GRANT_NOT_OBSERVED`.

### 2. WASAPI / application loopback + microfone

O build `26200` satisfaz o requisito de plataforma do process loopback da
Microsoft (build `20348` ou posterior). O Phone Link também expôs
temporariamente um perfil Hands-Free, mas não havia chamada/render stream ativo
quando o teste chegou a esta etapa.

Não foi executada captura silenciosa para fabricar um resultado: a amostra
oficial informa que process loopback retorna silêncio quando o processo alvo
não possui stream de render. Sem uma chamada ativa não seria possível distinguir
API funcional de PID/process tree incorreto ou roteamento especial do Phone
Link.

Resultado: `WASAPI_LIVE_CAPTURE_NOT_EXECUTED`.

### 3. Helper mínimo

Não foi implementado um helper de captura. Os mecanismos 1 e 2 não foram
comprovados insuficientes; faltou o evento físico que permitiria medi-los.
Avançar para um terceiro mecanismo violaria a ordem do spike.

Foi versionado apenas um helper de preflight sem áudio, [preflight.ps1](preflight.ps1),
e um probe de APIs do browser, [browser-api-probe.html](browser-api-probe.html).

Resultado: `CAPTURE_HELPER_DEFERRED_BY_SEQUENCE`.

## Matriz de opções

| Opção | Pré-requisitos e permissão | Separação de papéis | Eco/contaminação | Estabilidade e latência | Distribuição / rollback | Resultado |
| --- | --- | --- | --- | --- | --- | --- |
| Browser: mic + display/system audio | Edge/Chrome; HTTPS ou localhost; permissão de mic; escolha humana e áudio marcado em toda captura | Mic pode mapear para `operator`; o segundo track pode mapear para `counterparty` somente após provar que contém a chamada | Captura de sistema pode incluir outros apps; headset reduz vazamento acústico, mas não prova isolamento | Zero binário adicional; perda/troca de track deve ser observada | Reusar WebAudio/WebSocket atual; rollback é parar tracks/fechar sockets | APIs presentes; captura real não concedida/medida |
| WASAPI process loopback do Phone Link + mic | Windows build >=20348; PID/process tree correto; chamada com render ativo; consentimento de mic | Candidato mais forte a isolar `counterparty` por processo e `operator` por endpoint de captura | Exclui outros processos por desenho; eco e eventual áudio do próprio operador dentro do render ainda precisam ser medidos | Pacotes nativos e QPC permitem métricas; comportamento específico do Phone Link é desconhecido | Exige executável one-shot; rollback é encerrar o processo | Plataforma elegível; chamada/captura não executada |
| WASAPI loopback do endpoint + mic | Endpoint de render correto e chamada ativa | Dois inputs físicos, mas o stream remoto é mix do endpoint, não da aplicação | Outros sons do endpoint contaminam a contraparte | API madura, porém sensível a device switch/default communications | Helper one-shot; sem driver | Não executado; fallback de menor isolamento |
| Terceiro helper/mecanismo | Somente após falha reproduzível dos dois anteriores | Não avaliado | Não avaliado | Não avaliado | Proibido neste resultado | Adiado |

## Evidência local independente (não satisfaz o gate real)

- `faster-whisper tiny` transcreveu dois segundos de silêncio gerado em memória:
  texto vazio, `6605 ms` incluindo cold model load.
- Self-tests sintéticos de supressão de eco e parser: exit `0`.
- `bin/codex_llm.sh`, efêmero/read-only e sem API key, recebeu um caso sintético
  sem fala nova e devolveu o silêncio correto `--` em `11596 ms`.

Esses checks demonstram prontidão parcial do pipeline, não
`WHISPER_TWO_SIDES=YES`, `ECHO_RESULT=PASS` nem conselho útil numa chamada.

## Fluxo candidato para a próxima execução real

```text
celular/SIM ──Bluetooth/Phone Link──► render de chamada no Windows
                                         │
                                         ├─ browser system audio OU WASAPI process loopback
                                         │     role=counterparty
headset Windows ──microfone───────────────┤
                                               role=operator
                                         ▼
                                  adapter do #14
                                  PCM s16le/16 kHz/mono
                                         ▼
                           faster-whisper local → core único
                                         ▼
                              bin/codex_llm.sh → conselho/--
```

As setas de captura são candidatas, não evidência de funcionamento.

## Contrato recomendado para #14/#16

Não implementar este contrato no #15. O #14 deve expor um adapter que entregue:

| Campo | Valor para telefone |
| --- | --- |
| `conversation_channel` | `phone` |
| `role` | `operator` ou `counterparty`; nunca inferido do canal comercial |
| `physical_source` | identificador opaco e estável do adapter, por exemplo `windows.microphone` / `windows.phone_link_process_loopback` |
| `encoding` | `pcm_s16le` |
| `sample_rate_hz` | `16000` |
| `channels` | `1` |
| `frame_duration_ms` | `80` no wire atual; aceitar framing variável sem alterar o relógio de amostras |
| `payload` | bytes PCM binários, sem WAV/header |
| `lifecycle` | `source_open` → zero ou mais `audio_frame` → `source_lost|source_closed`; flush final somente em memória |
| `health` | monotonic timestamp, sample counter, dropped-frame counter, last-frame age e reason code; nunca texto/áudio |

Invariantes para #14/#16:

1. somente `counterparty` novo pode disparar conselho;
2. `operator` e `counterparty` permanecem separados desde a captura;
3. source desconhecido/duplicado falha fechado, sem criar terceiro papel;
4. troca/perda de dispositivo gera health event e não remapeia papel;
5. buffers, transcrição e conselho vivem apenas em memória;
6. logs contêm somente métricas e reason codes. O log INFO atual de supressão de
   eco inclui fragmento de texto e deve permanecer desabilitado ou ser redigido
   durante qualquer novo teste real.

## Próxima execução reproduzível

1. Abrir PowerShell e executar `powershell -ExecutionPolicy Bypass -File .\spikes\issue-15\preflight.ps1`.
   Para repetir o probe do browser, servir a raiz do repo com
   `python3 -m http.server 18766` e abrir
   `http://localhost:18766/spikes/issue-15/browser-api-probe.html`.
2. Confirmar `phone_link.running=true`, `audio.has_hands_free=true` e uma
   contraparte consentida disponível.
3. Subir o backend com logging de conteúdo desabilitado e abrir o frontend em
   `http://localhost:5005`.
4. Experimento 1: escolher a fonte no seletor, confirmar dois tracks, fazer a
   chamada SIM curta e registrar somente métricas/reason codes. Se e somente se
   falhar de modo reproduzível, registrar o primeiro erro e seguir ao WASAPI.
5. Experimento 2: usar a amostra process-loopback one-shot contra o PID/process
   tree do Phone Link, substituindo a escrita WAV por buffer em memória antes de
   qualquer fala real. Somente após isso decidir se um helper próprio é necessário.
6. Após o primeiro sucesso, encerrar o processo, repetir três chamadas/cold
   starts e testar perda/retorno da fonte e device switch.

## Fontes primárias

- [W3C Screen Capture Working Draft](https://www.w3.org/TR/screen-capture/)
- [Microsoft: ActivateAudioInterfaceAsync](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync)
- [Microsoft: Application loopback sample](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/ApplicationLoopback)
- [Microsoft: WASAPI loopback recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)
- [Microsoft Support: Phone Link calls](https://support.microsoft.com/en-us/windows/apps/make-and-receive-phone-calls-from-your-pc)
