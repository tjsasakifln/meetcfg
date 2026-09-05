"""In-memory meeting sessions.

Adapted from live-meeting-assistant's meeting.py (MIT, Ben Linford). The
in-person / group-meeting modes were dropped; echo suppression was kept
because the mic-vs-system split is what tells the copilot that *the lead*
just said something new.

Nothing is persisted: lines live in this process and die with it.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field

from .config import settings

log = logging.getLogger("meetcfg.meeting")

IDLE_PRUNE_S = 3600

#: The only two transcript channels. "mic" is Tiago, "system" is the lead.
#: Anything else is not a third speaker: it is rejected at ingest so it can
#: never satisfy the conversion state machine's "the other side spoke" test.
SOURCE_MIC = "mic"
SOURCE_SYSTEM = "system"
VALID_SOURCES = (SOURCE_MIC, SOURCE_SYSTEM)


def is_valid_source(source: object) -> bool:
    return isinstance(source, str) and source in VALID_SOURCES


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def _similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    # Endpointing can split an echo differently than the original, so full
    # containment of a substantial fragment counts as a match.
    if len(na) >= 15 and len(nb) >= 15:
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        if short in long_:
            return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


@dataclass
class Line:
    id: int
    source: str   # "mic" (Tiago) | "system" (the lead)
    text: str
    at: float     # server wall-clock (epoch seconds)
    # True when this line only exists because echo suppression retracted a
    # near-duplicate from the other stream. One of the two copies is spurious
    # and the heuristic cannot say which, so the survivor is not trustworthy
    # as an independent utterance from the other party.
    echo_derived: bool = False

    def speaker(self) -> str:
        return "Tiago" if self.source == "mic" else "Lead"


@dataclass
class MeetingSession:
    meeting_id: str
    lines: list[Line] = field(default_factory=list)
    listeners: set = field(default_factory=set)   # copilot websockets
    last_activity: float = field(default_factory=time.time)
    last_advice: dict | None = None
    engine: object = None  # set by main on creation (CopilotEngine)
    handraiser_id: str | None = None
    handraiser_context: dict | None = None
    handraiser_version: int = 0
    meeting_plan: dict | None = None
    conversion_state: dict | None = None
    _next_id: int = 0

    def ingest(self, source: str, text: str) -> tuple[str, int | None, int | None]:
        """Add a transcript line, suppressing speaker echo between the streams.

        The lead's voice leaking out of the speakers into the mic produces a
        near-duplicate of a "system" line on the "mic" stream. Whichever copy
        arrives second reveals the echo:
        - mic arrives second  -> the mic line IS the echo: suppress it
        - system arrives second -> the earlier mic line WAS the echo: accept
          the system line and retract the mic line

        Returns (action, new_line_id, retract_line_id) where action is one of
        "accept" | "suppress" | "accept_retract" | "reject".
        """
        self.last_activity = time.time()
        if not is_valid_source(source):
            # Not a known channel: never store it, never let it act as a
            # distinct speaker in the next-step board.
            log.warning("line rejected: unknown source %r", source)
            return ("reject", None, None)
        now = time.time()
        retract_id: int | None = None
        echo_derived = False

        if settings.echo_suppress:
            match = self._find_recent_match(source, text, now)
            if match is not None:
                if source == "mic":
                    log.info("echo suppressed (mic dup of system line %d): %.60s", match.id, text)
                    return ("suppress", None, None)
                # source == "system": the earlier mic line was the echo
                self.lines.remove(match)
                retract_id = match.id
                # The surviving system line is a copy of speech that already
                # arrived on the mic stream. Whoever really spoke it, it is
                # not a second, independent party agreeing.
                echo_derived = True
                log.info("echo retracted (mic line %d was dup of new system line): %.60s",
                         match.id, match.text)

        line = Line(id=self._next_id, source=source, text=text, at=now,
                    echo_derived=echo_derived)
        self._next_id += 1
        self.lines.append(line)
        self.refresh_conversion()
        if self.engine is not None:
            self.engine.poke()
        action = "accept_retract" if retract_id is not None else "accept"
        return (action, line.id, retract_id)

    def refresh_conversion(self) -> dict:
        """Rebuild next-step board from current lines. Flag off freezes the board."""
        from .copilot.conversion import CONVERSION_DISABLED, empty_state, rebuild_from_session
        enabled = bool(getattr(settings, "conversion_enabled", True))
        if not enabled:
            if self.conversion_state is None:
                frozen = empty_state()
                frozen["reason"] = CONVERSION_DISABLED
                self.conversion_state = frozen
            return self.conversion_state
        self.conversion_state = rebuild_from_session(self, enabled=True)
        return self.conversion_state

    def _find_recent_match(self, source: str, text: str, now: float) -> Line | None:
        other = "system" if source == "mic" else "mic"
        window = settings.echo_window_s
        best: Line | None = None
        best_score = 0.0
        for line in reversed(self.lines):
            if now - line.at > window:
                break
            if line.source != other:
                continue
            score = _similarity(line.text, text)
            if score > best_score:
                best, best_score = line, score
        if best is not None and best_score >= settings.echo_similarity:
            return best
        return None

    def lead_chars(self) -> int:
        """Characters spoken by the lead only — the copilot's trigger signal.

        Tiago talking (mic) must never on its own trigger a Codex call.
        """
        return sum(len(l.text) for l in self.lines if l.source == "system")

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload)
        dead = []
        for ws in list(self.listeners):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001 - a gone socket must not break the rest
                dead.append(ws)
        for ws in dead:
            self.listeners.discard(ws)


_sessions: dict[str, MeetingSession] = {}


def get_or_create(meeting_id: str) -> MeetingSession:
    _prune()
    s = _sessions.get(meeting_id)
    if s is None:
        s = MeetingSession(meeting_id=meeting_id)
        _sessions[meeting_id] = s
        log.info("meeting session created: %s", meeting_id)
    return s


def get(meeting_id: str) -> MeetingSession | None:
    return _sessions.get(meeting_id)


def reset_sessions() -> None:
    """Test hook: drop in-memory meetings. Not an HTTP path."""
    _sessions.clear()


def _prune() -> None:
    now = time.time()
    for mid, s in list(_sessions.items()):
        if now - s.last_activity > IDLE_PRUNE_S and not s.listeners:
            stop = getattr(s.engine, "stop", None)
            if stop:
                stop()
            del _sessions[mid]
            log.info("meeting session pruned: %s", mid)
