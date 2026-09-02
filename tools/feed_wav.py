"""Modo de teste com áudio real, SEM Google Meet: toca um WAV no backend.

Envia um arquivo WAV como se fosse o áudio do lead, em ritmo de tempo real,
pelo mesmo WebSocket que o navegador usa. Exercita o pipeline INTEIRO:
VAD -> faster-whisper -> transcrição -> Codex CLI -> SINAL/FAÇA/DIGA -> UI.

Uso:
    .venv/bin/python tools/feed_wav.py conversa.wav
    .venv/bin/python tools/feed_wav.py conversa.wav --source mic

Qualquer WAV PCM serve (mono/estéreo, qualquer taxa); é convertido para
16 kHz mono aqui. Não precisa de ffmpeg. Para gravar um: Gravador de Voz do
Windows, exporte/converta para .wav.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave

import numpy as np

try:
    from websockets.sync.client import connect
except ImportError:  # pragma: no cover
    print("faltando 'websockets' — ele vem com uvicorn[standard]; "
          "use o python do venv: .venv/bin/python tools/feed_wav.py ...", file=sys.stderr)
    raise

TARGET_RATE = 16000
CHUNK_MS = 80


def read_wav_16k_mono(path: str) -> bytes:
    """Any 16-bit PCM WAV -> 16 kHz mono Int16 bytes, using numpy only.

    (stdlib `audioop` would do this too, but it is gone in Python 3.13.)
    """
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: preciso de WAV PCM 16-bit (este é {w.getsampwidth()*8}-bit)")
        channels, rate = w.getnchannels(), w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    samples = samples.astype(np.float32)
    if rate != TARGET_RATE:
        n_out = int(len(samples) * TARGET_RATE / rate)
        samples = np.interp(
            np.arange(n_out) * (rate / TARGET_RATE), np.arange(len(samples)), samples)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wav")
    ap.add_argument("--url", default="ws://127.0.0.1:5005")
    ap.add_argument("--meeting", default="test")
    ap.add_argument("--source", default="system", choices=["system", "mic"],
                    help="system = fala do lead (padrão), mic = fala do Tiago")
    ap.add_argument("--fast", action="store_true",
                    help="envia o mais rápido possível em vez de tempo real")
    ap.add_argument("--wait", type=float, default=60.0,
                    help="segundos de espera pelas transcrições no fim")
    args = ap.parse_args()

    pcm = read_wav_16k_mono(args.wav)
    secs = len(pcm) / 2 / TARGET_RATE
    print(f"{args.wav}: {secs:.1f}s de áudio -> 16 kHz mono")
    print(f"abra http://localhost:5005/?meeting={args.meeting} para ver a orientação")

    # Silêncio no fim: é ele que faz o VAD fechar a última fala e transcrever
    # ENQUANTO ainda estamos conectados. Sem isso o backend só fecha a fala no
    # disconnect, quando já não há ninguém para receber a transcrição.
    pcm += b"\x00" * (TARGET_RATE * 2 * 2)  # 2 s

    chunk = TARGET_RATE * 2 * CHUNK_MS // 1000
    url = f"{args.url}/ws/audio?source={args.source}&meeting={args.meeting}"
    got = 0
    with connect(url, max_size=None) as ws:
        for i in range(0, len(pcm), chunk):
            ws.send(pcm[i:i + chunk])
            if not args.fast:
                time.sleep(CHUNK_MS / 1000)
        # Espera as transcrições chegarem (whisper na CPU leva alguns segundos).
        deadline = time.time() + args.wait
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=deadline - time.time()))
            except (TimeoutError, ValueError):
                break
            if msg.get("type") == "transcript":
                got += 1
                print(f"  transcrito: {msg['text']}")
            else:
                print(f"  <- {msg}")
    if not got:
        print("\nNENHUMA transcrição voltou — áudio silencioso demais? "
              "baixe VAD_THRESHOLD em meetcfg.env, ou aumente --wait.", file=sys.stderr)
        return 1
    print(f"\n{got} fala(s) transcrita(s). O copiloto responde em seguida — "
          "veja o log do backend e a página.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
