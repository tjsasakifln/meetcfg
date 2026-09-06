"""Conversation input seam shared by Meet and future local adapters.

The adapter owns physical-source names.  The conversation core only knows the
normalized role, the logical session, and the conversation channel.  Audio is
raw mono signed 16-bit PCM at 16 kHz; adapters may also replay transcript
events with a stable event id.

This module is deliberately transport-free.  It performs no I/O and persists
nothing.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

PCM_SAMPLE_RATE_HZ = 16_000
PCM_SAMPLE_FORMAT = "s16le"
PCM_CHANNELS = 1

CHANNEL_GOOGLE_MEET = "google_meet"
CHANNEL_PHONE = "phone"
VALID_CONVERSATION_CHANNELS = (CHANNEL_GOOGLE_MEET, CHANNEL_PHONE)

ROLE_OPERATOR = "operator"
ROLE_COUNTERPARTY = "counterparty"
VALID_ROLES = (ROLE_OPERATOR, ROLE_COUNTERPARTY)

SOURCE_HEALTH_HEALTHY = "healthy"
SOURCE_HEALTH_DEGRADED = "degraded"
SOURCE_HEALTH_DISCONNECTED = "disconnected"
VALID_SOURCE_HEALTH = (
    SOURCE_HEALTH_HEALTHY,
    SOURCE_HEALTH_DEGRADED,
    SOURCE_HEALTH_DISCONNECTED,
)

UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
UNKNOWN_ROLE = "UNKNOWN_ROLE"
UNKNOWN_CONVERSATION_CHANNEL = "UNKNOWN_CONVERSATION_CHANNEL"
UNKNOWN_SOURCE_HEALTH = "UNKNOWN_SOURCE_HEALTH"
SOURCE_DISCONNECTED = "SOURCE_DISCONNECTED"
INVALID_SAMPLE_RATE = "INVALID_SAMPLE_RATE"
INVALID_SAMPLE_FORMAT = "INVALID_SAMPLE_FORMAT"
INVALID_CHANNELS = "INVALID_CHANNELS"
INVALID_PCM = "INVALID_PCM"
SESSION_MISMATCH = "SESSION_MISMATCH"
INVALID_SESSION = "INVALID_SESSION"
INVALID_EVENT_ID = "INVALID_EVENT_ID"
INVALID_TRANSCRIPT = "INVALID_TRANSCRIPT"

GOOGLE_MEET_SOURCE_ROLES = MappingProxyType({
    "mic": ROLE_OPERATOR,
    "system": ROLE_COUNTERPARTY,
})


class AdapterContractError(ValueError):
    """A fail-closed adapter contract violation with a stable reason code."""

    def __init__(self, reason: str, *, allowed: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.allowed = allowed


@dataclass(frozen=True)
class AdapterBinding:
    """Identity and health of one adapter source bound to one session."""

    session_id: str
    conversation_channel: str
    role: str
    physical_source: str
    adapter_id: str
    source_health: str = SOURCE_HEALTH_HEALTHY
    sample_rate_hz: int = PCM_SAMPLE_RATE_HZ
    sample_format: str = PCM_SAMPLE_FORMAT
    channels: int = PCM_CHANNELS
    _contract: object = field(default=None, repr=False, compare=False)

    def validate(self) -> str:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            return INVALID_SESSION
        if self.conversation_channel not in VALID_CONVERSATION_CHANNELS:
            return UNKNOWN_CONVERSATION_CHANNEL
        if self.role not in VALID_ROLES:
            return UNKNOWN_ROLE
        if (
            not isinstance(self.physical_source, str)
            or not self.physical_source.strip()
        ):
            return UNKNOWN_SOURCE
        if not isinstance(self.adapter_id, str) or not self.adapter_id.strip():
            return UNKNOWN_SOURCE
        if self.source_health not in VALID_SOURCE_HEALTH:
            return UNKNOWN_SOURCE_HEALTH
        if self.source_health == SOURCE_HEALTH_DISCONNECTED:
            return SOURCE_DISCONNECTED
        if self.sample_rate_hz != PCM_SAMPLE_RATE_HZ:
            return INVALID_SAMPLE_RATE
        if self.sample_format != PCM_SAMPLE_FORMAT:
            return INVALID_SAMPLE_FORMAT
        if self.channels != PCM_CHANNELS:
            return INVALID_CHANNELS
        # Bindings are minted by their adapter façade. This validates every
        # adapter's declared physical-source mapping, not only Google Meet.
        contract = self._contract
        if not isinstance(contract, RoleMappedAdapter):
            return UNKNOWN_SOURCE
        if self.adapter_id != contract.adapter_id:
            return UNKNOWN_SOURCE
        if self.conversation_channel != contract.conversation_channel:
            return UNKNOWN_CONVERSATION_CHANNEL
        if self.sample_rate_hz != contract.sample_rate_hz:
            return INVALID_SAMPLE_RATE
        expected_role = contract.source_roles.get(self.physical_source)
        if expected_role is None:
            return UNKNOWN_SOURCE
        if self.role != expected_role:
            return UNKNOWN_ROLE
        return ""


@dataclass(frozen=True)
class TranscriptEvent:
    """A normalized transcript event; event_id makes adapter replay idempotent."""

    binding: AdapterBinding
    text: str
    event_id: str | None = None
    # True only when an upstream transcript adapter guarantees its own echo
    # settlement. Live PCM/transcription keeps the conservative grace period.
    echo_settled: bool = False

    def validate(self) -> str:
        reason = self.binding.validate()
        if reason:
            return reason
        if not isinstance(self.text, str) or not self.text.strip():
            return INVALID_TRANSCRIPT
        if self.event_id is not None and (
            not isinstance(self.event_id, str) or not self.event_id.strip()
        ):
            return INVALID_EVENT_ID
        return ""


@dataclass(frozen=True)
class PCM16kFrame:
    """One transport frame carrying its complete normalized source identity."""

    binding: AdapterBinding
    pcm: bytes | bytearray | memoryview

    def validate(self) -> str:
        return validate_pcm16k(self.binding, self.pcm)


@runtime_checkable
class ConversationInputAdapter(Protocol):
    """Small interface implemented by physical PCM/transcript adapters."""

    adapter_id: str
    conversation_channel: str
    sample_rate_hz: int
    sample_format: str
    channels: int

    def bind(
        self,
        session_id: str,
        physical_source: str,
        *,
        source_health: str = SOURCE_HEALTH_HEALTHY,
    ) -> AdapterBinding:
        """Normalize an adapter source or raise AdapterContractError."""


class RoleMappedAdapter:
    """Adapter façade for transports with a fixed source-to-role mapping."""

    def __init__(
        self,
        *,
        adapter_id: str,
        conversation_channel: str,
        source_roles: Mapping[str, str],
        sample_rate_hz: int = PCM_SAMPLE_RATE_HZ,
        sample_format: str = PCM_SAMPLE_FORMAT,
        channels: int = PCM_CHANNELS,
    ) -> None:
        self.adapter_id = adapter_id
        self.conversation_channel = conversation_channel
        self.sample_rate_hz = sample_rate_hz
        self.sample_format = sample_format
        self.channels = channels
        self.source_roles = MappingProxyType(dict(source_roles))

    @property
    def valid_sources(self) -> tuple[str, ...]:
        return tuple(self.source_roles)

    def bind(
        self,
        session_id: str,
        physical_source: str,
        *,
        source_health: str = SOURCE_HEALTH_HEALTHY,
    ) -> AdapterBinding:
        role = self.source_roles.get(physical_source)
        if role is None:
            raise AdapterContractError(UNKNOWN_SOURCE, allowed=self.valid_sources)
        binding = AdapterBinding(
            session_id=session_id,
            conversation_channel=self.conversation_channel,
            role=role,
            physical_source=physical_source,
            adapter_id=self.adapter_id,
            source_health=source_health,
            sample_rate_hz=self.sample_rate_hz,
            sample_format=self.sample_format,
            channels=self.channels,
            _contract=self,
        )
        reason = binding.validate()
        if reason:
            allowed = VALID_ROLES if reason == UNKNOWN_ROLE else ()
            raise AdapterContractError(reason, allowed=allowed)
        return binding


GOOGLE_MEET_ADAPTER = RoleMappedAdapter(
    adapter_id="google_meet",
    conversation_channel=CHANNEL_GOOGLE_MEET,
    source_roles=GOOGLE_MEET_SOURCE_ROLES,
)


def role_for_legacy_source(source: object) -> str | None:
    """Compatibility normalization for current source=mic|system clients."""
    if not isinstance(source, str):
        return None
    return GOOGLE_MEET_ADAPTER.source_roles.get(source)


def normalize_role(value: object, *, legacy_source: object = None) -> str | None:
    """Return one normalized role without admitting an unknown third party."""
    if isinstance(value, str) and value in VALID_ROLES:
        return value
    legacy = role_for_legacy_source(value)
    if legacy is not None:
        return legacy
    # Only an absent role may fall back to a separately supplied legacy
    # source. An explicit but unknown role must fail closed.
    if value not in (None, ""):
        return None
    return role_for_legacy_source(legacy_source)


def validate_pcm16k(binding: AdapterBinding, pcm: object) -> str:
    """Validate one PCM frame before it reaches the shared transcriber."""
    reason = binding.validate()
    if reason:
        return reason
    if (
        not isinstance(pcm, (bytes, bytearray, memoryview))
        or not pcm
        or len(pcm) % 2
    ):
        return INVALID_PCM
    return ""
