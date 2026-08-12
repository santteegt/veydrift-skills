"""Tests for veydrift_agent.log — the append-only sinks, secret scrubbing, and --digest.

`test_scrub_*` is the security-critical group: docs/SPEC.md §5.9 requires that a private
key never reaches disk, and that a legitimate tx hash of the same byte-length is NOT
collateral damage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from veydrift_agent import log


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "veydrift-home"
    monkeypatch.setenv("VEYDRIFT_HOME", str(home))
    return home


FAKE_PRIVATE_KEY = "0x" + "ab" * 32
FAKE_TX_HASH = "0x" + "cd" * 32


# --------------------------------------------------------------------------------------
# scrub_text
# --------------------------------------------------------------------------------------


def test_scrub_masks_unknown_hex64():
    scrubbed = log.scrub_text(f"secret: {FAKE_PRIVATE_KEY}")
    assert FAKE_PRIVATE_KEY not in scrubbed
    assert "0x" + "*" * 64 in scrubbed


def test_scrub_preserves_known_tx_hash():
    scrubbed = log.scrub_text(f"tx: {FAKE_TX_HASH}", known_tx_hashes=[FAKE_TX_HASH])
    assert FAKE_TX_HASH in scrubbed


def test_scrub_masks_one_but_not_the_other_in_the_same_string():
    text = f"tx={FAKE_TX_HASH} key={FAKE_PRIVATE_KEY}"
    scrubbed = log.scrub_text(text, known_tx_hashes=[FAKE_TX_HASH])
    assert FAKE_TX_HASH in scrubbed
    assert FAKE_PRIVATE_KEY not in scrubbed


def test_scrub_known_tx_hash_match_is_case_insensitive():
    scrubbed = log.scrub_text(f"tx: {FAKE_TX_HASH.upper()}", known_tx_hashes=[FAKE_TX_HASH])
    assert FAKE_TX_HASH.upper() in scrubbed


def test_scrub_redacts_configured_secret_env_var(monkeypatch):
    monkeypatch.setenv("VEYDRIFT_KEYSTORE_PASSWORD", "hunter2-super-secret")
    scrubbed = log.scrub_text("password used: hunter2-super-secret")
    assert "hunter2-super-secret" not in scrubbed
    assert "REDACTED" in scrubbed


def test_scrub_redacts_secret_env_var_with_and_without_0x_prefix(monkeypatch):
    raw_key = "ab" * 32
    monkeypatch.setenv("VEYDRIFT_PRIVATE_KEY", raw_key)  # no 0x prefix, unusual but possible
    scrubbed = log.scrub_text(f"key without prefix: {raw_key}")
    assert raw_key not in scrubbed


def test_configured_secret_env_vars_extendable_via_env(monkeypatch):
    monkeypatch.setenv("VEYDRIFT_SECRET_ENV_VARS", "MY_CUSTOM_SECRET")
    monkeypatch.setenv("MY_CUSTOM_SECRET", "topsecretvalue")
    scrubbed = log.scrub_text("value: topsecretvalue")
    assert "topsecretvalue" not in scrubbed


# --------------------------------------------------------------------------------------
# proposals.jsonl / actions.jsonl — sinks, append-only, secrets never written
# --------------------------------------------------------------------------------------


def test_log_proposal_appends_a_json_line(isolated_home):
    log.log_proposal({"ts": "2026-08-12T00:00:00Z", "tick": 1, "kind": "noop"})
    log.log_proposal({"ts": "2026-08-12T00:01:00Z", "tick": 2, "kind": "build"})
    lines = log.proposals_path().read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tick"] == 1
    assert json.loads(lines[1])["tick"] == 2


def test_log_action_only_writes_to_actions_jsonl(isolated_home):
    log.log_action({"ts": "2026-08-12T00:00:00Z", "tx_hash": FAKE_TX_HASH})
    assert log.actions_path().exists()
    assert not log.proposals_path().exists()


def test_a_private_key_embedded_in_a_proposal_record_never_reaches_disk(isolated_home, monkeypatch):
    """The scenario docs/SPEC.md §5.9 exists to prevent: something upstream accidentally
    stuffs a raw secret into a field that ends up in a proposal record. Even then, it must
    not survive to disk."""
    log.log_proposal(
        {
            "ts": "2026-08-12T00:00:00Z",
            "tick": 1,
            "note": f"leaked by mistake: {FAKE_PRIVATE_KEY}",
            "tx": {"data": "0x165715e3" + "00" * 32, "to": "0xabc"},
        }
    )
    raw = log.proposals_path().read_text()
    assert FAKE_PRIVATE_KEY not in raw


def test_tx_calldata_is_not_corrupted_by_the_hex64_mask(isolated_home):
    """tx.data legitimately contains long hex runs (e.g. a uint256 arg) that must survive
    scrubbing byte-for-byte -- it's public, about-to-be-broadcast calldata, not a secret."""
    calldata = "0x165715e3" + "00" * 32 + "0000000000000000000000000000000000000000000000000000000000000298"
    log.log_proposal({"ts": "x", "tick": 1, "tx": {"data": calldata, "to": "0xabc", "value": 0, "chain_id": 8453}})
    record = json.loads(log.proposals_path().read_text().splitlines()[0])
    assert record["tx"]["data"] == calldata


def test_log_action_record_with_known_tx_hash_preserves_it(isolated_home):
    log.log_action({"ts": "2026-08-12T00:00:00Z", "tx_hash": FAKE_TX_HASH, "gas_wei": 12345})
    record = json.loads(log.actions_path().read_text().splitlines()[0])
    assert record["tx_hash"] == FAKE_TX_HASH


# --------------------------------------------------------------------------------------
# strategy.md
# --------------------------------------------------------------------------------------


def test_append_strategy_is_append_only(isolated_home):
    log.append_strategy("first entry", now=datetime(2026, 8, 12, tzinfo=UTC))
    log.append_strategy("second entry", now=datetime(2026, 8, 12, 1, tzinfo=UTC))
    text = log.strategy_path().read_text()
    assert "first entry" in text
    assert "second entry" in text
    assert text.index("first entry") < text.index("second entry")


def test_append_strategy_scrubs_secrets(isolated_home):
    log.append_strategy(f"leaked: {FAKE_PRIVATE_KEY}")
    assert FAKE_PRIVATE_KEY not in log.strategy_path().read_text()


# --------------------------------------------------------------------------------------
# format_tick_block / write_tick_markdown
# --------------------------------------------------------------------------------------


def test_write_tick_markdown_creates_one_file_per_tick(isolated_home):
    block = log.format_tick_block(
        tick_number=1,
        taken_at=datetime(2026, 8, 12, 19, 42, 3, tzinfo=UTC),
        tier="advisor",
        planet_line="planet 664 (7:181:14)",
        state_line="M 1,842  C 1,201  D 318",
        queues_line="build idle",
        incoming_line="none",
        proposal_lines=["PROPOSE startBuildingUpgrade(...)"],
        next_hint="Metal Mine 3->4",
    )
    path = log.write_tick_markdown(block, taken_at=datetime(2026, 8, 12, 19, 42, 3, tzinfo=UTC))
    assert path.exists()
    assert path.parent == log.ticks_dir()
    text = path.read_text()
    assert "TICK #1" in text
    assert "next:     Metal Mine 3->4" in text


def test_write_tick_markdown_scrubs_secrets(isolated_home, monkeypatch):
    block = log.format_tick_block(
        tick_number=1,
        taken_at=datetime(2026, 8, 12, tzinfo=UTC),
        tier="advisor",
        planet_line="planet 664",
        state_line=f"leaked {FAKE_PRIVATE_KEY}",
        queues_line="idle",
        incoming_line="none",
        proposal_lines=[],
    )
    path = log.write_tick_markdown(block, taken_at=datetime(2026, 8, 12, tzinfo=UTC))
    assert FAKE_PRIVATE_KEY not in path.read_text()


# --------------------------------------------------------------------------------------
# --digest
# --------------------------------------------------------------------------------------


def test_parse_window_accepts_hours_and_days():
    from datetime import timedelta

    assert log.parse_window("24h") == timedelta(hours=24)
    assert log.parse_window("7d") == timedelta(days=7)


def test_parse_window_rejects_garbage():
    with pytest.raises(ValueError):
        log.parse_window("nonsense")


def test_digest_counts_executed_and_refused(isolated_home):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    log.log_proposal(
        {
            "ts": now.isoformat(),
            "tick": 1,
            "function": "startBuildingUpgrade",
            "guard_decision": "allow",
            "guard_verdicts": [],
            "executed": True,
        }
    )
    log.log_proposal(
        {
            "ts": now.isoformat(),
            "tick": 2,
            "function": "startResearch",
            "guard_decision": "block",
            "guard_verdicts": [{"gate": "tier", "status": "block", "detail": "not allowed"}],
            "executed": False,
        }
    )
    log.log_action({"ts": now.isoformat(), "gas_wei": 1000})

    digest = log.build_digest("24h", now=now)
    assert "executed: 1" in digest
    assert "refused (blocked/escalated): 1" in digest
    assert "startBuildingUpgrade: 1" in digest
    assert "tier: not allowed" in digest
    assert "1000 wei" in digest


def test_digest_excludes_entries_outside_the_window(isolated_home):
    from datetime import timedelta

    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=2)
    log.log_proposal({"ts": old.isoformat(), "tick": 1, "guard_decision": "allow", "guard_verdicts": [], "executed": True})
    digest = log.build_digest("24h", now=now)
    assert "proposals: 0" in digest


def test_digest_handles_empty_logs_gracefully(isolated_home):
    digest = log.build_digest("24h")
    assert "proposals: 0" in digest
    assert "(none)" in digest
