"""Config validation for `policy.engine.jev` (`JevWeights`, `JevCfg`): non-finite numbers,
out-of-range values and a weights vector with no confidence-bearing term are all rejected at
load time, and the shipped example policy stays valid."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from veydrift_agent.models import JevCfg, JevWeights, Policy

EXAMPLE = Path(__file__).parent.parent / "assets" / "policy.example.json"


def _policy_with_jev(jev: str) -> Policy:
    """A policy validated from a JSON *text* -- Python's parser accepts `Infinity`/`NaN`
    literals, exactly like `policy.json` loading does."""
    base = json.loads(EXAMPLE.read_text())
    base["engine"] = {"kind": "jev", "jev": "@@"}
    return Policy.model_validate(json.loads(json.dumps(base).replace('"@@"', jev)))


def test_defaults_are_valid():
    w = JevWeights()
    assert (w.fit, w.urgency, w.focus, w.economy, w.threat) == (0.30, 0.25, 0.15, 0.25, 0.05)
    cfg = JevCfg()
    assert cfg.payback_reference_hours == 24.0


def test_the_example_policy_is_valid():
    Policy.model_validate(json.loads(EXAMPLE.read_text()))


@pytest.mark.parametrize("field", ["fit", "urgency", "focus", "economy", "threat"])
@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
def test_non_finite_weights_are_rejected(field, literal):
    with pytest.raises(ValidationError):
        _policy_with_jev(f'{{"weights": {{"{field}": {literal}}}}}')


@pytest.mark.parametrize(
    "field", ["min_confidence", "min_margin", "timeout_s", "payback_reference_hours"]
)
@pytest.mark.parametrize("literal", ["Infinity", "NaN"])
def test_non_finite_cfg_numbers_are_rejected(field, literal):
    with pytest.raises(ValidationError):
        _policy_with_jev(f'{{"{field}": {literal}}}')


@pytest.mark.parametrize("field", ["fit", "urgency", "focus", "economy", "threat"])
def test_weights_are_bounded_to_0_100(field):
    JevWeights(**{field: 100})
    with pytest.raises(ValidationError):
        JevWeights(**{field: 100.01})
    with pytest.raises(ValidationError):
        JevWeights(**{field: -0.1})


def test_payback_reference_hours_is_bounded():
    JevCfg(payback_reference_hours=10000)
    with pytest.raises(ValidationError):
        JevCfg(payback_reference_hours=10001)
    with pytest.raises(ValidationError):
        JevCfg(payback_reference_hours=0)


@pytest.mark.parametrize(
    "weights",
    [
        {"fit": 0, "urgency": 0, "focus": 0, "economy": 1, "threat": 0},
        {"fit": 0, "urgency": 0, "focus": 0, "economy": 0, "threat": 1},
        {"fit": 0, "urgency": 0, "focus": 0, "economy": 0, "threat": 0},
    ],
)
def test_weights_without_a_judgment_term_are_rejected(weights):
    with pytest.raises(ValidationError, match="fit, urgency or focus"):
        JevWeights(**weights)


@pytest.mark.parametrize("field", ["fit", "urgency", "focus"])
def test_any_single_judgment_weight_is_enough(field):
    w = JevWeights(**{"fit": 0, "urgency": 0, "focus": 0, "economy": 1, "threat": 0, field: 1})
    assert getattr(w, field) == 1


def test_unknown_jev_keys_are_still_rejected():
    with pytest.raises(ValidationError):
        JevCfg(base_url="https://evil.example.invalid")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        JevWeights(bogus=1)  # type: ignore[call-arg]
