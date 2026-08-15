"""Tests for veydrift_agent.tick — the loop entrypoint.

Network- and subprocess-touching internals (`_fetch_snapshot`, `_live_addresses`,
`_walletctl_*`) are monkeypatched at the module level rather than mocked at the HTTP
layer: `read.py` and `plan.py` already have their own dedicated test suites (WP1/WP2),
so these tests isolate `tick.py`'s own contribution -- step ordering, the tier-1
dry-run floor, lockfile behaviour, and which sinks get written when.

`vd tick --dry-run` against the **live** API is verified separately (by hand, per the WP
brief) precisely because these tests must not depend on network access.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from veydrift_agent import guard as guard_mod
from veydrift_agent import http, log, tick
from veydrift_agent import plan as plan_mod
from veydrift_agent.models import (
    Action,
    ActionKind,
    EnergyBalance,
    Entity,
    GuardStatus,
    Limits,
    PlanetSnapshot,
    Policy,
    Resources,
    Snapshot,
    Tier,
    UnsignedTx,
)
from veydrift_agent.state import AgentState, PendingTx, UnresolvedProposal, init_policy, load_agent_state, save_agent_state

runner = CliRunner()
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
BASE = http.API_BASE_URL


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "veydrift-home"
    monkeypatch.setenv("VEYDRIFT_HOME", str(home))
    return home


def _write_policy(**overrides):
    policy = json.loads((Path(__file__).parent.parent / "assets" / "policy.example.json").read_text())
    policy.update(overrides)
    init_policy()
    from veydrift_agent.state import policy_path

    policy_path().write_text(json.dumps(policy))
    return policy_path()


def _healthy_snapshot(**overrides) -> Snapshot:
    planet = PlanetSnapshot(
        planet_id=664,
        coordinates="7:181:14",
        fields_used=7,
        fields_total=174,
        resources_as_of_now=Resources(metal=1000, crystal=1000, deuterium=0),
        storage_caps=Resources(metal=10000, crystal=10000, deuterium=10000),
        energy=EnergyBalance(produced=100, required=0, scale_bps=10_000, solar_satellite_energy=4),
        buildings=[Entity(id=3, name="Solar Plant", level=0, cost=Resources(metal=75, crystal=30))],
    )
    base = dict(
        taken_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
        wallet=WALLET,
        health_ok=True,
        deployment_abi_hash=guard_mod.PINNED_ABI_HASH,
        latest_indexed_block=100,
        planets=[planet],
    )
    base.update(overrides)
    return Snapshot(**base)


def _build_action() -> Action:
    return Action(
        kind=ActionKind.BUILD,
        function="startBuildingUpgrade",
        planet_id=664,
        entity_id=3,
        entity_name="Solar Plant",
        target_level=1,
        cost=Resources(metal=75, crystal=30),
        rule="6:building-queue-empty",
        rationale="test action",
    )


def _economy_policy(**overrides) -> Policy:
    base = dict(
        wallet=WALLET,
        tier=Tier.ECONOMY,
        planets=[664],
        limits=Limits(gas_per_tx_wei=10**16, gas_per_day_wei=10**18, eth_gas_floor_wei=0),
    )
    base.update(overrides)
    return Policy(**base)


# --------------------------------------------------------------------------------------
# --dry-run is forced at tier 1, independent of what's passed.
# --------------------------------------------------------------------------------------


def test_effective_dry_run_forced_true_at_advisor_tier():
    from veydrift_agent.models import Limits, Policy, Tier

    policy = Policy(
        wallet=WALLET,
        tier=Tier.ADVISOR,
        limits=Limits(gas_per_tx_wei=1, gas_per_day_wei=1, eth_gas_floor_wei=1),
    )
    assert tick._effective_dry_run(policy, False) is True
    assert tick._effective_dry_run(policy, True) is True


def test_effective_dry_run_respects_the_flag_at_economy_tier():
    from veydrift_agent.models import Limits, Policy, Tier

    policy = Policy(
        wallet=WALLET,
        tier=Tier.ECONOMY,
        limits=Limits(gas_per_tx_wei=1, gas_per_day_wei=1, eth_gas_floor_wei=1),
    )
    assert tick._effective_dry_run(policy, True) is True
    assert tick._effective_dry_run(policy, False) is False


# --------------------------------------------------------------------------------------
# vd tick init
# --------------------------------------------------------------------------------------


def test_init_command_writes_policy(isolated_home):
    result = runner.invoke(tick.app, ["init"])
    assert result.exit_code == 0, result.output
    from veydrift_agent.state import policy_path

    assert policy_path().exists()


# --------------------------------------------------------------------------------------
# killswitch — halts before any network call beyond /health.
# --------------------------------------------------------------------------------------


@respx.mock
def test_killswitch_halts_and_touches_only_health(isolated_home, monkeypatch):
    _write_policy()
    from veydrift_agent.state import killswitch_path

    killswitch_path().touch()
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(200, json={"ok": True, "readiness": {"ready": True}})
    )

    def _boom(*a, **kw):  # any snapshot fetch during a killswitch halt is a bug
        raise AssertionError("must not fetch a snapshot while KILLSWITCH is active")

    monkeypatch.setattr(tick, "_fetch_snapshot", _boom)
    monkeypatch.setattr(tick, "_live_addresses", _boom)

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output

    proposals = log.read_proposals()
    assert len(proposals) == 1
    assert proposals[0]["kind"] == "halt"
    assert not log.actions_path().exists()


# --------------------------------------------------------------------------------------
# The dry-run end-to-end path (tier 1): proposal logged, tick markdown written,
# actions.jsonl NEVER created, send NEVER called.
# --------------------------------------------------------------------------------------


def _patch_common(monkeypatch, *, snapshot=None, action=None, live_addresses=None, unsigned_tx=None, gas=None, built_tx_path=None):
    monkeypatch.setattr(tick, "_fetch_snapshot", lambda *a, **kw: snapshot or _healthy_snapshot())
    monkeypatch.setattr(plan_mod, "plan_next_action", lambda *a, **kw: action or _build_action())
    monkeypatch.setattr(tick, "_live_addresses", lambda: live_addresses)
    monkeypatch.setattr(
        tick,
        "_walletctl_build",
        lambda act, **kw: (unsigned_tx, gas, None, built_tx_path) if act.is_onchain() else (None, None, None, None),
    )
    monkeypatch.setattr(tick, "_walletctl_eth_balance_wei", lambda **kw: None)

    def _send_should_not_be_called(*a, **kw):
        raise AssertionError("walletctl send must never be called at tier 1")

    monkeypatch.setattr(tick, "_walletctl_send", _send_should_not_be_called)


def test_dry_run_writes_proposal_and_tick_report_never_writes_actions(isolated_home, monkeypatch):
    _write_policy()
    tx = UnsignedTx(to="0xf397910F005151b09644228573a4353818D3755d", data="0x165715e3" + "00" * 32, gas=None)
    _patch_common(monkeypatch, live_addresses={"0xf397910F005151b09644228573a4353818D3755d"}, unsigned_tx=tx)

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "NOT SUBMITTED" in result.output

    proposals = log.read_proposals()
    assert len(proposals) == 1
    assert proposals[0]["function"] == "startBuildingUpgrade"
    assert proposals[0]["executed"] is False
    # tier 1's own tier gate always blocks an on-chain function -- see guard.py.
    assert proposals[0]["guard_decision"] == "block"

    assert not log.actions_path().exists()
    tick_files = list(log.ticks_dir().glob("*.md"))
    assert len(tick_files) == 1
    assert "TICK #1" in tick_files[0].read_text()


def test_dry_run_at_tier1_never_sends_even_when_guard_would_allow(isolated_home, monkeypatch):
    """Belt and braces: even if a bug made guard.py ALLOW an on-chain action at tier 1,
    `_run_tick`'s own `policy.tier is not Tier.ADVISOR` check must independently prevent
    `_walletctl_send` from ever being reached."""
    _write_policy()
    tx = UnsignedTx(to="0xf397910F005151b09644228573a4353818D3755d", data="0x165715e3" + "00" * 32, gas=100_000)
    _patch_common(monkeypatch, live_addresses={"0xf397910F005151b09644228573a4353818D3755d"}, unsigned_tx=tx, gas=100_000)

    import veydrift_agent.guard as guard_module

    monkeypatch.setattr(
        guard_module,
        "evaluate_guardrails",
        lambda *a, **kw: guard_module.GuardReport(decision=guard_module.Decision.ALLOW, verdicts=[]),
    )
    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert not log.actions_path().exists()


def test_noop_action_produces_no_tx_and_no_extra_network_calls(isolated_home, monkeypatch):
    _write_policy()
    noop = Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="nothing to do")
    live_addr_calls = []
    monkeypatch.setattr(tick, "_fetch_snapshot", lambda *a, **kw: _healthy_snapshot())
    monkeypatch.setattr(plan_mod, "plan_next_action", lambda *a, **kw: noop)
    monkeypatch.setattr(tick, "_live_addresses", lambda: live_addr_calls.append(1) or None)

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert live_addr_calls == [], "an off-chain action must not trigger a live-address fetch"
    proposals = log.read_proposals()
    assert proposals[0]["kind"] == "noop"


# --------------------------------------------------------------------------------------
# --format json
# --------------------------------------------------------------------------------------


def test_format_json_prints_valid_json(isolated_home, monkeypatch):
    _write_policy()
    _patch_common(monkeypatch)
    result = runner.invoke(tick.app, ["--dry-run", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tick"] == 1
    assert "action" in payload and "guard" in payload


def test_expected_effect_appears_in_report_and_proposal_record(isolated_home, monkeypatch):
    action = _build_action().model_copy(update={"expected_effect": "produced energy 100 -> 150"})
    _write_policy()
    _patch_common(monkeypatch, action=action)

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "effect: produced energy 100 -> 150" in result.output

    proposals = log.read_proposals()
    assert proposals[0]["expected_effect"] == "produced energy 100 -> 150"


def test_empty_expected_effect_omits_the_effect_line(isolated_home, monkeypatch):
    _write_policy()
    _patch_common(monkeypatch)  # default _build_action() has no expected_effect

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "effect:" not in result.output


# --------------------------------------------------------------------------------------
# --readiness — reports without running a tick.
# --------------------------------------------------------------------------------------


def test_readiness_does_not_advance_tick_count(isolated_home, monkeypatch):
    _write_policy()
    state = AgentState()
    state.record_tick()
    state.record_tick()
    save_agent_state(state)

    result = runner.invoke(tick.app, ["--readiness"])
    assert result.exit_code == 0, result.output
    assert "tick_count:        2" in result.output
    assert "divergence:" in result.output
    assert "guardrails_fired (substantive" in result.output
    assert "structural_tier_blocks:" in result.output

    reloaded = load_agent_state()
    assert reloaded.tick_count == 2  # unchanged -- --readiness must not run a tick


# --------------------------------------------------------------------------------------
# Lockfile
# --------------------------------------------------------------------------------------


def test_concurrent_tick_is_skipped_not_crashed(isolated_home, monkeypatch):
    from veydrift_agent.state import tick_lock

    _write_policy()
    _patch_common(monkeypatch)

    with tick_lock():
        result = runner.invoke(tick.app, ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert not log.proposals_path().exists() or len(log.read_proposals()) == 0


# --------------------------------------------------------------------------------------
# Invalid policy is a hard stop.
# --------------------------------------------------------------------------------------


def test_invalid_policy_json_is_a_hard_stop(isolated_home):
    from veydrift_agent.state import policy_path

    init_policy()
    policy_path().write_text("{not valid json")
    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code != 0


def test_policy_with_unknown_key_is_a_hard_stop(isolated_home):
    """Policy.model_config sets extra='forbid' -- an unknown key must be a hard error,
    never silently ignored (docs/SPEC.md §5.6)."""
    from veydrift_agent.state import policy_path

    policy = json.loads((Path(__file__).parent.parent / "assets" / "policy.example.json").read_text())
    policy["totally_unknown_field"] = True
    init_policy()
    policy_path().write_text(json.dumps(policy))
    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code != 0


def test_missing_policy_is_a_hard_stop(isolated_home):
    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code != 0


# --------------------------------------------------------------------------------------
# Action -> walletctl JSON mapping
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "function,extra,expected_args",
    [
        ("startBuildingUpgrade", {"planet_id": 664, "entity_id": 3}, [664, 3]),
        ("startResearch", {"planet_id": 664, "entity_id": 0}, [664, 0]),
        ("startShipProduction", {"planet_id": 664, "entity_id": 9, "quantity": 2}, [664, 9, 2]),
        ("startDefenseProduction", {"planet_id": 664, "entity_id": 0, "quantity": 1}, [664, 0, 1]),
        ("resolveFleetMission", {"mission_id": 42}, [42]),
        ("settlePlanet", {"planet_id": 664}, [664]),
    ],
)
def test_action_to_walletctl_json_maps_args_positionally(function, extra, expected_args):
    action = Action(kind=ActionKind.BUILD, function=function, rule="x", rationale="x", **extra)
    built = tick._action_to_walletctl_json(action)
    assert built["function"] == function
    assert built["args"] == expected_args


def test_action_to_walletctl_json_raises_on_unknown_function():
    action = Action(kind=ActionKind.BUILD, function="someUnknownFunction", rule="x", rationale="x")
    with pytest.raises(ValueError):
        tick._action_to_walletctl_json(action)


# --------------------------------------------------------------------------------------
# FIX 1 — walletctl build's cost must cross the unit boundary correctly: `gas` is gas
# *units* (~1.5e5 on Base), `estimatedCostWei` is the wei-scale figure (gas * price).
# `_walletctl_build` must parse `gas_cost_wei` from `estimatedCostWei`, never from `gas`
# -- the confirmed defect was that it fed `gas` straight through, so guard.py's `gas`
# gate compared units against wei-scale ceilings and could never fire.
# --------------------------------------------------------------------------------------


def test_walletctl_build_parses_gas_cost_wei_from_estimated_cost_wei_not_gas_units(isolated_home, monkeypatch):
    action = _build_action()
    built_payload = {
        "to": "0xf397910F005151b09644228573a4353818D3755d",
        "data": "0x165715e3" + "00" * 32,
        "value": "0",
        "chainId": 8453,
        "gas": "156540",
        "maxFeePerGas": "12345678",
        "estimatedCostWei": "1932714120",  # 156540 * 12345678 -- wei-scale, NOT 156540
    }

    def _fake_run_walletctl(*args, timeout=None):
        out_path = Path(args[args.index("--out") + 1])
        out_path.write_text(json.dumps(built_payload))
        return subprocess.CompletedProcess(args, 0, stdout="wrote " + str(out_path), stderr="")

    monkeypatch.setattr(tick, "_run_walletctl", _fake_run_walletctl)

    unsigned_tx, gas_cost_wei, error, built_tx_path = tick._walletctl_build(action, provider="keystore")

    assert error is None
    assert unsigned_tx is not None
    assert unsigned_tx.gas == 156_540  # gas UNITS, preserved on the tx model as a gas-limit hint
    assert gas_cost_wei == 1_932_714_120  # the WEI figure -- not 156_540
    assert built_tx_path is not None


def test_walletctl_build_gas_cost_wei_none_when_estimated_cost_wei_is_null(isolated_home, monkeypatch):
    """`estimatedCostWei` may legitimately be `null` (e.g. no provider configured to
    estimate `--from`) -- must come through as `None`, never coerced to `0`."""
    action = _build_action()
    built_payload = {
        "to": "0xf397910F005151b09644228573a4353818D3755d",
        "data": "0x165715e3" + "00" * 32,
        "value": "0",
        "chainId": 8453,
        "gas": None,
        "maxFeePerGas": None,
        "estimatedCostWei": None,
    }

    def _fake_run_walletctl(*args, timeout=None):
        out_path = Path(args[args.index("--out") + 1])
        out_path.write_text(json.dumps(built_payload))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(tick, "_run_walletctl", _fake_run_walletctl)
    _unsigned_tx, gas_cost_wei, error, _path = tick._walletctl_build(action, provider="keystore")
    assert error is None
    assert gas_cost_wei is None


def test_walletctl_build_cost_crosses_the_unit_boundary_into_the_gas_gate(isolated_home, monkeypatch):
    """The full boundary-crossing regression test the FIX 1 brief calls for: a realistic
    `walletctl build` payload (gas units ~1.5e5, price ~1.2e7 wei) must make the `gas`
    gate compare ~1.9e9 wei against the ceiling -- not 156_540. A policy ceiling placed
    strictly between the two numbers proves which one the gate actually used: PASSing
    would mean the old units-vs-wei bug is back."""
    action = _build_action()
    built_payload = {
        "to": "0xf397910F005151b09644228573a4353818D3755d",
        "data": "0x165715e3" + "00" * 32,
        "value": "0",
        "chainId": 8453,
        "gas": "156540",
        "maxFeePerGas": "12345678",
        "estimatedCostWei": "1932714120",
    }

    def _fake_run_walletctl(*args, timeout=None):
        out_path = Path(args[args.index("--out") + 1])
        out_path.write_text(json.dumps(built_payload))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(tick, "_run_walletctl", _fake_run_walletctl)
    unsigned_tx, gas_cost_wei, _error, _path = tick._walletctl_build(action, provider="keystore")

    # A ceiling of 1_000_000 wei is far below the real cost (~1.9e9 wei) but far ABOVE the
    # raw gas-unit figure (156_540) -- if units leaked through instead of wei, this would
    # incorrectly PASS.
    policy = Policy(
        wallet=WALLET,
        tier=Tier.ECONOMY,
        limits=Limits(gas_per_tx_wei=1_000_000, gas_per_day_wei=10**18, eth_gas_floor_wei=0),
    )
    report = guard_mod.evaluate_guardrails(
        action,
        _healthy_snapshot(),
        policy,
        AgentState(),
        live_addresses={unsigned_tx.to},
        unsigned_tx=unsigned_tx,
        gas_cost_wei=gas_cost_wei,
        eth_balance_wei=10**18,
        now=datetime.now(UTC),
    )
    gas_verdict = next(v for v in report.verdicts if v.gate == "gas")
    assert gas_verdict.status is GuardStatus.BLOCK
    assert "1932714120" in gas_verdict.detail
    assert "156540" not in gas_verdict.detail


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


def test_reconcile_pending_clears_once_indexed_block_covers_it():
    from veydrift_agent.state import PendingTx

    state = AgentState(pending=PendingTx(key="k", tx_hash="0x" + "aa" * 32, block=100))
    unreconciled = tick._reconcile_pending(state, indexed_block=100, now=datetime.now(UTC))
    assert unreconciled is False
    assert state.pending is None


def test_reconcile_pending_stays_unreconciled_when_index_lags():
    from veydrift_agent.state import PendingTx

    state = AgentState(pending=PendingTx(key="k", tx_hash="0x" + "aa" * 32, block=100))
    unreconciled = tick._reconcile_pending(state, indexed_block=50, now=datetime.now(UTC))
    assert unreconciled is True
    assert state.pending is not None


def test_reconcile_pending_passes_trivially_with_nothing_pending():
    state = AgentState()
    assert tick._reconcile_pending(state, indexed_block=100, now=datetime.now(UTC)) is False


# --------------------------------------------------------------------------------------
# FIX 2 — a receipt discovered by _reconcile_pending (because _send_and_await's own poll
# timed out first) must be recorded exactly as if _send_and_await had seen it directly:
# a revert calls record_revert and charges gas; a late success increments
# executions_count. Neither path may double-charge gas across the two call sites.
# --------------------------------------------------------------------------------------


def test_reconcile_pending_records_a_revert_discovered_on_a_later_tick(isolated_home, monkeypatch):
    state = AgentState(pending=PendingTx(key="664:startBuildingUpgrade:3", tx_hash="0x" + "aa" * 32, block=None))
    monkeypatch.setattr(
        tick,
        "_walletctl_receipt",
        lambda tx_hash: {"status": "reverted", "blockNumber": "0x64", "actualCostWei": "500000000"},
    )
    unreconciled = tick._reconcile_pending(state, indexed_block=None, now=datetime.now(UTC))
    assert unreconciled is False
    assert state.pending is None
    assert state.revert_counts["664:startBuildingUpgrade:3"] == 1
    assert state.gas_spent_today(now=datetime.now(UTC)) == 500_000_000


def test_reconcile_pending_does_not_double_charge_gas_already_charged_at_send_time(isolated_home, monkeypatch):
    """`pending.gas_wei` is set once a cost has already been charged (by `_send_and_await`
    itself, e.g. from the pre-send estimate when the actual wasn't known yet). If a later
    tick's reconcile discovers the real `actualCostWei`, it must NOT charge the ledger a
    second time."""
    pending = PendingTx(key="k", tx_hash="0x" + "aa" * 32, block=None, gas_wei=100)  # already charged 100 wei
    state = AgentState(pending=pending)
    monkeypatch.setattr(
        tick,
        "_walletctl_receipt",
        lambda tx_hash: {"status": "reverted", "blockNumber": "0x64", "actualCostWei": "999999999"},
    )
    tick._reconcile_pending(state, indexed_block=None, now=datetime.now(UTC))
    assert state.gas_spent_today(now=datetime.now(UTC)) == 0  # NOT 999_999_999 -- already charged at send time


def test_reconcile_pending_records_a_late_success_and_increments_executions(isolated_home, monkeypatch):
    state = AgentState(pending=PendingTx(key="k", tx_hash="0x" + "aa" * 32, block=None))
    monkeypatch.setattr(
        tick,
        "_walletctl_receipt",
        lambda tx_hash: {"status": "success", "blockNumber": "0x64", "actualCostWei": "300000000"},
    )
    unreconciled = tick._reconcile_pending(state, indexed_block=None, now=datetime.now(UTC))
    assert unreconciled is True  # still waiting on the index-lag half of reconciliation
    assert state.executions_count == 1
    assert state.gas_spent_today(now=datetime.now(UTC)) == 300_000_000
    assert state.pending is not None
    assert state.pending.block == 100


def test_reconcile_pending_never_treats_a_statusless_receipt_as_success(isolated_home, monkeypatch):
    """A receipt with no `status` field (fetch degraded, or an unexpected shape) must not
    be silently treated as a success -- it just leaves the pending tx unresolved."""
    state = AgentState(pending=PendingTx(key="k", tx_hash="0x" + "aa" * 32, block=None))
    monkeypatch.setattr(tick, "_walletctl_receipt", lambda tx_hash: {"blockNumber": None})
    unreconciled = tick._reconcile_pending(state, indexed_block=None, now=datetime.now(UTC))
    assert unreconciled is True
    assert state.executions_count == 0
    assert state.revert_counts == {}


# --------------------------------------------------------------------------------------
# FIX 2 — _send_and_await itself: a reverted tx must never be counted as a success, must
# call record_revert, must still be written to actions.jsonl (hiding a revert is worse
# than recording it), and enough reverts must actually trip guard.py's revert_streak
# gate. An unknown outcome (no status ever observed) must not be treated as success
# either.
# --------------------------------------------------------------------------------------

_LIVE_ADDR = "0xf397910F005151b09644228573a4353818D3755d"


def test_send_and_await_reverted_tx_not_counted_as_success(isolated_home, monkeypatch):
    policy = _economy_policy()
    action = _build_action()
    unsigned_tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    agent_state = AgentState()
    tx_hash_sent = "0x" + "bb" * 32

    monkeypatch.setattr(tick, "_walletctl_send", lambda tx_path, *, tier, provider: (tx_hash_sent, None))
    monkeypatch.setattr(
        tick,
        "_walletctl_receipt",
        lambda tx_hash: {"status": "reverted", "blockNumber": "0x64", "actualCostWei": "555000000"},
    )

    executed, outcome, tx_hash = tick._send_and_await(
        policy, agent_state, action, unsigned_tx, _healthy_snapshot(), datetime.now(UTC), gas_cost_wei_estimate=1_000_000
    )

    assert executed is False
    assert outcome == "reverted"
    assert tx_hash == tx_hash_sent
    assert agent_state.executions_count == 0
    assert agent_state.revert_counts[guard_mod.idempotency_key(action)] == 1
    assert agent_state.pending is None  # nothing left to wait on -- it already landed
    assert agent_state.gas_spent_today(now=datetime.now(UTC)) == 555_000_000  # actual, not the estimate

    actions = log.read_actions()
    assert len(actions) == 1
    assert actions[0]["status"] == "reverted"
    assert actions[0]["tx_hash"] == tx_hash_sent


def test_send_and_await_success_counts_as_executed(isolated_home, monkeypatch):
    policy = _economy_policy()
    action = _build_action()
    unsigned_tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    agent_state = AgentState()
    tx_hash_sent = "0x" + "cc" * 32

    monkeypatch.setattr(tick, "_walletctl_send", lambda tx_path, *, tier, provider: (tx_hash_sent, None))
    monkeypatch.setattr(
        tick,
        "_walletctl_receipt",
        lambda tx_hash: {"status": "success", "blockNumber": "0x64", "actualCostWei": "444000000"},
    )
    monkeypatch.setattr(tick, "_await_indexed", lambda **kw: True)

    executed, outcome, tx_hash = tick._send_and_await(
        policy, agent_state, action, unsigned_tx, _healthy_snapshot(), datetime.now(UTC), gas_cost_wei_estimate=1_000_000
    )

    assert executed is True
    assert outcome == "success"
    assert agent_state.executions_count == 1
    assert agent_state.revert_counts == {}
    assert agent_state.gas_spent_today(now=datetime.now(UTC)) == 444_000_000
    actions = log.read_actions()
    assert actions[0]["status"] == "success"


def test_send_and_await_unknown_outcome_not_counted_as_success(isolated_home, monkeypatch):
    """No status ever observed within the poll window -- must escalate to unknown, not
    assume success."""
    policy = _economy_policy()
    action = _build_action()
    unsigned_tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    agent_state = AgentState()

    monkeypatch.setattr(tick, "_walletctl_send", lambda tx_path, *, tier, provider: ("0x" + "dd" * 32, None))
    monkeypatch.setattr(tick, "_walletctl_receipt", lambda tx_hash: None)  # receipt never available
    monkeypatch.setattr(tick, "_INDEX_POLL_INTERVAL_S", 0)  # don't actually sleep in the test
    monkeypatch.setattr(tick, "_RECEIPT_WAIT_S", 0)  # expire the poll immediately

    executed, outcome, tx_hash = tick._send_and_await(
        policy, agent_state, action, unsigned_tx, _healthy_snapshot(), datetime.now(UTC), gas_cost_wei_estimate=1_000_000
    )

    assert executed is False
    assert outcome == "unknown"
    assert agent_state.executions_count == 0
    assert agent_state.revert_counts == {}
    assert agent_state.pending is not None  # left in place for the next tick to re-check
    actions = log.read_actions()
    assert actions[0]["status"] == "unknown"


def test_send_and_await_send_failure_returns_send_failed_and_writes_nothing(isolated_home, monkeypatch):
    policy = _economy_policy()
    action = _build_action()
    unsigned_tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    agent_state = AgentState()

    monkeypatch.setattr(tick, "_walletctl_send", lambda tx_path, *, tier, provider: (None, "boom"))

    executed, outcome, tx_hash = tick._send_and_await(
        policy, agent_state, action, unsigned_tx, _healthy_snapshot(), datetime.now(UTC), gas_cost_wei_estimate=1_000_000
    )
    assert (executed, outcome, tx_hash) == (False, "send_failed", None)
    assert agent_state.pending is None
    assert not log.actions_path().exists()


def test_revert_streak_gate_blocks_after_on_revert_count_reverts(isolated_home, monkeypatch):
    """The end-to-end proof that Fix 2 closes the loop: enough reverts recorded via
    `_send_and_await` must actually trip guard.py's `revert_streak` gate -- previously
    dead config because `record_revert` was called by no production code."""
    policy = _economy_policy()
    action = _build_action()
    unsigned_tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    agent_state = AgentState()

    monkeypatch.setattr(tick, "_walletctl_send", lambda tx_path, *, tier, provider: ("0x" + "ee" * 32, None))
    monkeypatch.setattr(
        tick, "_walletctl_receipt", lambda tx_hash: {"status": "reverted", "blockNumber": "0x64", "actualCostWei": "1"}
    )

    for _ in range(policy.escalation.on_revert_count):
        tick._send_and_await(
            policy, agent_state, action, unsigned_tx, _healthy_snapshot(), datetime.now(UTC), gas_cost_wei_estimate=1
        )

    report = guard_mod.evaluate_guardrails(
        action,
        _healthy_snapshot(),
        policy,
        agent_state,
        live_addresses={_LIVE_ADDR},
        unsigned_tx=unsigned_tx,
        gas_cost_wei=1_000,
        eth_balance_wei=10**18,
        now=datetime.now(UTC),
    )
    revert_verdict = next(v for v in report.verdicts if v.gate == "revert_streak")
    assert revert_verdict.status is GuardStatus.ESCALATE
    assert report.decision is guard_mod.Decision.ESCALATE


# --------------------------------------------------------------------------------------
# FIX 3 — wallet_engine.require_confirmation must actually gate sending: `true` (the
# default) builds/guards/proposes but stops short of `walletctl send`, printing the exact
# command a human should run instead; `false` sends automatically, as before.
# --------------------------------------------------------------------------------------


def _allow_guard(monkeypatch):
    """Force guard.evaluate_guardrails to ALLOW, the same trick
    test_dry_run_at_tier1_never_sends_even_when_guard_would_allow already uses -- lets
    these tests isolate the require_confirmation branch without needing every one of the
    16 gates to genuinely pass."""
    import veydrift_agent.guard as guard_module

    monkeypatch.setattr(
        guard_module,
        "evaluate_guardrails",
        lambda *a, **kw: guard_module.GuardReport(decision=guard_module.Decision.ALLOW, verdicts=[]),
    )


def test_require_confirmation_true_skips_send_and_prints_the_command(isolated_home, monkeypatch, tmp_path):
    _write_policy(tier="economy")  # wallet_engine.require_confirmation defaults to true
    built_tx_path = tmp_path / "built-tx.json"
    tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    _patch_common(monkeypatch, live_addresses={_LIVE_ADDR}, unsigned_tx=tx, gas=1_000_000_000, built_tx_path=built_tx_path)
    _allow_guard(monkeypatch)

    result = runner.invoke(tick.app, [])  # NOT --dry-run
    assert result.exit_code == 0, result.output  # Fix 3: ends cleanly, not as an error
    # Rich's Panel word-wraps long lines across the box border, so check the pieces
    # rather than one exact substring; the tick markdown file (never wrapped) is the
    # more reliable place to check the whole command in one place.
    assert "AWAITING HUMAN CONFIRMATION" in result.output
    assert "walletctl send --tx" in result.output
    assert "--confirm" in result.output
    tick_files = list(log.ticks_dir().glob("*.md"))
    assert f"walletctl send --tx {built_tx_path} --confirm" in tick_files[0].read_text()
    assert not log.actions_path().exists()  # _walletctl_send stub (in _patch_common) would raise if reached

    proposals = log.read_proposals()
    assert proposals[0]["executed"] is False


def test_require_confirmation_false_sends_automatically(isolated_home, monkeypatch, tmp_path):
    _write_policy(tier="economy", wallet_engine={"provider": "keystore", "require_confirmation": False})
    built_tx_path = tmp_path / "built-tx.json"
    tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    _patch_common(monkeypatch, live_addresses={_LIVE_ADDR}, unsigned_tx=tx, gas=1_000_000_000, built_tx_path=built_tx_path)
    _allow_guard(monkeypatch)

    calls = []

    def _fake_send_and_await(*a, **kw):
        calls.append((a, kw))
        return True, "success", "0x" + "cc" * 32

    monkeypatch.setattr(tick, "_send_and_await", _fake_send_and_await)

    result = runner.invoke(tick.app, [])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "AWAITING HUMAN CONFIRMATION" not in result.output
    proposals = log.read_proposals()
    assert proposals[0]["executed"] is True


# --------------------------------------------------------------------------------------
# FIX 4 — wallet_engine.provider must actually reach walletctl (as --provider), and the
# dead actions.allow_fleet_noncombat key must warn rather than silently mislead.
# --------------------------------------------------------------------------------------


def test_walletctl_build_passes_the_provider_flag(isolated_home, monkeypatch):
    captured = {}

    def _fake_run_walletctl(*args, timeout=None):
        captured["args"] = args
        out_path = Path(args[args.index("--out") + 1])
        out_path.write_text(
            json.dumps({"to": _LIVE_ADDR, "data": "0x165715e3" + "00" * 32, "value": "0", "chainId": 8453, "gas": None, "estimatedCostWei": None})
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(tick, "_run_walletctl", _fake_run_walletctl)
    tick._walletctl_build(_build_action(), provider="envkey")
    assert "--provider" in captured["args"]
    assert captured["args"][captured["args"].index("--provider") + 1] == "envkey"


def test_walletctl_send_passes_the_provider_flag(isolated_home, monkeypatch, tmp_path):
    captured = {}

    def _fake_run_walletctl(*args, timeout=None):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="SUBMITTED: 0x" + "aa" * 32 + "\n", stderr="")

    monkeypatch.setattr(tick, "_run_walletctl", _fake_run_walletctl)
    tick._walletctl_send(tmp_path / "tx.json", tier=Tier.ECONOMY, provider="envkey")
    assert "--provider" in captured["args"]
    assert captured["args"][captured["args"].index("--provider") + 1] == "envkey"


def test_walletctl_eth_balance_wei_passes_the_provider_flag(isolated_home, monkeypatch):
    captured = {}

    def _fake_run_walletctl(*args, timeout=None):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="balance: 1.0 ETH\n", stderr="")

    monkeypatch.setattr(tick, "_run_walletctl", _fake_run_walletctl)
    result = tick._walletctl_eth_balance_wei(provider="envkey")
    assert result == 10**18
    assert "--provider" in captured["args"]
    assert captured["args"][captured["args"].index("--provider") + 1] == "envkey"


# --------------------------------------------------------------------------------------
# `npx skills add` copies veydrift-wallet's source + package.json/package-lock.json but
# never runs `npm install` at the destination. `_run_walletctl` self-heals this once (from
# the pinned lockfile, never a floating resolution) rather than letting a raw
# ERR_MODULE_NOT_FOUND surface as an opaque walletctl_build ESCALATE detail.
# --------------------------------------------------------------------------------------


def test_ensure_wallet_deps_installed_skips_npm_when_node_modules_present(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("npm install should not run when node_modules already exists")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    assert tick._ensure_wallet_deps_installed(tmp_path) is None


def test_ensure_wallet_deps_installed_runs_npm_install_when_missing(tmp_path, monkeypatch):
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout="added 42 packages", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert tick._ensure_wallet_deps_installed(tmp_path) is None
    assert captured["argv"] == ["npm", "install", "--no-audit", "--no-fund"]
    assert captured["cwd"] == tmp_path


def test_ensure_wallet_deps_installed_reports_npm_failure_without_a_raw_stack(tmp_path, monkeypatch):
    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="npm ERR! network timeout")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    error = tick._ensure_wallet_deps_installed(tmp_path)
    assert error is not None
    assert "npm ERR! network timeout" in error
    assert str(tmp_path) in error


def test_ensure_wallet_deps_installed_reports_when_npm_itself_is_missing(tmp_path, monkeypatch):
    def _fake_run(argv, **kwargs):
        raise FileNotFoundError("npm not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    error = tick._ensure_wallet_deps_installed(tmp_path)
    assert error is not None
    assert "npm install" in error
    assert str(tmp_path) in error


def test_run_walletctl_installs_deps_then_still_runs_the_real_command(tmp_path, monkeypatch):
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "npm":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="balance: 1.0 ETH\n", stderr="")

    monkeypatch.setattr(tick, "_walletctl_argv", lambda *args: (["npx", "--yes", "tsx", "cli.ts", *args], tmp_path))
    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = tick._run_walletctl("status")

    assert len(calls) == 2
    assert calls[0] == ["npm", "install", "--no-audit", "--no-fund"]
    assert calls[1][0] == "npx"
    assert result.returncode == 0


def test_run_walletctl_never_shells_out_when_install_fails(tmp_path, monkeypatch):
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="npm ERR! network timeout")

    monkeypatch.setattr(tick, "_walletctl_argv", lambda *args: (["npx", "--yes", "tsx", "cli.ts", *args], tmp_path))
    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = tick._run_walletctl("status")

    assert len(calls) == 1  # only the npm install attempt -- never the real walletctl call
    assert result.returncode != 0
    assert "npm ERR! network timeout" in result.stderr


def test_run_walletctl_skips_the_check_when_no_wallet_dir_resolved(monkeypatch):
    # cwd=None means walletctl is on PATH (e.g. npm link) -- nothing to npm-install into.
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("should not attempt npm install when cwd is None")

    monkeypatch.setattr(tick, "_walletctl_argv", lambda *args: (["walletctl", *args], None))
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))
    result = tick._run_walletctl("status")
    assert result.returncode == 0


def test_load_policy_warns_when_allow_fleet_noncombat_is_dead_config(isolated_home, capsys):
    from veydrift_agent.state import policy_path

    policy = json.loads((Path(__file__).parent.parent / "assets" / "policy.example.json").read_text())
    policy["actions"]["allow_fleet_noncombat"] = True
    init_policy()
    policy_path().write_text(json.dumps(policy))

    tick._load_policy(policy_path())
    captured = capsys.readouterr()
    assert "allow_fleet_noncombat" in captured.out
    assert "no effect" in captured.out


def test_load_policy_is_silent_when_allow_fleet_noncombat_is_false(isolated_home, capsys):
    from veydrift_agent.state import policy_path

    _write_policy()  # default policy.example.json ships allow_fleet_noncombat: false
    tick._load_policy(policy_path())
    captured = capsys.readouterr()
    assert "allow_fleet_noncombat" not in captured.out


# --------------------------------------------------------------------------------------
# FIX 5 — a structural tier block must not drown strategy.md, but a substantive gate
# firing alongside it (or on its own) must still be narrated there.
# --------------------------------------------------------------------------------------


def test_structural_tier_block_alone_does_not_append_to_strategy_md(isolated_home, monkeypatch):
    """At tier 1 with no wallet configured, a routine onchain proposal's only non-passing
    gates are `tier` (BLOCK) + `gas`/`eth_floor` (ESCALATE, missing data) -- exactly the
    `13/16 pass (block)` cluster AGENTS.md's worked example shows. That is expected noise,
    not new information, so it must not accrue a strategy.md entry every single tick."""
    _write_policy()  # tier advisor
    tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=None)
    _patch_common(monkeypatch, live_addresses={_LIVE_ADDR}, unsigned_tx=tx)  # gas=None -> gas gate ESCALATEs too

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output

    proposals = log.read_proposals()
    non_passing = {(v["gate"], v["status"]) for v in proposals[0]["guard_verdicts"] if v["status"] != "pass"}
    assert ("tier", "block") in non_passing  # sanity: this really is the tier-block case

    assert not log.strategy_path().exists()


def test_substantive_block_alongside_tier_still_appends_to_strategy_md(isolated_home, monkeypatch):
    """A REAL problem (address mismatch, here) firing alongside the structural tier
    block must still be narrated in strategy.md -- Fix 5 only suppresses the purely
    structural case, never a genuinely informative one."""
    _write_policy()  # tier advisor
    tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=None)
    # live_addresses deliberately does NOT contain the tx's `to` -- trips the `address`
    # gate's BLOCK on top of the structural tier block.
    _patch_common(monkeypatch, live_addresses={"0x000000000000000000000000000000000000dead"}, unsigned_tx=tx)

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert log.strategy_path().exists()
    assert "tick 1:" in log.strategy_path().read_text()


def test_is_structural_tier_block_true_for_the_tier1_expected_cluster():
    assert guard_mod.is_structural_tier_block([("tier", "block")]) is True
    assert guard_mod.is_structural_tier_block([("tier", "block"), ("gas", "escalate")]) is True
    assert guard_mod.is_structural_tier_block([("tier", "block"), ("gas", "escalate"), ("eth_floor", "escalate")]) is True


def test_is_structural_tier_block_false_when_anything_else_is_wrong():
    assert guard_mod.is_structural_tier_block([]) is False
    assert guard_mod.is_structural_tier_block([("gas", "escalate")]) is False  # no tier block at all
    assert guard_mod.is_structural_tier_block([("tier", "block"), ("affordability", "block")]) is False
    assert guard_mod.is_structural_tier_block([("tier", "block"), ("gas", "block")]) is False  # real ceiling breach
    assert guard_mod.is_structural_tier_block([("tier", "block"), ("energy", "warn")]) is False


# --------------------------------------------------------------------------------------
# Dedup: a repeated `vd tick` invocation producing a content-identical proposal to the
# immediately-previous one must not be double-counted or double-logged. Confirmed real
# bug: a live proposals.jsonl showed ticks #9/#10/#11 and #16/#17 as byte-identical
# records (excl. ts/tick) logged seconds apart, caused by re-running `vd tick` purely to
# re-inspect output.
# --------------------------------------------------------------------------------------


def test_repeated_identical_tick_is_not_logged_twice(isolated_home, monkeypatch):
    _write_policy()  # tier advisor
    _patch_common(monkeypatch)  # same snapshot/action on both calls (defaults)

    r1 = runner.invoke(tick.app, ["--dry-run"])
    r2 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output

    assert load_agent_state().tick_count == 1
    assert len(log.read_proposals()) == 1
    assert "duplicate" in r2.output.lower()


def test_genuinely_different_second_tick_is_logged_normally(isolated_home, monkeypatch):
    """Proves the check doesn't over-suppress: a second call whose action differs must be
    logged as its own tick, same as today."""
    _write_policy()  # tier advisor
    _patch_common(monkeypatch)  # first call: default _build_action()

    r1 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output

    changed_action = _build_action().model_copy(update={"target_level": 2})
    _patch_common(monkeypatch, action=changed_action)

    r2 = runner.invoke(tick.app, ["--dry-run"])
    assert r2.exit_code == 0, r2.output

    assert load_agent_state().tick_count == 2
    assert len(log.read_proposals()) == 2


def test_duplicate_substantive_block_does_not_append_a_second_strategy_md_entry(isolated_home, monkeypatch):
    """Extends test_substantive_block_alongside_tier_still_appends_to_strategy_md: a
    genuine substantive gate firing (address mismatch) is narrated once; an immediately
    repeated, content-identical call must not add a second entry."""
    _write_policy()  # tier advisor
    tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=None)
    _patch_common(monkeypatch, live_addresses={"0x000000000000000000000000000000000000dead"}, unsigned_tx=tx)

    r1 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output
    first_strategy = log.strategy_path().read_text()
    assert "tick 1:" in first_strategy

    r2 = runner.invoke(tick.app, ["--dry-run"])
    assert r2.exit_code == 0, r2.output
    second_strategy = log.strategy_path().read_text()
    assert second_strategy == first_strategy  # unchanged -- no new entry appended
    assert load_agent_state().tick_count == 1


# --------------------------------------------------------------------------------------
# Human-activity reconciliation: a best-effort /wallet/{addr}/activity check for the
# previous tick's unresolved (tier 1, or require_confirmation-stopped) on-chain proposal.
# Never affects Decision; deliberately does not classify match/diverge -- see
# tick.py's _maybe_check_human_activity docstring.
# --------------------------------------------------------------------------------------


def test_no_activity_check_on_the_very_first_tick(isolated_home, monkeypatch):
    _write_policy()  # tier advisor
    _patch_common(monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("must not check /activity when nothing is unresolved yet")

    monkeypatch.setattr(tick.read, "fetch_activity", _boom)

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output


def test_activity_checked_on_the_tick_after_a_tier1_onchain_proposal(isolated_home, monkeypatch):
    _write_policy()  # tier advisor
    _patch_common(monkeypatch)

    r1 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output
    previous = load_agent_state().last_unresolved_onchain_proposal
    assert previous is not None
    assert previous.function == "startBuildingUpgrade"

    calls = []

    def _fake_fetch_activity(wallet, *, since=None, max_age=None):
        calls.append({"wallet": wallet, "since": since})
        return {
            "items": [
                {
                    "kind": "planet-started",
                    "title": "Home planet settled",
                    "detail": "Planet #664",
                    "transactionHash": "0x" + "ab" * 32,
                    "occurredAt": "1786121739",
                    "metadata": {"planetId": "664"},
                }
            ]
        }

    monkeypatch.setattr(tick.read, "fetch_activity", _fake_fetch_activity)
    changed_action = _build_action().model_copy(update={"target_level": 2})
    _patch_common(monkeypatch, action=changed_action)  # must differ, or tick 2 dedups against tick 1

    r2 = runner.invoke(tick.app, ["--dry-run"])
    assert r2.exit_code == 0, r2.output

    assert len(calls) == 1
    assert calls[0]["wallet"] == WALLET
    assert calls[0]["since"] == str(int(previous.ts.timestamp()))

    proposals = log.read_proposals()
    assert len(proposals) == 2
    check = proposals[1]["human_activity_check"]
    assert check["checked"] is True
    assert check["items_found"] == 1
    assert check["items"][0]["kind"] == "planet-started"
    assert "activity: 1 activity item(s)" in r2.output


@respx.mock
def test_activity_check_skipped_on_killswitch_tick(isolated_home, monkeypatch):
    from veydrift_agent.state import killswitch_path

    _write_policy()  # tier advisor
    _patch_common(monkeypatch)

    r1 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output
    assert load_agent_state().last_unresolved_onchain_proposal is not None

    def _boom(*a, **kw):
        raise AssertionError("killswitch tick must not check /activity")

    monkeypatch.setattr(tick.read, "fetch_activity", _boom)
    monkeypatch.setattr(tick, "_fetch_snapshot", _boom)

    killswitch_path().touch()
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"ok": True, "readiness": {"ready": True}}))

    result = runner.invoke(tick.app, ["--dry-run"])
    assert result.exit_code == 0, result.output


def test_activity_check_degrades_gracefully_on_fetch_failure(isolated_home, monkeypatch):
    _write_policy()  # tier advisor
    _patch_common(monkeypatch)

    r1 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output

    def _fail(*a, **kw):
        raise http.VeydriftNetworkError("boom")

    monkeypatch.setattr(tick.read, "fetch_activity", _fail)
    changed_action = _build_action().model_copy(update={"target_level": 3})
    _patch_common(monkeypatch, action=changed_action)

    r2 = runner.invoke(tick.app, ["--dry-run"])
    assert r2.exit_code == 0, r2.output  # never crashes on a fetch failure

    proposals = log.read_proposals()
    check = proposals[1]["human_activity_check"]
    assert check["checked"] is True
    assert check["items_found"] is None
    assert "boom" in check["fetch_error"]
    assert proposals[1]["guard_decision"] == proposals[0]["guard_decision"]  # guard untouched by the failure


def test_last_unresolved_onchain_proposal_set_when_require_confirmation_stops_send(isolated_home, monkeypatch, tmp_path):
    _write_policy(tier="economy")  # wallet_engine.require_confirmation defaults to true
    tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    _patch_common(monkeypatch, live_addresses={_LIVE_ADDR}, unsigned_tx=tx, gas=1_000_000_000, built_tx_path=tmp_path / "tx.json")
    _allow_guard(monkeypatch)

    result = runner.invoke(tick.app, [])
    assert result.exit_code == 0, result.output

    previous = load_agent_state().last_unresolved_onchain_proposal
    assert previous is not None
    assert previous.function == "startBuildingUpgrade"


def test_last_unresolved_onchain_proposal_cleared_when_tool_executes_the_action_itself(isolated_home, monkeypatch, tmp_path):
    _write_policy(tier="economy", wallet_engine={"provider": "keystore", "require_confirmation": False})
    tx = UnsignedTx(to=_LIVE_ADDR, data="0x165715e3" + "00" * 32, gas=156_540)
    _patch_common(monkeypatch, live_addresses={_LIVE_ADDR}, unsigned_tx=tx, gas=1_000_000_000, built_tx_path=tmp_path / "tx.json")
    _allow_guard(monkeypatch)
    monkeypatch.setattr(tick, "_send_and_await", lambda *a, **kw: (True, "success", "0x" + "cc" * 32))

    result = runner.invoke(tick.app, [])
    assert result.exit_code == 0, result.output
    assert load_agent_state().last_unresolved_onchain_proposal is None


def test_duplicate_tick_leaves_last_unresolved_onchain_proposal_untouched(isolated_home, monkeypatch):
    _write_policy()  # tier advisor
    monkeypatch.setattr(tick.read, "fetch_activity", lambda *a, **kw: {"items": []})
    _patch_common(monkeypatch)

    r1 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output
    first = load_agent_state().last_unresolved_onchain_proposal
    assert first is not None

    r2 = runner.invoke(tick.app, ["--dry-run"])  # identical action -> deduped against tick 1
    assert r2.exit_code == 0, r2.output
    second = load_agent_state().last_unresolved_onchain_proposal
    assert second is not None
    assert second.ts == first.ts  # untouched, not re-set to tick 2's own `now`


def test_activity_items_filtered_by_metadata_planet_id_mismatch_excluded_but_missing_metadata_kept(isolated_home, monkeypatch):
    previous = UnresolvedProposal(
        ts=datetime(2026, 8, 12, 12, 0, tzinfo=UTC), planet_id=664, function="startBuildingUpgrade", entity_id=3
    )

    def _fake_fetch_activity(wallet, *, since=None, max_age=None):
        return {
            "items": [
                {"kind": "a", "title": "same planet", "metadata": {"planetId": "664"}},
                {"kind": "b", "title": "different planet", "metadata": {"planetId": "999"}},
                {"kind": "c", "title": "no metadata"},
            ]
        }

    monkeypatch.setattr(tick.read, "fetch_activity", _fake_fetch_activity)

    record, _line = tick._maybe_check_human_activity(_economy_policy(), previous, now=datetime(2026, 8, 12, 13, 0, tzinfo=UTC))

    titles = {i["title"] for i in record["items"]}
    assert titles == {"same planet", "no metadata"}


def test_readiness_reports_human_activity_checked_and_hits_counts(isolated_home, monkeypatch):
    _write_policy()  # tier advisor
    _patch_common(monkeypatch)

    r1 = runner.invoke(tick.app, ["--dry-run"])
    assert r1.exit_code == 0, r1.output

    monkeypatch.setattr(
        tick.read, "fetch_activity", lambda *a, **kw: {"items": [{"kind": "planet-started", "title": "x", "metadata": {"planetId": "664"}}]}
    )
    changed_action = _build_action().model_copy(update={"target_level": 4})
    _patch_common(monkeypatch, action=changed_action)

    r2 = runner.invoke(tick.app, ["--dry-run"])
    assert r2.exit_code == 0, r2.output

    result = runner.invoke(tick.app, ["--readiness"])
    assert result.exit_code == 0, result.output
    assert "human_activity_checked: 1 tick(s) checked" in result.output
    assert "; 1 found" in result.output

