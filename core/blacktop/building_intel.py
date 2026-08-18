"""F5 — Last-100-Feet Intel. The moat.

A knowledge base keyed by building / complex / address capturing everything
that costs time at the destination: access procedure, gate code (encrypted),
entry door, elevator bank, lobby handoff, dock, safe parking, friction.

Capture must cost under two seconds (PRD 9.4): notes arrive either as preset
taps (``PRESETS``) or free text from on-device transcription. Retrieval is
automatic when an offer's dropoff matches a known node — before the accept
decision (feeds F1's friction term) and again on approach.

Compliance:
* All entries are the driver's own observations (I5).
* ``shareable`` defaults False and can only flip True for commercial /
  multi-unit properties on explicit opt-in — ``set_shareable`` refuses
  single-family residences outright (F14 exclusion rule).
* Gate codes are stored only through an injected cipher; this module never
  holds plaintext at rest. The reference default is an XOR obfuscator that
  stands in for Android Keystore / pgsodium in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .models import Building, DestinationClass

# Preset two-tap capture options (PRD F5 capture UX).
PRESETS: dict[str, dict[str, object]] = {
    "lobby_ok":      {"lobby_handoff_ok": True},
    "no_lobby":      {"lobby_handoff_ok": False},
    "dock":          {"dock_access": True},
    "no_dock":       {"dock_access": False},
    "dog":           {"access_notes": "dog on premises"},
    "stairs":        {"access_notes": "stairs only"},
    "gate":          {"access_notes": "gated entry"},
}

DEFAULT_FRICTION_BY_CLASS: dict[DestinationClass, float] = {
    DestinationClass.SINGLE_FAMILY: 1.0,
    DestinationClass.MULTI_UNIT: 3.0,
    DestinationClass.HIGH_RISE: 6.0,
    DestinationClass.COMMERCIAL: 3.0,
    DestinationClass.CAMPUS: 5.0,
    DestinationClass.HOTEL: 4.0,
    DestinationClass.UNKNOWN: 2.0,
}

SHAREABLE_CLASSES = {DestinationClass.MULTI_UNIT, DestinationClass.HIGH_RISE,
                     DestinationClass.COMMERCIAL, DestinationClass.HOTEL,
                     DestinationClass.CAMPUS}


def _xor_cipher(key: bytes) -> tuple[Callable[[str], bytes], Callable[[bytes], str]]:
    """Reference stand-in for a real cipher (Android Keystore / pgsodium in
    production). Documented as obfuscation, not security."""
    def enc(plain: str) -> bytes:
        data = plain.encode()
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    def dec(blob: bytes) -> str:
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(blob)).decode()
    return enc, dec


class BuildingIntel:
    def __init__(self,
                 encrypt: Callable[[str], bytes] | None = None,
                 decrypt: Callable[[bytes], str] | None = None) -> None:
        if encrypt is None or decrypt is None:
            encrypt, decrypt = _xor_cipher(b"blacktop-reference-key")
        self._encrypt = encrypt
        self._decrypt = decrypt
        self._nodes: dict[str, Building] = {}
        self._friction_samples: dict[str, list[float]] = {}

    # ------------------------------------------------------------- lookup
    @staticmethod
    def node_key(address: str) -> str:
        return " ".join(address.lower().split())

    def get(self, address: str) -> Building | None:
        return self._nodes.get(self.node_key(address))

    def get_or_create(self, address: str, hex_res9: str = "",
                      destination_class: DestinationClass = DestinationClass.UNKNOWN) -> Building:
        key = self.node_key(address)
        node = self._nodes.get(key)
        if node is None:
            node = Building(building_id=key, hex_res9=hex_res9, label=address,
                            destination_class=destination_class)
            self._nodes[key] = node
        return node

    # ------------------------------------------------------------ capture
    def apply_preset(self, address: str, preset: str) -> Building:
        if preset not in PRESETS:
            raise KeyError(f"unknown preset {preset!r}")
        node = self.get_or_create(address)
        for field_name, value in PRESETS[preset].items():
            if field_name == "access_notes":
                existing = node.access_notes
                node.access_notes = f"{existing}; {value}" if existing else str(value)
            else:
                setattr(node, field_name, value)
        return node

    def add_note(self, address: str, note: str) -> Building:
        """Free-text note from voice transcription ('Anything?')."""
        node = self.get_or_create(address)
        node.access_notes = f"{node.access_notes}; {note}" if node.access_notes else note
        return node

    def set_gate_code(self, address: str, code: str) -> Building:
        node = self.get_or_create(address)
        node.gate_code_encrypted = self._encrypt(code)
        return node

    def get_gate_code(self, address: str) -> str | None:
        node = self.get(address)
        if node is None or node.gate_code_encrypted is None:
            return None
        return self._decrypt(node.gate_code_encrypted)

    def observe_friction(self, address: str, minutes: float) -> Building:
        """Realized dropoff friction (arrival at destination -> completed)."""
        node = self.get_or_create(address)
        samples = self._friction_samples.setdefault(node.building_id, [])
        samples.append(min(max(minutes, 0.0), 30.0))
        samples.sort()
        node.sample_n = len(samples)
        mid = (len(samples) - 1) / 2
        node.friction_minutes_p50 = (samples[int(mid)] + samples[int(mid + 0.5)]) / 2
        return node

    # ---------------------------------------------------------- retrieval
    def friction_minutes(self, address: str,
                         destination_class: DestinationClass = DestinationClass.UNKNOWN) -> float:
        """F1's T_dropoff_friction: learned P50 when known, class default
        otherwise."""
        node = self.get(address)
        if node is not None and node.friction_minutes_p50 is not None:
            return node.friction_minutes_p50
        cls = node.destination_class if node is not None and \
            node.destination_class is not DestinationClass.UNKNOWN else destination_class
        return DEFAULT_FRICTION_BY_CLASS[cls]

    def approach_brief(self, address: str) -> str | None:
        """One-glance brief surfaced on approach."""
        node = self.get(address)
        if node is None:
            return None
        parts: list[str] = []
        if node.access_notes:
            parts.append(node.access_notes)
        if node.entry_door:
            parts.append(f"entry: {node.entry_door}")
        if node.lobby_handoff_ok is True:
            parts.append("lobby handoff OK")
        if node.gate_code_encrypted is not None:
            parts.append(f"gate code {self._decrypt(node.gate_code_encrypted)}")
        if node.safe_park_note:
            parts.append(f"park: {node.safe_park_note}")
        return "; ".join(parts) if parts else None

    # --------------------------------------------------------- share gate
    def set_shareable(self, address: str, shareable: bool) -> Building:
        """F14 exclusion rule: single-family residences are never shareable,
        period. Everything else requires this explicit opt-in call."""
        node = self.get_or_create(address)
        if shareable and node.destination_class not in SHAREABLE_CLASSES:
            raise PermissionError(
                "only commercial and multi-unit properties may be shared; "
                f"{node.destination_class.value} is excluded")
        node.shareable = shareable
        return node
