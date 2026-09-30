"""Tests for `veydrift_agent.engine` -- the dispatch between the ladder and the jev engine,
`vd doctor`'s engine lines, and `vd engine pool|compare` / `vd plan run --engine`.

`jev_engine` is a separate work package; every test here monkeypatches
`veydrift_agent.jev_engine.decide` / `build_request` with canned results, so nothing here
depends on how the real engine composes its answer, and nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from veydrift_agent import engine as engine_mod
from veydrift_agent import jev_engine
from veydrift_agent import plan as plan_mod
from veydrift_agent.cli import app as vd_app
from veydrift_agent.jev import QuestionSpec
from veydrift_agent.models import (
    Action,
    ActionKind,
    ActionsCfg,
    EngineCfg,
    EngineTrace,
    Limits,
    Policy,
    Snapshot,
    StorageCfg,
)

FIXTURES = Path(__file__).parent / "fixtures"
WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
SENTINEL_KEY = "ts-sentinel-key-do-not-print-0123456789"

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "veydrift-home"
    monkeypatch.setenv("VEYDRIFT_HOME", str(home))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return home


def load_snapshot(name: str) -> Snapshot:
    return Snapshot.model_validate(json.loads((FIXTURES / name).read_text()))


def make_policy(**overrides) -> Policy:
    base = {
        "wallet": WALLET,
        "planets": [],
        "limits": Limits(
            gas_per_tx_wei=3_000_000_000_000_000,
            gas_per_day_wei=20_000_000_000_000_000,
            eth_gas_floor_wei=2_000_000_000_000_000,
        ),
        "actions": ActionsCfg(allow_building=True, allow_research=True, allow_defense=False, allow_ships=False),
        "storage": StorageCfg(hours_to_cap_trigger=2.0),
    }
    base.update(overrides)
    return Policy(**base)


def jev_policy(**overrides) -> Policy:
    return make_policy(engine=EngineCfg(kind="jev"), **overrides)


def _jev_action(rule: str = "6:building-queue-empty") -> Action:
    return Action(
        kind=ActionKind.BUILD,
        function="startBuildingUpgrade",
        planet_id=664,
        entity_id=3,
        entity_name="Solar Plant",
        target_level=1,
        rule=rule,
        rationale="jev pick",
        engine="jev",
    )


def _jev_trace(**overrides) -> EngineTrace:
    base = {
        "engine": "jev",
        "configured": "jev",
        "model": "jev-1.13.0",
        "latency_ms": 140,
        "winner_confidence": 0.71,
        "agrees_with_ladder": True,
    }
    base.update(overrides)
    return EngineTrace(**base)


# --------------------------------------------------------------------------------------
# decide: ladder (the default)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["planet_664.json", "planet_hot.json"])
def test_default_policy_decide_is_exactly_the_ladder(fixture, monkeypatch):
    snapshot = load_snapshot(fixture)
    policy = make_policy(planets=[snapshot.planets[0].planet_id])

    def _never(*a, **kw):
        raise AssertionError("jev_engine must not be used under the default policy")

    monkeypatch.setattr(jev_engine, "decide", _never)

    action, trace = engine_mod.decide(snapshot, policy)

    assert action.model_dump() == plan_mod.plan_next_action(snapshot, policy).model_dump()
    assert trace.engine == "ladder"
    assert trace.configured == "ladder"
    assert trace.fallback_reason is None


def test_ladder_decide_forwards_every_planner_kwarg_through_the_module_attribute(monkeypatch):
    captured: dict = {}

    def _spy(snapshot, policy, **kwargs):
        captured.update(kwargs)
        return Action(kind=ActionKind.NOOP, rule="9:no-match", rationale="spy")

    monkeypatch.setattr(plan_mod, "plan_next_action", _spy)
    snapshot = load_snapshot("planet_664.json")

    engine_mod.decide(
        snapshot,
        make_policy(),
        killswitch_active=False,
        pending_tx_unreconciled=True,
        resolvable_mission_ids=[5],
        colonize_targets=[("1:1:1", 3)],
        last_attended_planet_id=664,
    )

    assert set(captured) == {
        "killswitch_active",
        "pending_tx_unreconciled",
        "resolvable_mission_ids",
        "own_planet_debris",
        "foreign_debris_targets",
        "colonize_targets",
        "attack_targets",
        "missile_targets",
        "last_attended_planet_id",
    }
    assert captured["pending_tx_unreconciled"] is True
    assert captured["resolvable_mission_ids"] == [5]
    assert captured["last_attended_planet_id"] == 664


# --------------------------------------------------------------------------------------
# decide: jev
# --------------------------------------------------------------------------------------


def test_engine_override_jev_routes_to_jev_engine(monkeypatch):
    calls: list[dict] = []
    canned = (_jev_action(), _jev_trace())

    def _fake(snapshot, policy, **kwargs):
        calls.append(kwargs)
        return canned

    monkeypatch.setattr(jev_engine, "decide", _fake)
    snapshot = load_snapshot("planet_664.json")
    context = engine_mod.EngineContext()
    sentinel_backend = object()

    action, trace = engine_mod.decide(
        snapshot,
        make_policy(planets=[664]),  # default policy: ladder -- the override wins
        engine_override="jev",
        pending_tx_unreconciled=True,
        context=context,
        backend=sentinel_backend,  # type: ignore[arg-type]
    )

    assert (action, trace) == canned
    assert len(calls) == 1
    assert "killswitch_active" not in calls[0]
    assert calls[0]["pending_tx_unreconciled"] is True
    assert calls[0]["context"] is context
    assert calls[0]["backend"] is sentinel_backend


def test_configured_jev_routes_to_jev_engine(monkeypatch):
    canned = (_jev_action(), _jev_trace())
    monkeypatch.setattr(jev_engine, "decide", lambda *a, **kw: canned)
    action, trace = engine_mod.decide(load_snapshot("planet_664.json"), jev_policy(planets=[664]))
    assert action.engine == "jev"
    assert trace.configured == "jev"


def test_engine_override_ladder_beats_a_jev_policy(monkeypatch):
    def _never(*a, **kw):
        raise AssertionError("jev_engine must not be used")

    monkeypatch.setattr(jev_engine, "decide", _never)
    action, trace = engine_mod.decide(
        load_snapshot("planet_664.json"), jev_policy(planets=[664]), engine_override="ladder"
    )
    assert action.engine == "ladder"
    assert trace.configured == "ladder"


def test_an_exception_inside_the_jev_engine_falls_back_to_the_ladder(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("secret detail that must not be copied into the trace")

    monkeypatch.setattr(jev_engine, "decide", _boom)
    snapshot = load_snapshot("planet_664.json")
    policy = jev_policy(planets=[664])

    action, trace = engine_mod.decide(snapshot, policy)

    assert action.model_dump() == plan_mod.plan_next_action(snapshot, policy).model_dump()
    assert trace.engine == "ladder"
    assert trace.configured == "jev"
    assert trace.fallback_reason == "engine_error:RuntimeError"
    assert "secret" not in trace.model_dump_json()


def test_killswitch_with_jev_halts_without_calling_the_jev_engine(monkeypatch):
    def _never(*a, **kw):
        raise AssertionError("the killswitch must never reach jev_engine")

    monkeypatch.setattr(jev_engine, "decide", _never)

    action, trace = engine_mod.decide(
        load_snapshot("planet_664.json"), jev_policy(planets=[664]), killswitch_active=True
    )

    assert action.kind is ActionKind.HALT
    assert action.rule == "0:killswitch"
    assert trace.engine == "ladder"
    assert trace.configured == "jev"
    assert trace.pre_empted_by == "0:killswitch"
    assert trace.fallback_reason is None


def test_a_ladder_failure_is_not_swallowed(monkeypatch):
    """Only the engine is guarded: if the ladder itself raises, the tick learns about it,
    exactly as before this feature existed."""

    def _boom(*a, **kw):
        raise ValueError("ladder broke")

    monkeypatch.setattr(plan_mod, "plan_next_action", _boom)
    with pytest.raises(ValueError, match="ladder broke"):
        engine_mod.decide(load_snapshot("planet_664.json"), make_policy())


# --------------------------------------------------------------------------------------
# describe_trace
# --------------------------------------------------------------------------------------


def test_describe_trace_formats():
    assert engine_mod.describe_trace(_jev_trace()) == "jev (jev-1.13.0, 140ms, confidence 0.71, agrees with ladder)"
    assert (
        engine_mod.describe_trace(_jev_trace(agrees_with_ladder=False))
        == "jev (jev-1.13.0, 140ms, confidence 0.71, differs from ladder)"
    )
    assert (
        engine_mod.describe_trace(EngineTrace(engine="ladder", configured="jev", fallback_reason="timeout"))
        == "jev -> ladder fallback (timeout)"
    )
    assert (
        engine_mod.describe_trace(EngineTrace(engine="ladder", configured="jev", pre_empted_by="1b:game-paused"))
        == "jev pre-empted by 1b:game-paused"
    )


# --------------------------------------------------------------------------------------
# doctor_lines / `vd doctor`
# --------------------------------------------------------------------------------------


def test_doctor_lines_without_a_policy_file(isolated_home):
    lines = engine_mod.doctor_lines()
    assert lines[0] == "engine: ladder (no policy.json)"
    assert lines[1] == "TYPESAFE_API_KEY: unset"
    assert lines[2] in ("typesafe-sdk: importable", "typesafe-sdk: missing")
    assert not (isolated_home / "policy.json").exists()  # read-only


def test_doctor_lines_reads_the_configured_kind_and_never_prints_the_key(isolated_home, monkeypatch):
    isolated_home.mkdir(parents=True)
    (isolated_home / "policy.json").write_text(jev_policy().model_dump_json())
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL_KEY)

    lines = engine_mod.doctor_lines()

    assert lines[0] == "engine: jev"
    assert lines[1] == "TYPESAFE_API_KEY: set"
    assert SENTINEL_KEY not in "\n".join(lines)


def test_doctor_lines_with_an_invalid_policy_file(isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "policy.json").write_text("{not json")
    assert engine_mod.doctor_lines()[0] == "engine: unknown (policy.json invalid)"


def test_doctor_does_not_import_the_sdk(monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "typesafe_sdk", raising=False)
    engine_mod.doctor_lines()
    assert "typesafe_sdk" not in sys.modules


def test_vd_doctor_prints_the_engine_lines(isolated_home, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL_KEY)
    result = runner.invoke(vd_app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "engine: ladder (no policy.json)" in result.output
    assert "TYPESAFE_API_KEY: set" in result.output
    assert "typesafe-sdk:" in result.output
    assert SENTINEL_KEY not in result.output


# --------------------------------------------------------------------------------------
# `vd engine pool`
# --------------------------------------------------------------------------------------


def _write_inputs(tmp_path: Path, policy: Policy, fixture: str = "planet_664.json") -> tuple[str, str]:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(policy.model_dump_json())
    return str(FIXTURES / fixture), str(policy_file)


def test_engine_pool_lists_candidates_and_the_rendered_request(tmp_path, monkeypatch):
    snap, pol = _write_inputs(tmp_path, jev_policy(planets=[664]))
    seen: dict = {}

    def _fake_build(snapshot, policy, pool, context=None):
        seen["pool_size"] = len(pool)
        return {"situation": {"economy": "active"}}, {"tick_focus": QuestionSpec("choice", "which group?", {"a": "b"})}

    monkeypatch.setattr(jev_engine, "build_request", _fake_build)

    result = runner.invoke(vd_app, ["engine", "pool", "--snapshot", snap, "--policy", pol])

    assert result.exit_code == 0, result.output
    assert seen["pool_size"] >= 1
    assert "pool:" in result.output
    assert "c0" in result.output
    assert "rejected:" in result.output
    assert "estimated request size" in result.output
    assert '"tick_focus"' in result.output


def test_engine_pool_json(tmp_path, monkeypatch):
    snap, pol = _write_inputs(tmp_path, jev_policy(planets=[664]))
    monkeypatch.setattr(
        jev_engine,
        "build_request",
        lambda *a, **kw: ({"s": 1}, {"q": QuestionSpec("noul", "threat?")}),
    )

    result = runner.invoke(vd_app, ["engine", "pool", "--snapshot", snap, "--policy", pol, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pool"] and payload["pool"][0]["id"] == "c0"
    assert set(payload["pool"][0]) >= {"band", "group", "family", "planet_id", "entity", "score_basis"}
    assert isinstance(payload["rejected"], dict)
    assert payload["request"]["state"] == {"s": 1}
    assert payload["request"]["estimated_tokens"] > 0
    assert "note" not in payload


def test_engine_pool_needs_no_key_and_no_network(tmp_path, monkeypatch):
    snap, pol = _write_inputs(tmp_path, jev_policy(planets=[664]))
    monkeypatch.setattr(jev_engine, "build_request", lambda *a, **kw: ({}, {}))

    def _no_backend(*a, **kw):
        raise AssertionError("pool must never build a backend")

    monkeypatch.setattr("veydrift_agent.jev.default_backend", _no_backend)
    result = runner.invoke(vd_app, ["engine", "pool", "--snapshot", snap, "--policy", pol])
    assert result.exit_code == 0, result.output


def test_engine_pool_load_error_exits_4(tmp_path):
    result = runner.invoke(
        vd_app, ["engine", "pool", "--snapshot", str(tmp_path / "missing.json"), "--policy", str(tmp_path / "p.json")]
    )
    assert result.exit_code == 4


# --------------------------------------------------------------------------------------
# `vd engine compare`
# --------------------------------------------------------------------------------------


def _compare(tmp_path, *extra):
    snap, pol = _write_inputs(tmp_path, make_policy(planets=[664]))
    return runner.invoke(vd_app, ["engine", "compare", "--snapshot", snap, "--policy", pol, *extra])


def test_engine_compare_agree_exits_0(tmp_path, monkeypatch):
    ladder = plan_mod.plan_next_action(load_snapshot("planet_664.json"), make_policy(planets=[664]))
    monkeypatch.setattr(
        jev_engine, "decide", lambda *a, **kw: (ladder.model_copy(update={"engine": "jev"}), _jev_trace())
    )

    result = _compare(tmp_path)

    assert result.exit_code == 0, result.output
    assert "result: agree" in result.output
    assert "confidence: 0.71" in result.output


def test_engine_compare_disagree_exits_1(tmp_path, monkeypatch):
    other = _jev_action().model_copy(update={"entity_id": 1, "entity_name": "Metal Mine"})
    monkeypatch.setattr(jev_engine, "decide", lambda *a, **kw: (other, _jev_trace(agrees_with_ladder=False)))

    result = _compare(tmp_path)

    assert result.exit_code == 1, result.output
    assert "DISAGREE" in result.output


def test_engine_compare_fallback_exits_3_even_though_the_picks_agree(tmp_path, monkeypatch):
    ladder = plan_mod.plan_next_action(load_snapshot("planet_664.json"), make_policy(planets=[664]))
    monkeypatch.setattr(
        jev_engine,
        "decide",
        lambda *a, **kw: (ladder, EngineTrace(engine="ladder", configured="jev", fallback_reason="missing_key")),
    )

    result = _compare(tmp_path)

    assert result.exit_code == 3, result.output
    assert "FALLBACK" in result.output
    assert "fallback_reason: missing_key" in result.output


def test_engine_compare_engine_error_is_a_fallback(tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(jev_engine, "decide", _boom)
    result = _compare(tmp_path)
    assert result.exit_code == 3, result.output
    assert "engine_error:RuntimeError" in result.output


def test_engine_compare_json(tmp_path, monkeypatch):
    ladder = plan_mod.plan_next_action(load_snapshot("planet_664.json"), make_policy(planets=[664]))
    monkeypatch.setattr(
        jev_engine, "decide", lambda *a, **kw: (ladder.model_copy(update={"engine": "jev"}), _jev_trace())
    )

    result = _compare(tmp_path, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agree"] is True
    assert payload["fallback"] is False
    assert payload["trace"]["model"] == "jev-1.13.0"
    assert payload["ladder"]["rule"] == payload["jev"]["rule"]


def test_engine_compare_load_error_exits_4(tmp_path):
    result = runner.invoke(
        vd_app,
        ["engine", "compare", "--snapshot", str(tmp_path / "nope.json"), "--policy", str(tmp_path / "nope2.json")],
    )
    assert result.exit_code == 4


def test_engine_help_lists_both_commands():
    result = runner.invoke(vd_app, ["engine", "--help"])
    assert result.exit_code == 0
    assert "pool" in result.output and "compare" in result.output


# --------------------------------------------------------------------------------------
# `vd plan run --engine`
# --------------------------------------------------------------------------------------


def _plan_run(tmp_path, policy: Policy, *extra):
    snap, pol = _write_inputs(tmp_path, policy)
    return runner.invoke(vd_app, ["plan", "run", "--snapshot", snap, "--policy", pol, *extra])


def test_plan_run_engine_ladder_is_identical_to_no_flag(tmp_path, monkeypatch):
    def _never(*a, **kw):
        raise AssertionError("--engine ladder must stay offline")

    monkeypatch.setattr(jev_engine, "decide", _never)
    policy = jev_policy(planets=[664])  # even a jev policy: --engine ladder means ladder

    default = _plan_run(tmp_path, policy)
    explicit = _plan_run(tmp_path, policy, "--engine", "ladder")

    assert default.exit_code == 0, default.output
    assert explicit.exit_code == 0, explicit.output
    assert explicit.output == default.output
    assert "engine:" not in explicit.output

    default_json = _plan_run(tmp_path, policy, "--json")
    explicit_json = _plan_run(tmp_path, policy, "--json", "--engine", "ladder")
    assert explicit_json.output == default_json.output


def test_plan_run_engine_jev_prints_the_trace_line(tmp_path, monkeypatch):
    monkeypatch.setattr(jev_engine, "decide", lambda *a, **kw: (_jev_action(), _jev_trace()))

    result = _plan_run(tmp_path, make_policy(planets=[664]), "--engine", "jev")

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.replace("│", " ").split())
    assert "engine: jev (jev-1.13.0, 140ms, confidence 0.71, agrees with ladder)" in flat


def test_plan_run_engine_policy_follows_the_policy(tmp_path, monkeypatch):
    calls: list[int] = []

    def _fake(*a, **kw):
        calls.append(1)
        return _jev_action(), _jev_trace()

    monkeypatch.setattr(jev_engine, "decide", _fake)

    ladder_policy = _plan_run(tmp_path, make_policy(planets=[664]), "--engine", "policy")
    assert ladder_policy.exit_code == 0
    assert calls == []
    assert "engine:" not in ladder_policy.output

    jev_result = _plan_run(tmp_path, jev_policy(planets=[664]), "--engine", "policy")
    assert jev_result.exit_code == 0
    assert calls == [1]


def test_plan_run_rejects_an_unknown_engine(tmp_path):
    result = _plan_run(tmp_path, make_policy(planets=[664]), "--engine", "gpt")
    assert result.exit_code == 2
