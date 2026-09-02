"""Tests for veydrift_agent.state — $VEYDRIFT_HOME resolution, agent-state.json,
KILLSWITCH detection, the tick lockfile, and `vd init`'s underlying `init_policy`.

Every test isolates `$VEYDRIFT_HOME` to a pytest tmp_path via monkeypatch, so none of
these ever touch a real `~/.veydrift`.
"""

from __future__ import annotations

import json

import pytest

from veydrift_agent import state


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "veydrift-home"
    monkeypatch.setenv("VEYDRIFT_HOME", str(home))
    return home


# --------------------------------------------------------------------------------------
# veydrift_home()
# --------------------------------------------------------------------------------------


def test_veydrift_home_created_on_first_use(isolated_home):
    assert not isolated_home.exists()
    resolved = state.veydrift_home()
    assert resolved == isolated_home
    assert resolved.is_dir()


def test_veydrift_home_default_is_dot_veydrift(monkeypatch, tmp_path):
    monkeypatch.delenv("VEYDRIFT_HOME", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    resolved = state.veydrift_home()
    assert resolved == fake_home / ".veydrift"


def test_bundled_asset_resolved_from_file_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # prove cwd is irrelevant
    path = state.bundled_asset("policy.example.json")
    assert path.name == "policy.example.json"
    assert path.exists(), "assets/policy.example.json must exist and resolve via __file__"


def test_cache_logs_ticks_dirs_created_under_home(isolated_home):
    assert state.cache_dir() == isolated_home / "cache"
    assert state.logs_dir() == isolated_home / "logs"
    assert state.ticks_dir() == isolated_home / "logs" / "ticks"
    for d in (state.cache_dir(), state.logs_dir(), state.ticks_dir()):
        assert d.is_dir()


# --------------------------------------------------------------------------------------
# KILLSWITCH
# --------------------------------------------------------------------------------------


def test_killswitch_absent_by_default():
    assert state.killswitch_active() is False


def test_killswitch_detected_once_touched(isolated_home):
    state.veydrift_home()  # ensure dir exists
    state.killswitch_path().touch()
    assert state.killswitch_active() is True


def test_killswitch_cleared_after_removal():
    state.killswitch_path().touch()
    assert state.killswitch_active() is True
    state.killswitch_path().unlink()
    assert state.killswitch_active() is False


# --------------------------------------------------------------------------------------
# vd init / init_policy
# --------------------------------------------------------------------------------------


def test_init_policy_copies_example_and_validates(isolated_home):
    from veydrift_agent.models import Policy

    dest = state.init_policy()
    assert dest == state.policy_path()
    assert dest.exists()
    policy = Policy.model_validate(json.loads(dest.read_text()))
    assert policy.wallet.startswith("0x")


def test_init_policy_refuses_to_overwrite_without_force(isolated_home):
    state.init_policy()
    with pytest.raises(state.PolicyInitError):
        state.init_policy()


def test_init_policy_force_overwrites(isolated_home):
    dest = state.init_policy()
    dest.write_text("{}")  # corrupt it
    state.init_policy(force=True)
    # a force re-copy must restore a validating policy, not leave the corruption
    from veydrift_agent.models import Policy

    Policy.model_validate(json.loads(dest.read_text()))


# --------------------------------------------------------------------------------------
# AgentState — load/save round trip, gas-day rollover, revert counting
# --------------------------------------------------------------------------------------


def test_load_agent_state_defaults_when_missing(isolated_home):
    loaded = state.load_agent_state()
    assert loaded.tick_count == 0
    assert loaded.pending is None
    assert loaded.revert_counts == {}


def test_save_and_load_round_trips(isolated_home):
    s = state.AgentState()
    s.record_tick()
    s.pending = state.PendingTx(key="664:startBuildingUpgrade:3", tx_hash="0x" + "ab" * 32)
    state.save_agent_state(s)

    reloaded = state.load_agent_state()
    assert reloaded.tick_count == 1
    assert reloaded.pending is not None
    assert reloaded.pending.tx_hash == "0x" + "ab" * 32


def test_agent_state_file_is_valid_json_on_disk(isolated_home):
    s = state.AgentState()
    s.record_tick()
    state.save_agent_state(s)
    raw = json.loads(state.agent_state_path().read_text())
    assert raw["tick_count"] == 1


def test_last_proposal_fingerprint_defaults_none_and_round_trips(isolated_home):
    assert state.load_agent_state().last_proposal_fingerprint is None

    s = state.AgentState()
    s.last_proposal_fingerprint = "deadbeef"
    state.save_agent_state(s)

    assert state.load_agent_state().last_proposal_fingerprint == "deadbeef"


def test_agent_state_missing_fingerprint_field_loads_with_default(isolated_home):
    """An agent-state.json written before this field existed must still load -- additive
    field, not a breaking rename (AgentState is not part of the frozen models.py
    contract, see AGENTS.md §4)."""
    state.agent_state_path().parent.mkdir(parents=True, exist_ok=True)
    state.agent_state_path().write_text(json.dumps({"version": 1, "tick_count": 3}))

    loaded = state.load_agent_state()
    assert loaded.tick_count == 3
    assert loaded.last_proposal_fingerprint is None


def test_last_unresolved_onchain_proposal_defaults_none_and_round_trips(isolated_home):
    from datetime import UTC, datetime

    assert state.load_agent_state().last_unresolved_onchain_proposal is None

    s = state.AgentState()
    s.last_unresolved_onchain_proposal = state.UnresolvedProposal(
        ts=datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC),
        planet_id=664,
        function="startBuildingUpgrade",
        entity_id=0,
        entity_name="Metal Mine",
        target_level=5,
    )
    state.save_agent_state(s)

    reloaded = state.load_agent_state().last_unresolved_onchain_proposal
    assert reloaded is not None
    assert reloaded.planet_id == 664
    assert reloaded.function == "startBuildingUpgrade"
    assert reloaded.target_level == 5


def test_agent_state_missing_unresolved_proposal_field_loads_with_default(isolated_home):
    """Same additive-field guarantee as last_proposal_fingerprint above."""
    state.agent_state_path().parent.mkdir(parents=True, exist_ok=True)
    state.agent_state_path().write_text(json.dumps({"version": 1, "tick_count": 3}))

    loaded = state.load_agent_state()
    assert loaded.last_unresolved_onchain_proposal is None


def test_record_gas_spent_accumulates_within_a_day():
    s = state.AgentState()
    from datetime import UTC, datetime

    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    s.record_gas_spent(100, now=now)
    s.record_gas_spent(50, now=now.replace(hour=11))
    assert s.gas_spent_today(now=now.replace(hour=12)) == 150


def test_record_gas_spent_resets_on_new_day():
    s = state.AgentState()
    from datetime import UTC, datetime

    day1 = datetime(2026, 8, 12, 23, 0, tzinfo=UTC)
    day2 = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
    s.record_gas_spent(100, now=day1)
    assert s.gas_spent_today(now=day1) == 100
    s.record_gas_spent(30, now=day2)
    assert s.gas_spent_today(now=day2) == 30  # not 130 -- new day resets the counter


def test_gas_spent_today_is_zero_on_a_different_day_even_without_recording():
    s = state.AgentState()
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    s.record_gas_spent(999, now=now)
    assert s.gas_spent_today(now=now + timedelta(days=2)) == 0


def test_record_revert_increments_per_key():
    s = state.AgentState()
    key = "664:startBuildingUpgrade:3"
    assert s.record_revert(key) == 1
    assert s.record_revert(key) == 2
    assert s.revert_counts[key] == 2
    assert s.revert_counts.get("other-key", 0) == 0


# --------------------------------------------------------------------------------------
# radar-state.json
# --------------------------------------------------------------------------------------


def test_load_radar_state_defaults_when_missing(isolated_home):
    loaded = state.load_radar_state()
    assert loaded.wallets == {}


def test_load_radar_state_defaults_on_empty_file(isolated_home):
    state.radar_state_path().write_text("")
    loaded = state.load_radar_state()
    assert loaded.wallets == {}


def test_save_and_load_radar_state_round_trips(isolated_home):
    s = state.RadarState()
    s.wallets["0xabc"] = state.WalletRadarState(last_seen_mission_id="61740")
    state.save_radar_state(s)

    reloaded = state.load_radar_state()
    assert reloaded.wallets["0xabc"].last_seen_mission_id == "61740"


def test_radar_state_file_is_valid_json_on_disk(isolated_home):
    s = state.RadarState()
    s.wallets["0xabc"] = state.WalletRadarState(last_seen_mission_id="1")
    state.save_radar_state(s)
    raw = json.loads(state.radar_state_path().read_text())
    assert raw["wallets"]["0xabc"]["last_seen_mission_id"] == "1"


def test_radar_state_supports_multiple_wallets_independently(isolated_home):
    """Not folded into AgentState (implicitly single-wallet) precisely because a
    standalone `vd radar check --alliance-id` run can watch many wallets that are not
    policy.wallet at all -- see radar.py's module docstring."""
    s = state.RadarState()
    s.wallets["0xaaa"] = state.WalletRadarState(last_seen_mission_id="1")
    s.wallets["0xbbb"] = state.WalletRadarState(last_seen_mission_id="2")
    state.save_radar_state(s)

    reloaded = state.load_radar_state()
    assert reloaded.wallets["0xaaa"].last_seen_mission_id == "1"
    assert reloaded.wallets["0xbbb"].last_seen_mission_id == "2"


def test_radar_state_missing_wallets_field_loads_with_default(isolated_home):
    """A radar-state.json written before a given wallet was ever checked must still
    load with that wallet simply absent -- additive convention, same as AgentState's
    own missing-field tests above."""
    state.radar_state_path().write_text(json.dumps({"version": 1}))
    loaded = state.load_radar_state()
    assert loaded.wallets == {}


# --------------------------------------------------------------------------------------
# Tick lockfile
# --------------------------------------------------------------------------------------


def test_tick_lock_allows_sequential_acquisition(isolated_home):
    with state.tick_lock():
        pass
    with state.tick_lock():
        pass  # a second, later acquisition after the first released must succeed


def test_tick_lock_blocks_concurrent_acquisition_in_a_subprocess(isolated_home):
    """The lock is a process-level `flock`, so re-entering it from the *same* process
    with a nested `with` doesn't contend (POSIX flock is per-fd, and Python doesn't
    re-open the same fd here) -- the real contention scenario is two separate
    processes, which this test exercises via a subprocess holding the lock."""
    import subprocess
    import sys
    import time

    home = isolated_home
    holder_script = (
        "import time, os\n"
        "os.environ['VEYDRIFT_HOME'] = " + repr(str(home)) + "\n"
        "import sys; sys.path.insert(0, " + repr(str(_src_dir())) + ")\n"
        "from veydrift_agent import state\n"
        "with state.tick_lock():\n"
        "    print('LOCKED', flush=True)\n"
        "    time.sleep(2)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script], stdout=subprocess.PIPE, text=True
    )
    try:
        line = proc.stdout.readline()
        assert line.strip() == "LOCKED"
        time.sleep(0.2)  # let the child actually hold the flock
        with pytest.raises(state.TickLockedError), state.tick_lock():
            pass  # pragma: no cover - must not be reached
    finally:
        proc.wait(timeout=5)


def _src_dir():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "src"
