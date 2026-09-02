"""Runtime configuration — environment variables only.

Trimmed from live-meeting-assistant's config.py (MIT, Ben Linford): the
settings.json override layer and its live-edit API were dropped, because the
MVP is configured once in `meetcfg.env` and restarted.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("meetcfg.config")


@dataclass(frozen=True)
class Field:
    attr: str
    env: str
    type: type
    default: object


FIELDS: list[Field] = [
    # identity
    Field("user_name", "USER_NAME", str, "Tiago"),
    Field("host", "MEETCFG_HOST", str, "0.0.0.0"),
    Field("port", "MEETCFG_PORT", int, 5005),
    # LLM: the only provider is a shell command (bin/codex_llm.sh -> codex exec)
    Field("custom_llm_cmd", "CUSTOM_LLM_CMD", str, "bin/codex_llm.sh"),
    Field("suggest_timeout_s", "SUGGEST_TIMEOUT_S", float, 90.0),
    # commercial context file, prepended to every copilot prompt
    Field("sales_context_path", "SALES_CONTEXT_PATH", str, "sales_context.md"),
    # whisper (faster-whisper, local). CPU/int8 by default: no GPU assumed.
    Field("whisper_model", "WHISPER_MODEL", str, "small"),
    Field("whisper_device", "WHISPER_DEVICE", str, "cpu"),
    Field("whisper_compute_type", "WHISPER_COMPUTE_TYPE", str, "int8"),
    Field("whisper_language", "WHISPER_LANGUAGE", str, "pt"),
    Field("whisper_beam_size", "WHISPER_BEAM_SIZE", int, 1),
    Field("whisper_watchdog_s", "WHISPER_WATCHDOG_S", float, 120.0),
    # endpointing (energy VAD)
    Field("vad_threshold", "VAD_THRESHOLD", float, 0.008),
    Field("endpoint_silence_ms", "ENDPOINT_SILENCE_MS", int, 800),
    Field("min_utt_ms", "MIN_UTT_MS", int, 350),
    Field("max_utt_ms", "MAX_UTT_MS", int, 15000),
    Field("preroll_ms", "PREROLL_MS", int, 250),
    # copilot cadence
    Field("suggest_enabled", "SUGGEST_ENABLED", bool, True),
    Field("suggest_min_interval_s", "SUGGEST_MIN_INTERVAL_S", float, 15.0),
    # only NEW speech from the lead counts toward this threshold
    Field("suggest_min_new_chars", "SUGGEST_MIN_NEW_CHARS", int, 80),
    # rolling window handed to the model (~60-120s of talk)
    Field("suggest_transcript_chars", "SUGGEST_TRANSCRIPT_CHARS", int, 2500),
    # echo suppression (lead's voice leaking from speakers into the mic)
    Field("echo_suppress", "ECHO_SUPPRESS", bool, True),
    Field("echo_window_s", "ECHO_WINDOW_S", float, 12.0),
    Field("echo_similarity", "ECHO_SIMILARITY", float, 0.82),
]


def _coerce(field: Field, raw: object):
    if raw is None:
        return field.default
    if field.type is bool:
        return str(raw).strip().lower() not in ("false", "0", "no", "off", "")
    try:
        return field.type(raw)
    except (TypeError, ValueError):
        log.warning("bad value for %s: %r; using default", field.env, raw)
        return field.default


class Settings:
    sample_rate: int = 16000  # fixed by the browser worklet


settings = Settings()


def reload() -> None:
    for f in FIELDS:
        setattr(settings, f.attr, _coerce(f, os.getenv(f.env)))


reload()
