"""In-memory meeting sessions.

Adapted from live-meeting-assistant's meeting.py (MIT, Ben Linford). The
in-person / group-meeting modes were dropped; echo suppression was kept.
Physical source names are normalized at the adapter seam before this core
decides which conversational role spoke.

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
from .conversation import (
    AdapterContractError,
    AdapterBinding,
    GOOGLE_MEET_ADAPTER,
    ROLE_COUNTERPARTY,
    ROLE_OPERATOR,
    SESSION_MISMATCH,
    TranscriptEvent,
    normalize_role,
)

log = logging.getLogger("meetcfg.meeting")

IDLE_PRUNE_S = 3600

#: Compatibility names for the current Google Meet wire.  They are physical
#: sources, not roles, and remain closed so an unknown source cannot become a
#: third speaker.
SOURCE_MIC = "mic"
SOURCE_SYSTEM = "system"
VALID_SOURCES = (SOURCE_MIC, SOURCE_SYSTEM)


def is_valid_source(source: object) -> bool:
    return isinstance(source, str) and source in GOOGLE_MEET_ADAPTER.valid_sources


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
    source: str   # physical source; kept on the current wire
    text: str
    at: float     # server wall-clock (epoch seconds)
    # True when this line only exists because echo suppression retracted a
    # near-duplicate from the other stream. One of the two copies is spurious
    # and the heuristic cannot say which, so the survivor is not trustworthy
    # as an independent utterance from the other party.
    echo_derived: bool = False
    role: str = ""
    conversation_channel: str = "google_meet"
    adapter_id: str = "google_meet"
    source_health: str = "healthy"
    echo_pending_until: float = 0.0
    binding: AdapterBinding | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Existing callers and fixtures construct Line(source="mic|system").
        # Normalize once here; all core decisions below consume role.
        normalized = normalize_role(self.role, legacy_source=self.source)
        if normalized is not None:
            self.role = normalized
        if self.binding is not None:
            reason = self.binding.validate()
            if reason:
                raise AdapterContractError(reason)
            self.source = self.binding.physical_source
            self.role = self.binding.role
            self.conversation_channel = self.binding.conversation_channel
            self.adapter_id = self.binding.adapter_id
            self.source_health = self.binding.source_health

    def speaker(self) -> str:
        if self.role == ROLE_OPERATOR:
            return "Tiago"
        if self.role == ROLE_COUNTERPARTY:
            return "Lead"
        return "Unknown"

    def echo_untrusted(self) -> bool:
        """Whether this line is derived echo or still inside echo grace."""
        return self.echo_derived or (
            self.role == ROLE_COUNTERPARTY
            and self.echo_pending_until > time.time()
        )


@dataclass(frozen=True)
class IngestResult:
    action: str
    line_id: int | None = None
    retract_line_id: int | None = None
    reason: str = ""


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
    _seen_adapter_events: set[tuple[str, str, str]] = field(default_factory=set)
    _active_inputs: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    echo_revision: int = 0

    def ingest(self, source: str, text: str) -> tuple[str, int | None, int | None]:
        """Compatibility façade for current Meet source=mic|system callers."""
        try:
            binding = GOOGLE_MEET_ADAPTER.bind(self.meeting_id, source)
        except AdapterContractError:
            log.warning("line rejected: unknown source %r", source)
            return ("reject", None, None)
        result = self.ingest_event(TranscriptEvent(
            binding=binding,
            text=text,
            echo_settled=True,
        ))
        return (result.action, result.line_id, result.retract_line_id)

    def ingest_event(self, event: TranscriptEvent) -> IngestResult:
        """Ingest one normalized adapter event and suppress cross-role echo.

        The counterparty voice leaking into the operator source produces a
        near-duplicate. Whichever role copy arrives second reveals the echo:
        - operator second -> suppress it
        - counterparty second -> retract operator and mark the survivor as
          echo-derived, so it cannot confirm a second interlocutor

        Stable adapter event ids are idempotent.  Replays never append a line
        or poke the engine.
        """
        reason = event.validate()
        if not reason and event.binding.session_id != self.meeting_id:
            reason = SESSION_MISMATCH
        if reason:
            log.warning("adapter event rejected reason=%s", reason)
            return IngestResult("reject", reason=reason)
        self.last_activity = time.time()
        binding = event.binding
        if event.event_id is not None:
            replay_key = (binding.adapter_id, binding.physical_source, event.event_id)
            if replay_key in self._seen_adapter_events:
                return IngestResult("replay", reason="REPLAY")
            self._seen_adapter_events.add(replay_key)
        now = time.time()
        retract_id: int | None = None
        echo_derived = False

        if settings.echo_suppress:
            match = self._find_recent_match(binding, event.text, now)
            if match is not None:
                if binding.role == ROLE_OPERATOR:
                    # The counterparty copy looked independent when it arrived,
                    # but the later duplicate proves it is ambiguous. Rebuild
                    # conversion so it cannot confirm a second interlocutor.
                    match.echo_derived = True
                    match.echo_pending_until = 0.0
                    self.echo_revision += 1
                    self.refresh_conversion()
                    rebase = getattr(self.engine, "rebase_trigger", None)
                    if callable(rebase):
                        rebase()
                    log.info("echo suppressed (operator dup of counterparty line %d)", match.id)
                    return IngestResult("suppress")
                # Counterparty second: the earlier operator line was the echo.
                self.lines.remove(match)
                retract_id = match.id
                # The surviving system line is a copy of speech that already
                # arrived on the mic stream. Whoever really spoke it, it is
                # not a second, independent party agreeing.
                echo_derived = True
                log.info("echo retracted (operator line %d was duplicate)", match.id)

        line = Line(
            id=self._next_id,
            source=binding.physical_source,
            text=event.text,
            at=now,
            echo_derived=echo_derived,
            role=binding.role,
            conversation_channel=binding.conversation_channel,
            adapter_id=binding.adapter_id,
            source_health=binding.source_health,
            echo_pending_until=(
                now + settings.echo_window_s
                if binding.role == ROLE_COUNTERPARTY
                and not echo_derived
                and not event.echo_settled
                else 0.0
            ),
            binding=binding,
        )
        self._next_id += 1
        self.lines.append(line)
        self.refresh_conversion()
        if (
            self.engine is not None
            and line.role == ROLE_COUNTERPARTY
            and not line.echo_derived
        ):
            self.engine.poke()
        action = "accept_retract" if retract_id is not None else "accept"
        return IngestResult(action, line.id, retract_id)

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

    def _find_recent_match(
        self, binding: AdapterBinding, text: str, now: float,
    ) -> Line | None:
        other_role = (
            ROLE_COUNTERPARTY if binding.role == ROLE_OPERATOR else ROLE_OPERATOR
        )
        window = settings.echo_window_s
        best: Line | None = None
        best_score = 0.0
        for line in reversed(self.lines):
            if now - line.at > window:
                break
            # Echo is an acoustic property of one adapter. Switching between
            # Meet and phone in the same logical session cannot retract a line
            # from the other channel.
            if line.adapter_id != binding.adapter_id or line.role != other_role:
                continue
            score = _similarity(line.text, text)
            if score > best_score:
                best, best_score = line, score
        if best is not None and best_score >= settings.echo_similarity:
            return best
        return None

    def counterparty_chars(self) -> int:
        """Settled counterparty characters: the engine trigger signal.

        Operator and echo-provisional speech never trigger a Codex call.
        """
        return sum(
            len(line.text)
            for line in self.lines
            if line.role == ROLE_COUNTERPARTY and not line.echo_untrusted()
        )

    def next_counterparty_settlement_delay(self) -> float | None:
        """Seconds until the next provisional counterparty line settles."""
        now = time.time()
        waits = [
            line.echo_pending_until - now
            for line in self.lines
            if line.role == ROLE_COUNTERPARTY
            and not line.echo_derived
            and line.echo_pending_until > now
        ]
        return min(waits) if waits else None

    def lead_chars(self) -> int:
        """Backward-compatible name for counterparty_chars()."""
        return self.counterparty_chars()

    def attach_input(self, binding: AdapterBinding) -> str:
        """Mark a live adapter stream so prune cannot orphan its session."""
        reason = binding.validate()
        if not reason and binding.session_id != self.meeting_id:
            reason = SESSION_MISMATCH
        if reason:
            return reason
        key = (
            binding.adapter_id,
            binding.conversation_channel,
            binding.physical_source,
            binding.role,
        )
        self._active_inputs[key] = self._active_inputs.get(key, 0) + 1
        self.last_activity = time.time()
        return ""

    def detach_input(self, binding: AdapterBinding) -> None:
        """Release one live adapter stream reference."""
        key = (
            binding.adapter_id,
            binding.conversation_channel,
            binding.physical_source,
            binding.role,
        )
        count = self._active_inputs.get(key, 0)
        if count <= 1:
            self._active_inputs.pop(key, None)
        else:
            self._active_inputs[key] = count - 1
        self.last_activity = time.time()

    @property
    def active_inputs(self) -> int:
        return sum(self._active_inputs.values())

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


# Additive semantic name for new adapters. Existing imports keep MeetingSession.
ConversationSession = MeetingSession


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


def prune_sessions(*, now: float | None = None, idle_s: float = IDLE_PRUNE_S) -> list[str]:
    """Prune idle sessions and return their ids (public for deterministic tests)."""
    now = time.time() if now is None else now
    pruned: list[str] = []
    for mid, s in list(_sessions.items()):
        if (
            now - s.last_activity > idle_s
            and not s.listeners
            and not s.active_inputs
        ):
            stop = getattr(s.engine, "stop", None)
            if stop:
                stop()
            del _sessions[mid]
            pruned.append(mid)
            log.info("meeting session pruned: %s", mid)
    return pruned


def _prune() -> None:
    prune_sessions()
