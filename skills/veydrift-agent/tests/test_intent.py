"""Tests for the adaptive-intent override: `engine.resolve_effective_intent` (every branch) and
the `vd engine intent set|show|clear` CLI. Everything runs in an isolated `$VEYDRIFT_HOME`; no
network and no TypeSafe key is involved."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from veydrift_agent import engine as engine_mod
from veydrift_agent import log, state
from veydrift_agent.cli import app as vd_app
from veydrift_agent.jev_engine import DEFAULT_INTENT
from veydrift_agent.models import Policy

EXAMPLE = Path(__file__).parent.parent / "assets" / "policy.example.json"
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
# A well-known throwaway address (never a real signer), used only to exercise the signer check.
SIGNER = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "veydrift-home"
    monkeypatch.setenv("VEYDRIFT_HOME", str(home))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return home


def policy_dict(*, kind: str = "jev", adaptive: bool = True, intent: str = "", **jev: object) -> dict:
    raw = json.loads(EXAMPLE.read_text())
    raw["engine"]["kind"] = kind
    raw["engine"]["jev"]["intent"] = intent
    raw["engine"]["jev"]["adaptive_intent"] = adaptive
    raw["engine"]["jev"].update(jev)
    return raw


def make_policy(**kwargs: object) -> Policy:
    return Policy.model_validate(policy_dict(**kwargs))  # type: ignore[arg-type]


def write_policy(path: Path | None = None, **kwargs: object) -> Path:
    path = path or state.policy_path()
    path.write_text(json.dumps(policy_dict(**kwargs)))  # type: ignore[arg-type]
    return path


def store(intent: str = "Defenses first while under raid.", *, expires_in: timedelta = timedelta(hours=2)) -> state.IntentOverride:
    override = state.IntentOverride(
        intent=intent, reason="3 raids today", set_at=NOW - timedelta(hours=1), expires_at=NOW + expires_in
    )
    state.save_intent_override(override)
    return override


def strategy_text() -> str:
    path = log.strategy_path()
    return path.read_text() if path.exists() else ""


# --------------------------------------------------------------------------------------
# resolve_effective_intent -- every branch
# --------------------------------------------------------------------------------------


def test_ladder_configured_uses_the_policy_intent_even_with_a_live_override():
    store()
    result = engine_mod.resolve_effective_intent(make_policy(kind="ladder", intent="Grow the economy."), now=NOW)
    assert (result.text, result.source, result.note, result.expired) == ("Grow the economy.", "policy", None, False)


def test_ladder_configured_with_an_empty_policy_intent_uses_the_default():
    result = engine_mod.resolve_effective_intent(make_policy(kind="ladder"), now=NOW)
    assert (result.text, result.source) == (DEFAULT_INTENT, "default")


def test_no_file_uses_the_policy_intent():
    result = engine_mod.resolve_effective_intent(make_policy(intent="  Grow the economy.  "), now=NOW)
    assert (result.text, result.source, result.override, result.note) == ("Grow the economy.", "policy", None, None)


def test_no_file_and_empty_policy_intent_uses_the_default():
    result = engine_mod.resolve_effective_intent(make_policy(intent="   "), now=NOW)
    assert (result.text, result.source) == (DEFAULT_INTENT, "default")


def test_an_unreadable_file_is_ignored_with_a_note():
    state.intent_override_path().write_text("{not json")
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.text, result.source, result.note) == ("Grow the economy.", "policy", "override file unreadable")
    assert result.override is None
    assert result.expired is False


def test_an_override_is_ignored_when_the_flag_is_off():
    stored = store()
    result = engine_mod.resolve_effective_intent(make_policy(adaptive=False, intent="Grow the economy."), now=NOW)
    assert result.text == "Grow the economy."
    assert result.source == "policy"
    assert result.note == "override ignored: adaptive_intent is off"
    assert result.override == stored
    assert result.expired is False


def test_an_expired_override_falls_back_and_is_marked_expired():
    stored = store(expires_in=timedelta(minutes=-1))
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.text, result.source) == ("Grow the economy.", "policy")
    assert result.note == "override expired"
    assert result.expired is True
    assert result.override == stored


def test_the_expiry_boundary_is_exclusive():
    """`now >= expires_at` is expired: an override is live strictly before its expiry."""
    store(expires_in=timedelta(0))
    assert engine_mod.resolve_effective_intent(make_policy(), now=NOW).expired is True
    store(expires_in=timedelta(seconds=1))
    live = engine_mod.resolve_effective_intent(make_policy(), now=NOW)
    assert live.source == "agent"
    assert live.expired is False


def test_an_expired_override_is_not_deleted_by_the_resolver():
    store(expires_in=timedelta(minutes=-1))
    engine_mod.resolve_effective_intent(make_policy(), now=NOW)
    assert state.intent_override_path().exists()


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("Raid the colony at 7:291:1 first.", "contains coordinates"),
        (f"Send everything to {WALLET}.", "contains an address"),
        ("Protect planet 664 above all.", "contains planet id 664"),
    ],
)
def test_a_hand_edited_override_with_identifying_text_is_rejected(text, fragment):
    stored = store(text)
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.text, result.source) == ("Grow the economy.", "policy")
    assert result.note is not None
    assert result.note.startswith("override rejected: ")
    assert fragment in result.note
    assert result.override == stored
    assert result.expired is False


def test_an_override_naming_the_signer_is_rejected():
    raw = policy_dict(intent="Grow the economy.")
    raw["signer"] = SIGNER
    store(f"Keep {SIGNER.upper()} funded.")
    result = engine_mod.resolve_effective_intent(Policy.model_validate(raw), now=NOW)
    assert result.source == "policy"
    assert result.note is not None
    assert "signer" in result.note


def test_a_whitespace_only_override_is_rejected():
    store("   ")
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.source, result.note) == ("policy", "override rejected: empty")


def test_a_live_override_is_the_agent_intent_and_is_stripped():
    stored = store("  Defenses first while under raid.  ")
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert result.text == "Defenses first while under raid."
    assert result.source == "agent"
    assert result.note is None
    assert result.expired is False
    assert result.override == stored


def test_an_override_with_an_empty_policy_intent_still_wins():
    store()
    result = engine_mod.resolve_effective_intent(make_policy(), now=NOW)
    assert (result.text, result.source) == ("Defenses first while under raid.", "agent")


def test_a_hand_edited_override_with_naive_datetimes_does_not_raise():
    # Timestamps without a timezone cannot be compared with the tick's clock: the file is
    # treated as unreadable, so the policy intent applies and nothing raises.
    state.intent_override_path().write_text(
        json.dumps(
            {
                "version": 1,
                "intent": "Defenses first.",
                "reason": "r",
                "set_at": "2026-10-02T10:00:00",
                "expires_at": "2099-10-03T10:00:00",
            }
        )
    )
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.source, result.note) == ("policy", "override file unreadable")
    with pytest.raises(state.IntentOverrideError):
        state.load_intent_override()


def stored_raw(**changes: object) -> dict:
    raw = {
        "version": 1,
        "intent": "Defenses first.",
        "reason": "r",
        "set_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=2)).isoformat(),
    }
    raw.update(changes)
    return raw


def write_raw(**changes: object) -> None:
    state.intent_override_path().write_text(json.dumps(stored_raw(**changes)))


@pytest.mark.parametrize(
    ("label", "changes"),
    [
        ("lifetime over 72h", {"set_at": (NOW - timedelta(hours=1)).isoformat(), "expires_at": (NOW + timedelta(hours=72)).isoformat()}),
        ("far future expiry", {"expires_at": "9999-12-31T23:59:59+00:00"}),
        ("expires after the cap from now", {"set_at": (NOW - timedelta(hours=40)).isoformat(), "expires_at": (NOW + timedelta(hours=73)).isoformat()}),
        ("expires before it was set", {"set_at": (NOW + timedelta(hours=5)).isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat()}),
        ("expires when it was set", {"set_at": (NOW + timedelta(hours=2)).isoformat(), "expires_at": (NOW + timedelta(hours=2)).isoformat()}),
        ("set in the future", {"set_at": (NOW + timedelta(hours=1)).isoformat(), "expires_at": (NOW + timedelta(hours=3)).isoformat()}),
    ],
)
def test_an_override_with_an_impossible_lifetime_is_rejected(label, changes):
    write_raw(**changes)
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.text, result.source, result.note) == ("Grow the economy.", "policy", "override rejected: lifetime"), label
    assert result.expired is False
    assert result.override is not None


def test_an_override_of_exactly_the_maximum_lifetime_is_honoured():
    write_raw(set_at=NOW.isoformat(), expires_at=(NOW + timedelta(hours=72)).isoformat())
    assert engine_mod.resolve_effective_intent(make_policy(), now=NOW).source == "agent"


def test_a_set_at_a_few_minutes_ahead_is_clock_skew_not_rejected():
    write_raw(set_at=(NOW + timedelta(minutes=4)).isoformat(), expires_at=(NOW + timedelta(hours=2)).isoformat())
    assert engine_mod.resolve_effective_intent(make_policy(), now=NOW).source == "agent"
    write_raw(set_at=(NOW + timedelta(minutes=6)).isoformat(), expires_at=(NOW + timedelta(hours=2)).isoformat())
    assert engine_mod.resolve_effective_intent(make_policy(), now=NOW).note == "override rejected: lifetime"


@pytest.mark.parametrize("version", [0, 2, 99])
def test_an_override_of_an_unsupported_version_is_rejected(version):
    write_raw(version=version)
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.source, result.note) == ("policy", "override rejected: unsupported version")


def test_a_naive_now_is_read_as_utc():
    store()
    naive = NOW.replace(tzinfo=None)
    assert engine_mod.resolve_effective_intent(make_policy(), now=naive).source == "agent"
    store(expires_in=timedelta(minutes=-1))
    assert engine_mod.resolve_effective_intent(make_policy(), now=naive).expired is True


def test_an_expired_override_is_reported_expired_even_with_the_flag_off():
    stored = store(expires_in=timedelta(minutes=-1))
    result = engine_mod.resolve_effective_intent(make_policy(adaptive=False, intent="Grow the economy."), now=NOW)
    assert (result.note, result.expired, result.override) == ("override expired", True, stored)


def test_the_flag_off_note_still_wins_over_a_bad_lifetime():
    write_raw(expires_at="9999-12-31T23:59:59+00:00")
    result = engine_mod.resolve_effective_intent(make_policy(adaptive=False), now=NOW)
    assert result.note == "override ignored: adaptive_intent is off"


def test_a_pathologically_nested_file_is_ignored_with_the_unreadable_note():
    state.intent_override_path().write_text("[" * 200_000)
    result = engine_mod.resolve_effective_intent(make_policy(intent="Grow the economy."), now=NOW)
    assert (result.source, result.note) == ("policy", "override file unreadable")


# --------------------------------------------------------------------------------------
# parse_ttl
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90m", timedelta(minutes=90)),
        ("6h", timedelta(hours=6)),
        ("1.5h", timedelta(minutes=90)),
        ("2d", timedelta(days=2)),
        ("6", timedelta(hours=6)),
        ("0.5", timedelta(minutes=30)),
        (" 3H ", timedelta(hours=3)),
        ("0h", timedelta(0)),
    ],
)
def test_parse_ttl(raw, expected):
    assert engine_mod.parse_ttl(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "", "h", "-1h", "1w", "1h30m", "nan", "inf", "1e3h", "1,5h"])
def test_parse_ttl_rejects_junk(raw):
    with pytest.raises(ValueError, match="ttl"):
        engine_mod.parse_ttl(raw)


# --------------------------------------------------------------------------------------
# vd engine intent set
# --------------------------------------------------------------------------------------


def invoke_set(*args: str):
    return runner.invoke(vd_app, ["engine", "intent", "set", *args])


GOOD = "Defenses first while under raid."


def test_set_is_refused_when_the_flag_is_off():
    write_policy(adaptive=False)
    result = invoke_set(GOOD, "--reason", "raided")
    assert result.exit_code == 2
    assert "adaptive_intent" in result.output
    assert not state.intent_override_path().exists()
    assert strategy_text() == ""


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("Hit 7:291:1 now.", "coordinates"),
        ("Planet 664 needs defenses.", "planet id 664"),
        (f"Fund {WALLET}.", "address"),
        ("", "empty"),
        ("   ", "empty"),
        ("x" * 1001, "1001 characters"),
    ],
)
def test_set_is_refused_for_bad_text(text, fragment):
    write_policy()
    result = invoke_set(text, "--reason", "raided")
    assert result.exit_code == 2
    assert fragment in result.output
    assert not state.intent_override_path().exists()
    assert strategy_text() == ""


@pytest.mark.parametrize("reason", ["", "   ", "r" * 281])
def test_set_is_refused_for_a_bad_reason(reason):
    write_policy()
    result = invoke_set(GOOD, "--reason", reason)
    assert result.exit_code == 2
    assert "reason" in result.output
    assert not state.intent_override_path().exists()


def test_set_requires_a_reason_option():
    write_policy()
    result = invoke_set(GOOD)
    assert result.exit_code == 2
    assert not state.intent_override_path().exists()


@pytest.mark.parametrize(
    ("ttl", "fragment"),
    [("73h", "maximum of 72h"), ("0h", "greater than zero"), ("abc", "cannot parse ttl"), ("4321m", "maximum of 72h"), ("4d", "maximum of 72h")],
)
def test_set_is_refused_for_a_bad_ttl(ttl, fragment):
    write_policy()
    result = invoke_set(GOOD, "--reason", "raided", "--ttl", ttl)
    assert result.exit_code == 2
    assert fragment in result.output
    assert not state.intent_override_path().exists()
    assert strategy_text() == ""


def test_set_accepts_the_maximum_ttl():
    write_policy()
    assert invoke_set(GOOD, "--reason", "raided", "--ttl", "72h").exit_code == 0
    assert invoke_set(GOOD, "--reason", "raided", "--ttl", "3d").exit_code == 0


def test_set_reports_every_problem_at_once():
    write_policy(adaptive=False)
    result = invoke_set("Hit 7:291:1", "--reason", " ", "--ttl", "99h")
    assert result.exit_code == 2
    for fragment in ("adaptive_intent", "coordinates", "reason", "72h"):
        assert fragment in result.output


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [("90m", timedelta(minutes=90)), ("1.5h", timedelta(minutes=90)), ("2d", timedelta(days=2)), ("6", timedelta(hours=6))],
)
def test_set_ttl_parsing(ttl, expected):
    write_policy()
    result = invoke_set(GOOD, "--reason", "raided", "--ttl", ttl)
    assert result.exit_code == 0, result.output
    stored = state.load_intent_override()
    assert stored is not None
    assert stored.expires_at - stored.set_at == expected


def test_set_default_ttl_is_six_hours():
    write_policy()
    assert invoke_set(GOOD, "--reason", "raided").exit_code == 0
    stored = state.load_intent_override()
    assert stored is not None
    assert stored.expires_at - stored.set_at == timedelta(hours=6)


def test_set_default_ttl_follows_the_policy():
    write_policy(adaptive_intent_default_hours=12)
    assert invoke_set(GOOD, "--reason", "raided").exit_code == 0
    stored = state.load_intent_override()
    assert stored is not None
    assert stored.expires_at - stored.set_at == timedelta(hours=12)


def test_set_writes_a_stripped_override_and_a_strategy_line():
    write_policy()
    result = invoke_set(f"  {GOOD}  ", "--reason", "  3 raids today  ", "--ttl", "2h")
    assert result.exit_code == 0, result.output
    assert "intent override set" in result.output
    stored = state.load_intent_override()
    assert stored is not None
    assert (stored.intent, stored.reason) == (GOOD, "3 raids today")
    assert stored.set_at.tzinfo is not None
    assert stored.expires_at.tzinfo is not None
    text = strategy_text()
    assert f'intent override set until {stored.expires_at.isoformat()}: "{GOOD}" -- 3 raids today' in text
    assert text.count("intent override set") == 1


def test_set_replaces_an_existing_override():
    write_policy()
    assert invoke_set("First intent.", "--reason", "one").exit_code == 0
    result = invoke_set("Second intent.", "--reason", "two")
    assert result.exit_code == 0
    assert "replaced" in result.output
    stored = state.load_intent_override()
    assert stored is not None
    assert stored.intent == "Second intent."


def test_set_replaces_an_unreadable_override():
    write_policy()
    state.intent_override_path().write_text("{broken")
    result = invoke_set(GOOD, "--reason", "raided")
    assert result.exit_code == 0
    assert "replaced" in result.output
    assert state.load_intent_override() is not None


def test_set_under_the_ladder_warns_but_still_writes():
    write_policy(kind="ladder")
    result = invoke_set(GOOD, "--reason", "raided")
    assert result.exit_code == 0
    assert "no effect" in result.output
    assert state.load_intent_override() is not None


def test_set_with_jev_does_not_warn():
    write_policy()
    assert "no effect" not in invoke_set(GOOD, "--reason", "raided").output


def test_set_json_is_the_stored_override():
    write_policy()
    result = invoke_set(GOOD, "--reason", "raided", "--ttl", "1h", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    stored = state.load_intent_override()
    assert stored is not None
    assert payload["intent"] == GOOD
    assert payload["reason"] == "raided"
    assert payload["replaced"] is False
    assert payload["warnings"] == []
    assert datetime.fromisoformat(payload["expires_at"]) == stored.expires_at


def test_set_json_under_the_ladder_carries_the_warning_and_stays_parsable():
    write_policy(kind="ladder")
    result = invoke_set(GOOD, "--reason", "raided", "--json")
    assert result.exit_code == 0
    assert "no effect" in json.loads(result.stdout)["warnings"][0]


def test_set_keeps_brackets_in_text_literal():
    write_policy()
    assert invoke_set("Prefer [bold]research[/bold] now.", "--reason", "[red]why[/red]").exit_code == 0
    stored = state.load_intent_override()
    assert stored is not None
    assert stored.intent == "Prefer [bold]research[/bold] now."


def test_set_honours_the_policy_option(tmp_path):
    elsewhere = write_policy(tmp_path / "other-policy.json")
    assert not state.policy_path().exists()
    result = invoke_set(GOOD, "--reason", "raided", "--policy", str(elsewhere))
    assert result.exit_code == 0
    assert state.load_intent_override() is not None


def test_set_uses_the_policy_option_over_the_home_policy(tmp_path):
    write_policy(adaptive=False)  # $VEYDRIFT_HOME/policy.json would refuse
    elsewhere = write_policy(tmp_path / "other-policy.json", adaptive=True)
    assert invoke_set(GOOD, "--reason", "raided", "--policy", str(elsewhere)).exit_code == 0


def test_set_checks_the_signer_and_planets_of_the_loaded_policy(tmp_path):
    raw = policy_dict()
    raw["signer"] = SIGNER
    raw["planets"] = [664, 1234]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(raw))
    for text, fragment in ((f"Keep {SIGNER.upper()} funded.", "signer"), ("Defend 1234 first.", "planet id 1234")):
        result = invoke_set(text, "--reason", "r", "--policy", str(path))
        assert result.exit_code == 2
        assert fragment in result.output


def test_set_exits_4_for_a_missing_policy():
    result = invoke_set(GOOD, "--reason", "raided")
    assert result.exit_code == 4
    assert not state.intent_override_path().exists()


def test_set_exits_4_for_invalid_json_or_an_invalid_policy():
    path = state.policy_path()
    path.write_text("{nope")
    assert invoke_set(GOOD, "--reason", "raided").exit_code == 4
    path.write_text(json.dumps({"version": 1}))
    assert invoke_set(GOOD, "--reason", "raided").exit_code == 4
    assert not state.intent_override_path().exists()


def test_set_exits_4_for_a_policy_whose_own_intent_is_identifying():
    raw = policy_dict()
    raw["engine"]["jev"]["intent"] = "Protect planet 664."
    state.policy_path().write_text(json.dumps(raw))
    result = invoke_set(GOOD, "--reason", "raided")
    assert result.exit_code == 4
    assert "engine.jev.intent" in result.output


# --------------------------------------------------------------------------------------
# vd engine intent show
# --------------------------------------------------------------------------------------


def invoke_show(*args: str):
    return runner.invoke(vd_app, ["engine", "intent", "show", *args])


def test_show_with_no_override_reports_the_policy_intent():
    write_policy(intent="Grow the economy.")
    result = invoke_show()
    assert result.exit_code == 0
    assert "intent: Grow the economy." in result.output
    assert "source: policy" in result.output
    assert "override" not in result.output


def test_show_with_no_override_and_no_policy_intent_reports_the_default():
    write_policy()
    result = invoke_show()
    assert f"intent: {DEFAULT_INTENT}" in result.output
    assert "source: default" in result.output


def test_show_json_with_no_override():
    write_policy(intent="Grow the economy.")
    payload = json.loads(invoke_show("--json").stdout)
    assert payload == {
        "engine_kind": "jev",
        "adaptive_intent": True,
        "intent": "Grow the economy.",
        "source": "policy",
        "note": None,
        "expired": False,
        "override": None,
        "send_time_check": SEND_TIME_NOTE,
    }


SEND_TIME_NOTE = (
    "ticks re-check this text against the account's planets and targets right before sending "
    "and may still substitute it"
)


def test_show_says_a_tick_still_re_checks_the_text_at_send_time():
    write_policy(intent="Grow the economy.")
    assert f"send-time check: {SEND_TIME_NOTE}" in invoke_show().output
    assert json.loads(invoke_show("--json").stdout)["send_time_check"] == SEND_TIME_NOTE
    assert invoke_set(GOOD, "--reason", "r", "--ttl", "2h").exit_code == 0
    assert f"send-time check: {SEND_TIME_NOTE}" in invoke_show().output
    assert json.loads(invoke_show("--json").stdout)["send_time_check"] == SEND_TIME_NOTE


def test_show_a_live_override():
    write_policy(intent="Grow the economy.")
    assert invoke_set(GOOD, "--reason", "3 raids today", "--ttl", "2h").exit_code == 0
    stored = state.load_intent_override()
    assert stored is not None
    result = invoke_show()
    assert result.exit_code == 0
    assert f"intent: {GOOD}" in result.output
    assert "source: agent" in result.output
    assert "override reason: 3 raids today" in result.output
    assert f"override expires at: {stored.expires_at.isoformat()}" in result.output
    assert f"override set at: {stored.set_at.isoformat()}" in result.output
    assert "note:" not in result.output
    payload = json.loads(invoke_show("--json").stdout)
    assert payload["source"] == "agent"
    assert payload["intent"] == GOOD
    assert payload["override"]["reason"] == "3 raids today"
    assert payload["override"]["intent"] == GOOD
    assert payload["note"] is None


def test_show_an_expired_override_reports_it_and_does_not_delete_it():
    write_policy(intent="Grow the economy.")
    state.save_intent_override(
        state.IntentOverride(
            intent=GOOD,
            reason="old",
            set_at=datetime.now(UTC) - timedelta(hours=8),
            expires_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    result = invoke_show()
    assert result.exit_code == 0
    assert "intent: Grow the economy." in result.output
    assert "source: policy" in result.output
    assert "note: override expired" in result.output
    assert "override reason: old" in result.output
    payload = json.loads(invoke_show("--json").stdout)
    assert payload["expired"] is True
    assert payload["note"] == "override expired"
    assert payload["override"]["intent"] == GOOD
    assert state.intent_override_path().exists()


def test_show_with_the_flag_off_reports_the_override_as_ignored():
    write_policy(adaptive=False, intent="Grow the economy.")
    store_live = state.IntentOverride(
        intent=GOOD, reason="r", set_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    state.save_intent_override(store_live)
    result = invoke_show()
    assert "source: policy" in result.output
    assert "note: override ignored: adaptive_intent is off" in result.output
    assert f"override text (not in use): {GOOD}" in result.output


def test_show_under_the_ladder_mentions_an_unused_override():
    write_policy(kind="ladder")
    state.save_intent_override(
        state.IntentOverride(
            intent=GOOD, reason="r", set_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    )
    result = invoke_show()
    assert result.exit_code == 0
    assert "source: default" in result.output
    assert "not used until" in result.output
    assert f"override text (not in use): {GOOD}" in result.output


def test_show_an_unreadable_file_reports_the_note():
    write_policy(intent="Grow the economy.")
    state.intent_override_path().write_text("{broken")
    result = invoke_show()
    assert result.exit_code == 0
    assert "note: override file unreadable" in result.output
    assert state.intent_override_path().exists()


def test_show_exits_4_for_an_invalid_policy():
    state.policy_path().write_text("{nope")
    assert invoke_show().exit_code == 4
    assert invoke_show("--json").exit_code == 4


def test_show_honours_the_policy_option(tmp_path):
    elsewhere = write_policy(tmp_path / "other-policy.json", intent="From the other file.")
    result = invoke_show("--policy", str(elsewhere))
    assert result.exit_code == 0
    assert "intent: From the other file." in result.output


# --------------------------------------------------------------------------------------
# vd engine intent clear
# --------------------------------------------------------------------------------------


def invoke_clear(*args: str):
    return runner.invoke(vd_app, ["engine", "intent", "clear", *args])


def test_clear_removes_the_override_and_logs_the_reason():
    write_policy()
    assert invoke_set(GOOD, "--reason", "raided").exit_code == 0
    result = invoke_clear("--reason", "raid is over")
    assert result.exit_code == 0
    assert "cleared" in result.output
    assert not state.intent_override_path().exists()
    assert "intent override cleared -- raid is over" in strategy_text()


def test_clear_without_a_reason_logs_a_default():
    write_policy()
    assert invoke_set(GOOD, "--reason", "raided").exit_code == 0
    assert invoke_clear().exit_code == 0
    assert "intent override cleared -- no reason given" in strategy_text()


def test_clear_with_nothing_stored_exits_0_and_logs_nothing():
    result = invoke_clear("--reason", "tidy")
    assert result.exit_code == 0
    assert "no intent override" in result.output
    assert "cleared" not in strategy_text()


def test_clear_works_with_the_flag_off_and_without_any_policy():
    store()
    assert not state.policy_path().exists()
    assert invoke_clear().exit_code == 0
    assert not state.intent_override_path().exists()


def test_clear_removes_an_unreadable_file():
    state.intent_override_path().write_text("{broken")
    result = invoke_clear("--reason", "corrupt")
    assert result.exit_code == 0
    assert not state.intent_override_path().exists()


def test_intent_help_lists_the_three_commands():
    result = runner.invoke(vd_app, ["engine", "intent", "--help"])
    assert result.exit_code == 0
    for name in ("set", "show", "clear"):
        assert name in result.output


# ---- `set --policy` somewhere other than $VEYDRIFT_HOME/policy.json ----------------------


def test_set_with_an_alternate_policy_notes_which_policy_ticks_read(tmp_path):
    elsewhere = write_policy(tmp_path / "other-policy.json")
    result = invoke_set(GOOD, "--reason", "raided", "--policy", str(elsewhere))
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "ticks read" in flat and str(state.policy_path()) in flat
    assert state.load_intent_override() is not None  # still written


def test_set_with_the_home_policy_path_given_explicitly_has_no_such_note():
    path = write_policy()
    result = invoke_set(GOOD, "--reason", "raided", "--policy", str(path))
    assert result.exit_code == 0
    assert "ticks read" not in result.output


def test_set_json_carries_the_alternate_policy_note(tmp_path):
    elsewhere = write_policy(tmp_path / "other-policy.json")
    result = invoke_set(GOOD, "--reason", "raided", "--policy", str(elsewhere), "--json")
    assert any("ticks read" in w for w in json.loads(result.output)["warnings"])


def test_a_ladder_policy_with_a_three_part_ratio_in_its_intent_is_usable_by_the_cli():
    state.policy_path().write_text(json.dumps(policy_dict(kind="ladder", intent="Keep a 3:2:1 ratio")))
    result = runner.invoke(vd_app, ["engine", "intent", "show"])
    assert result.exit_code == 0, result.output
    assert "3:2:1" in result.output
