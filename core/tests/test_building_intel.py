import pytest

from blacktop.building_intel import (
    DEFAULT_FRICTION_BY_CLASS, PRESETS, BuildingIntel,
)
from blacktop.models import DestinationClass

ADDR = "77 Hudson St, Jersey City"


class TestNodes:
    def test_key_normalization(self):
        bi = BuildingIntel()
        bi.get_or_create("77  Hudson St,   Jersey City")
        assert bi.get("77 hudson st, jersey city") is not None

    def test_get_missing(self):
        assert BuildingIntel().get("nope") is None

    def test_get_or_create_idempotent(self):
        bi = BuildingIntel()
        a = bi.get_or_create(ADDR)
        b = bi.get_or_create(ADDR)
        assert a is b


class TestCapture:
    def test_presets_apply_fields(self):
        bi = BuildingIntel()
        node = bi.apply_preset(ADDR, "lobby_ok")
        assert node.lobby_handoff_ok is True
        bi.apply_preset(ADDR, "dock")
        assert node.dock_access is True

    def test_preset_notes_accumulate(self):
        bi = BuildingIntel()
        bi.apply_preset(ADDR, "dog")
        bi.apply_preset(ADDR, "stairs")
        node = bi.get(ADDR)
        assert "dog" in node.access_notes and "stairs" in node.access_notes

    def test_unknown_preset_rejected(self):
        with pytest.raises(KeyError):
            BuildingIntel().apply_preset(ADDR, "jetpack")

    def test_all_presets_are_appliable(self):
        bi = BuildingIntel()
        for name in PRESETS:
            bi.apply_preset(f"{ADDR} #{name}", name)

    def test_free_text_note(self):
        bi = BuildingIntel()
        bi.add_note(ADDR, "call box broken, use side door")
        assert "side door" in bi.get(ADDR).access_notes


class TestGateCodes:
    def test_roundtrip(self):
        bi = BuildingIntel()
        bi.set_gate_code(ADDR, "#4821")
        assert bi.get_gate_code(ADDR) == "#4821"

    def test_not_stored_in_plaintext(self):
        bi = BuildingIntel()
        bi.set_gate_code(ADDR, "#4821")
        blob = bi.get(ADDR).gate_code_encrypted
        assert isinstance(blob, bytes)
        assert b"4821" not in blob

    def test_missing_returns_none(self):
        assert BuildingIntel().get_gate_code(ADDR) is None


class TestFriction:
    def test_class_defaults(self):
        bi = BuildingIntel()
        for cls, minutes in DEFAULT_FRICTION_BY_CLASS.items():
            assert bi.friction_minutes("unseen addr", cls) == minutes

    def test_high_rise_costs_more_than_house(self):
        d = DEFAULT_FRICTION_BY_CLASS
        assert d[DestinationClass.HIGH_RISE] > d[DestinationClass.SINGLE_FAMILY]

    def test_learned_median_overrides_default(self):
        bi = BuildingIntel()
        for m in (6.0, 8.0, 10.0):
            bi.observe_friction(ADDR, m)
        assert bi.friction_minutes(ADDR) == 8.0
        assert bi.get(ADDR).sample_n == 3

    def test_friction_clipped(self):
        bi = BuildingIntel()
        bi.observe_friction(ADDR, 500.0)
        assert bi.friction_minutes(ADDR) == 30.0


class TestApproachBrief:
    def test_brief_contains_the_essentials(self):
        bi = BuildingIntel()
        node = bi.get_or_create(ADDR)
        node.entry_door = "north side"
        node.safe_park_note = "loading zone on Greene St"
        bi.set_gate_code(ADDR, "#4821")
        bi.apply_preset(ADDR, "lobby_ok")
        brief = bi.approach_brief(ADDR)
        assert "north side" in brief
        assert "#4821" in brief
        assert "lobby handoff OK" in brief
        assert "Greene St" in brief

    def test_unknown_address(self):
        assert BuildingIntel().approach_brief("nope") is None

    def test_empty_node_gives_none(self):
        bi = BuildingIntel()
        bi.get_or_create(ADDR)
        assert bi.approach_brief(ADDR) is None


class TestShareGate:
    """F14 exclusion rules (compliance I5)."""

    def test_default_not_shareable(self):
        bi = BuildingIntel()
        assert bi.get_or_create(ADDR).shareable is False

    def test_single_family_never_shareable(self):
        bi = BuildingIntel()
        bi.get_or_create("12 Maple Ave", destination_class=DestinationClass.SINGLE_FAMILY)
        with pytest.raises(PermissionError):
            bi.set_shareable("12 Maple Ave", True)

    def test_unknown_class_not_shareable(self):
        bi = BuildingIntel()
        bi.get_or_create(ADDR)
        with pytest.raises(PermissionError):
            bi.set_shareable(ADDR, True)

    def test_commercial_shareable_on_explicit_optin(self):
        bi = BuildingIntel()
        bi.get_or_create(ADDR, destination_class=DestinationClass.HIGH_RISE)
        assert bi.set_shareable(ADDR, True).shareable is True

    def test_optout_always_allowed(self):
        bi = BuildingIntel()
        bi.get_or_create("12 Maple Ave", destination_class=DestinationClass.SINGLE_FAMILY)
        assert bi.set_shareable("12 Maple Ave", False).shareable is False
