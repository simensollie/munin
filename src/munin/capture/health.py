"""Microphone health: can the source a recording is about to use deliver audio?

Presence is not health (D28). On 2026-10-14 a Bluetooth headset's call-mode
link flapped, WirePlumber aborted half-way through rebuilding its nodes, and
left a *ghost device*: the card at profile ``off``, a stale microphone node
still registered, and a configured default sink naming an output that no
longer existed. ``default_mic_target`` resolved the stale node, the capturer
pinned it, and the recording was a file of silence that said ``mic=ok``.

This module reads one decoded ``pw-dump`` and says ``ok``, ``degraded`` or
``unknown``. It is the one place the ghost signature lives, so the daemon (at
call detection and capture start), the capturer's warnings, ``munin doctor``
and ``munin status`` all agree on what "degraded" means.

Signals (spec 2026-10-14-mic-preflight, section 5.1), checked in this order;
the reason names the first that fired:

- **S1 ``node-error``**: the mic node's ``info.state`` is ``"error"``. **Not
  checked.** No ``pw-dump`` was taken while the ghost existed, so it is not
  known whether the node reports it. ``munin-audiofix`` snapshots ``pw-dump``
  before every repair so the next occurrence settles it; until then only the
  user-facing text for it exists here.
- **S2 ``profile-off``**: a ``bluez_card.*`` device whose *active* profile is
  ``off`` while ``api.bluez5.connection`` is ``connected``. The active profile
  is ``info.params.Profile[0].name`` -- the device's ``bluez5.profile``
  property is **not** it: measured on the healthy 2026-10-14 14:28 dump, that
  property read ``"off"`` while the card was playing A2DP. Only when a dump
  carries no ``Profile`` param does the property stand in.
- **S3 ``default-sink-missing``**: the ``default`` metadata object's
  ``default.configured.audio.sink`` names a node absent from the dump.
  WirePlumber keeps the configured default after the node is gone, which
  makes this the signal most likely to fire first, and the cheapest.

A muted or 0 % sink is not a signal: that is a user choice.

Never raises, never spawns anything in :func:`assess`. :func:`check` is the
platform-free seam that runs the platform's dump; a dump that cannot be had is
``unknown``, and everything carries on exactly as before (the check must never
block detection).

Owner: capture workstream.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "MicHealth",
    "REASON_TEXT",
    "SIGNALS",
    "assess",
    "bluez_cards",
    "check",
    "configured_default_sink",
    "describe_reason",
]

#: Signal names, in check order. They are what ``mic_health.reason`` and the
#: ``history[]`` entries carry.
SIGNALS: tuple[str, ...] = ("node-error", "profile-off", "default-sink-missing")

#: User-facing text for each signal (spec section 5.3), exactly.
REASON_TEXT: dict[str, str] = {
    "node-error": "in an error state",
    "profile-off": "connected but has no audio profile",
    "default-sink-missing": "set as default but no longer exists",
}

STATES: tuple[str, ...] = ("ok", "degraded", "unknown")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def describe_reason(reason: str | None) -> str:
    """The user-facing text for a signal name; the name itself when unknown."""
    if not reason:
        return ""
    return REASON_TEXT.get(reason, reason)


@dataclass(frozen=True)
class MicHealth:
    """One answer to "can the microphone deliver audio right now"."""

    state: str = "unknown"  # ok | degraded | unknown
    reason: str | None = None  # a SIGNALS name when degraded
    label: str | None = None  # the device the reason is about, for a human
    checked_at: datetime | None = None

    @property
    def degraded(self) -> bool:
        return self.state == "degraded"

    def to_dict(self) -> dict[str, Any]:
        """The ``mic_health`` shape of the state file (spec section 5.5)."""
        return {
            "state": self.state,
            "reason": self.reason,
            "label": self.label,
            "checked_at": _iso(self.checked_at),
        }

    def sentence(self) -> str:
        """``"<label> is <reason>"``, as the notification and the panel say it."""
        label = self.label or "The microphone"
        return f"{label} is {describe_reason(self.reason)}"


# --------------------------------------------------------------------------
# Reading a dump. Kept free of doctor's and detect's helpers on purpose: this
# module is imported by the daemon, and must not pull a platform module in.
# --------------------------------------------------------------------------


def _props(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    """Nodes and devices carry props under ``info.props``; Metadata at the top."""
    info = obj.get("info")
    if isinstance(info, Mapping):
        props = info.get("props")
        if isinstance(props, Mapping):
            return props
    props = obj.get("props")
    return props if isinstance(props, Mapping) else {}


def _type(obj: Mapping[str, Any]) -> str:
    return str(obj.get("type", ""))


def _objects(dump: Iterable[Any]) -> list[Mapping[str, Any]]:
    return [obj for obj in dump if isinstance(obj, Mapping)]


def _default_metadata(dump: Sequence[Mapping[str, Any]], key: str) -> str | None:
    for obj in dump:
        if not _type(obj).endswith(":Metadata"):
            continue
        if _props(obj).get("metadata.name") != "default":
            continue
        rows = obj.get("metadata")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or row.get("key") != key:
                continue
            value = row.get("value")
            if isinstance(value, Mapping):
                name = value.get("name")
                return str(name) if name else None
            return str(value) if value else None
    return None


def configured_default_sink(dump: Iterable[Any]) -> str | None:
    """``default.configured.audio.sink``'s node name, or ``None``."""
    return _default_metadata(_objects(dump), "default.configured.audio.sink")


def _node_names(dump: Sequence[Mapping[str, Any]]) -> set[str]:
    names: set[str] = set()
    for obj in dump:
        if not _type(obj).endswith(":Node"):
            continue
        name = _props(obj).get("node.name")
        if name:
            names.add(str(name))
    return names


def _active_profile(obj: Mapping[str, Any], props: Mapping[str, Any]) -> str | None:
    info = obj.get("info")
    params = info.get("params") if isinstance(info, Mapping) else None
    if isinstance(params, Mapping):
        profiles = params.get("Profile")
        if isinstance(profiles, list) and profiles:
            first = profiles[0]
            if isinstance(first, Mapping) and first.get("name"):
                return str(first["name"])
    value = props.get("bluez5.profile")
    return str(value) if value else None


def bluez_cards(dump: Iterable[Any]) -> list[dict[str, str | None]]:
    """Every Bluetooth audio card in the dump, reduced to what the checks read."""
    cards: list[dict[str, str | None]] = []
    for obj in _objects(dump):
        if not _type(obj).endswith(":Device"):
            continue
        props = _props(obj)
        name = str(props.get("device.name") or "")
        if props.get("device.api") != "bluez5" and not name.startswith("bluez_card."):
            continue
        cards.append(
            {
                "name": name or None,
                "label": str(
                    props.get("device.description")
                    or props.get("device.alias")
                    or name
                    or "Bluetooth device"
                ),
                "address": str(props.get("api.bluez5.address") or "") or None,
                "connection": str(props.get("api.bluez5.connection") or "") or None,
                "profile": _active_profile(obj, props),
            }
        )
    return cards


def _sink_label(sink: str, cards: Sequence[Mapping[str, str | None]]) -> str:
    """A human name for a sink that no longer exists.

    The node is gone, so its description is too. A Bluetooth output is named
    ``bluez_output.<AA_BB_..>.<n>`` and its card ``bluez_card.<AA_BB_..>``;
    when the card is still there its description is the headset's name.
    """
    if sink.startswith("bluez_output."):
        address = sink[len("bluez_output."):].rsplit(".", 1)[0]
        for card in cards:
            if card.get("name") == f"bluez_card.{address}":
                return str(card.get("label") or sink)
    return sink


def _mic_label(dump: Sequence[Mapping[str, Any]], mic_source: str) -> str | None:
    wanted = mic_source
    if mic_source == "default":
        wanted = _default_metadata(dump, "default.audio.source") or _default_metadata(
            dump, "default.configured.audio.source"
        ) or ""
    if not wanted:
        return None
    for obj in dump:
        if not _type(obj).endswith(":Node"):
            continue
        props = _props(obj)
        if not str(props.get("media.class", "")).startswith("Audio/Source"):
            continue
        if props.get("node.name") == wanted or str(props.get("object.serial")) == wanted:
            return str(props.get("node.description") or props.get("node.name") or wanted)
    return None


def assess(
    dump: Iterable[Any] | None,
    *,
    mic_source: str = "default",
    now: datetime | None = None,
) -> MicHealth:
    """The health of the microphone, from one decoded ``pw-dump``.

    ``None`` (no dump could be had) is ``unknown``. Pure: no I/O.
    """
    checked_at = now or datetime.now().astimezone()
    if dump is None:
        return MicHealth(state="unknown", checked_at=checked_at)
    objects = _objects(dump)
    cards = bluez_cards(objects)

    # S1 (node-error) is deliberately not checked; see the module docstring.

    for card in cards:
        if card.get("connection") == "connected" and card.get("profile") == "off":
            return MicHealth(
                state="degraded",
                reason="profile-off",
                label=card.get("label"),
                checked_at=checked_at,
            )

    sink = configured_default_sink(objects)
    if sink and sink not in _node_names(objects):
        return MicHealth(
            state="degraded",
            reason="default-sink-missing",
            label=_sink_label(sink, cards),
            checked_at=checked_at,
        )

    return MicHealth(
        state="ok", label=_mic_label(objects, mic_source), checked_at=checked_at
    )


def check(
    mic_source: str = "default",
    *,
    platform: str | None = None,
    now: datetime | None = None,
) -> MicHealth:
    """Read this machine's audio graph once and assess it.

    The platform-free seam, in the same idiom as ``recover_capture``: the
    platform module's ``mic_health`` does the reading, and a platform without
    one has no answer, which is ``unknown`` rather than an error. Never raises.
    """
    from munin.capture import get_capturer  # the one place that picks a platform

    checked_at = now or datetime.now().astimezone()
    try:
        module = importlib.import_module(get_capturer(platform).__module__)
        probe = getattr(module, "mic_health", None)
        if probe is None:
            return MicHealth(state="unknown", checked_at=checked_at)
        result = probe(mic_source, now=checked_at)
    except Exception:  # noqa: BLE001 - a health check must never block a recording
        return MicHealth(state="unknown", checked_at=checked_at)
    return result if isinstance(result, MicHealth) else MicHealth(checked_at=checked_at)
