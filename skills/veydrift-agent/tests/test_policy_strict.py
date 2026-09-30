"""A misspelled or unknown key must be a hard error at every nesting level of `Policy`,
never silently dropped -- while API-response models stay tolerant."""

from __future__ import annotations

import json
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from veydrift_agent import models
from veydrift_agent.models import Policy, PolicyBase, Resources

EXAMPLE = Path(__file__).resolve().parent.parent / "assets" / "policy.example.json"

_NESTED = [
    "cadence",
    "limits",
    "reserves",
    "storage",
    "actions",
    "escalation",
    "wallet_engine",
    "strategy",
    "radar",
]


def _example() -> dict:
    return json.loads(EXAMPLE.read_text())


def test_example_policy_still_validates() -> None:
    Policy.model_validate(_example())


def test_unknown_top_level_key_raises() -> None:
    data = _example()
    data["bogus"] = 1
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


@pytest.mark.parametrize("section", _NESTED)
def test_unknown_key_in_nested_section_raises(section: str) -> None:
    data = _example()
    data.setdefault(section, {})
    data[section]["definitely_not_a_field"] = 1
    with pytest.raises(ValidationError, match="definitely_not_a_field"):
        Policy.model_validate(data)


def test_misspelled_strategy_key_raises() -> None:
    data = _example()
    data.setdefault("strategy", {})["allow_agent_action_overide"] = True
    with pytest.raises(ValidationError, match="allow_agent_action_overide"):
        Policy.model_validate(data)


def test_unknown_key_in_resource_weights_and_entity_target_raises() -> None:
    data = _example()
    data.setdefault("strategy", {})["resource_weights"] = {"metal": 1, "crystl": 1}
    with pytest.raises(ValidationError, match="crystl"):
        Policy.model_validate(data)
    data = _example()
    data.setdefault("strategy", {})["ship_targets"] = [{"name": "Cruiser", "cnt": 3}]
    with pytest.raises(ValidationError, match="cnt"):
        Policy.model_validate(data)


def test_plain_resources_instance_is_still_accepted() -> None:
    policy = Policy.model_validate({**_example(), "reserves": Resources(metal=5)})
    assert policy.reserves.metal == 5
    assert policy.reserves.covers(Resources(metal=5))


def test_every_model_reachable_from_policy_forbids_extra_keys() -> None:
    seen: set[type] = set()

    def walk(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        assert model.model_config.get("extra") == "forbid", model.__name__
        for field in model.model_fields.values():
            for cand in _model_types(field.annotation):
                walk(cand)

    walk(Policy)
    assert len(seen) >= 10  # Policy + its nested config models


def _model_types(annotation) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    out: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        out.extend(_model_types(arg))
    return out


def test_api_response_models_stay_tolerant() -> None:
    assert models.Resources.model_config.get("extra") == "ignore"
    assert models.Snapshot.model_config.get("extra") == "ignore"
    assert Resources.model_validate({"metal": 1, "unexpected": 2}).metal == 1
    assert not issubclass(models.Snapshot, PolicyBase)
