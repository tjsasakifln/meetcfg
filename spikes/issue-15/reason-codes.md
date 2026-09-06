# Reason codes do spike #15

| Reason code | Significado terminal/reproduzível |
| --- | --- |
| `NO_GO_INSUFFICIENT_REAL_WINDOWS_EVIDENCE` | O gate exigia chamada Windows/SIM real e um ou mais resultados só têm preflight/componente/simulação. Nunca converter em `GO`. |
| `BROWSER_CAPTURE_GRANT_NOT_OBSERVED` | APIs existem, mas não foi observada uma seleção humana concluída que retornasse tracks de mic e chamada. |
| `NO_ACTIVE_PHONE_CALL_AUDIO_SESSION` | Phone Link/processo pode estar aberto, porém não existe render de uma chamada ativa para medir. |
| `WASAPI_LIVE_CAPTURE_NOT_EXECUTED` | Build/API são elegíveis, mas nenhuma captura WASAPI foi feita durante chamada real. |
| `REAL_TWO_SIDE_TRANSCRIPTION_NOT_EXECUTED` | Whisper local não recebeu fala real separada de operador e contraparte. |
| `REAL_ECHO_NOT_EXECUTED` | Apenas supressão sintética foi exercitada; falso interlocutor/compromisso real continua desconhecido. |
| `COLD_START_NOT_EXECUTED` | Cold start não pode ser contado sem sucesso real anterior e nova chamada curta. |
| `CAPTURE_HELPER_DEFERRED_BY_SEQUENCE` | Helper de captura não pode ser criado enquanto browser/WASAPI não tiverem falha técnica reproduzível. |
| `WHISPER_SMALL_MODEL_WARMUP_STALLED` | Primeiro download/warm-up do modelo configurado não ficou pronto no intervalo observado; não implica falha do STT local. |
| `DEVICE_ENDPOINT_TRANSIENT` | A contagem/perfil de endpoint mudou sem recovery validado; não remapear papéis automaticamente. |
