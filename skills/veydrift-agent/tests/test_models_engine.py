"""Config validation for `policy.engine.jev` (`JevWeights`, `JevCfg`): non-finite numbers,
out-of-range values and a weights vector with no confidence-bearing term are all rejected at
load time, and the shipped example policy stays valid."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from veydrift_agent.models import (
    ADAPTIVE_INTENT_MAX_HOURS,
    JevCfg,
    JevWeights,
    Policy,
    intent_text_problems,
)

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


# --------------------------------------------------------------------------------------
# Adaptive intent: the privacy validator and the default-hours bound
# --------------------------------------------------------------------------------------

WALLET = "0x224aba5d489675a7bd3ce07786fada466b46fa0f"
# A well-known throwaway address, used only to exercise the signer check.
SIGNER = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"


def _problems(text: str, **kwargs):
    return intent_text_problems(text, **kwargs)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Focus on research, starting with Laser Technology.",
        "Get the Metal Mine to level 12 before anything else.",
        "Keep 3 raids worth of defenses and 20:30 as the cutoff.",  # a two-part ratio is not a coordinate
        "Version 1:2 of the plan.",
    ],
)
def test_ordinary_intent_text_has_no_problems(text):
    assert _problems(text, wallet=WALLET, signer=SIGNER, planet_ids=[664, 1234]) == []


def test_an_address_is_flagged():
    assert _problems("Send to 0x" + "ab" * 20) == ["contains an address"]
    assert _problems("Send to 0x" + "AB" * 32) == ["contains an address"]  # a 32-byte value too
    assert _problems("Say 0x1234 and 0x" + "a" * 39) == []  # too short for an address


def test_coordinates_are_flagged():
    assert _problems("Colonize 7:291:1 soon") == ["contains coordinates"]
    assert _problems("7:291:14") == ["contains coordinates"]


def test_the_wallet_and_signer_are_flagged_case_insensitively():
    assert "contains the wallet address" in _problems(f"Keep {WALLET.upper()} funded", wallet=WALLET)
    assert "contains the signer address" in _problems(f"Keep {SIGNER.upper()} funded", signer=SIGNER)
    assert "contains the wallet address" in _problems(f"keep {WALLET[:20]}{WALLET[20:].upper()}", wallet=WALLET)


def test_a_partial_wallet_is_not_a_wallet_match():
    assert _problems(f"Keep {WALLET[:12]} funded", wallet=WALLET) == []


def test_a_wallet_that_is_not_given_is_not_checked_by_name():
    assert _problems(f"Keep {WALLET} funded", wallet=None, signer=None) == ["contains an address"]


def test_a_planet_id_is_flagged_as_a_standalone_number():
    assert _problems("Defend planet 664 first.", planet_ids=[664]) == ["contains planet id 664"]
    assert _problems("664", planet_ids=[664]) == ["contains planet id 664"]
    assert _problems("Defend (664), then 1234.", planet_ids=[664, 1234]) == [
        "contains planet id 664",
        "contains planet id 1234",
    ]


def test_a_planet_id_inside_a_larger_number_is_not_flagged():
    assert _problems("Reach 16640 points and level 1664.", planet_ids=[664]) == []
    assert _problems("Aim for 6640 or 2664.", planet_ids=[664]) == []


def test_problems_accumulate():
    problems = _problems(f"At 7:291:1 near planet 664, wallet {WALLET}", wallet=WALLET, planet_ids=[664])
    assert problems == [
        "contains an address",
        "contains coordinates",
        "contains the wallet address",
        "contains planet id 664",
    ]


def _example_with_intent(intent: str, **top) -> dict:
    raw = json.loads(EXAMPLE.read_text())
    raw["engine"]["jev"]["intent"] = intent
    raw.update(top)
    return raw


@pytest.mark.parametrize(
    ("intent", "fragment"),
    [
        ("Colonize 7:291:1 first.", "contains coordinates"),
        ("Protect planet 664.", "contains planet id 664"),
        (f"Fund {WALLET} first.", "contains an address"),
    ],
)
def test_a_policy_whose_intent_identifies_the_account_fails_to_load(intent, fragment):
    with pytest.raises(ValidationError) as excinfo:
        Policy.model_validate(_example_with_intent(intent))
    message = str(excinfo.value)
    assert "engine.jev.intent" in message
    assert "TypeSafe" in message
    assert fragment in message


def test_a_policy_whose_intent_names_the_signer_fails_to_load():
    raw = _example_with_intent(f"Keep {SIGNER} funded.", signer=SIGNER)
    with pytest.raises(ValidationError, match="contains the signer address"):
        Policy.model_validate(raw)


def test_a_policy_with_an_ordinary_intent_loads():
    policy = Policy.model_validate(_example_with_intent("Focus on research, starting with Laser Technology."))
    assert policy.engine.jev.intent == "Focus on research, starting with Laser Technology."


def test_the_example_policy_still_loads_with_adaptive_intent_off():
    policy = Policy.model_validate(json.loads(EXAMPLE.read_text()))
    assert policy.engine.jev.adaptive_intent is False
    assert policy.engine.jev.adaptive_intent_default_hours == 6.0


def test_adaptive_intent_default_hours_is_bounded():
    assert JevCfg(adaptive_intent_default_hours=72).adaptive_intent_default_hours == 72
    assert JevCfg(adaptive_intent_default_hours=0.5).adaptive_intent_default_hours == 0.5
    assert ADAPTIVE_INTENT_MAX_HOURS == 72
    for bad in (0, -1, 73, 72.01):
        with pytest.raises(ValidationError):
            JevCfg(adaptive_intent_default_hours=bad)


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
def test_adaptive_intent_default_hours_rejects_non_finite_numbers(literal):
    with pytest.raises(ValidationError):
        _policy_with_jev(f'{{"adaptive_intent_default_hours": {literal}}}')
