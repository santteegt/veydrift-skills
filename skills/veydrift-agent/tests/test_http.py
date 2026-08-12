"""Tests for `veydrift_agent.http`: retries, cache TTLs, and never-cache-non-200.

No live network calls: `respx.mock` replaces httpx's transport, and any request that
doesn't match a registered route raises instead of going out to the network.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from veydrift_agent import http

BASE = http.API_BASE_URL


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own $VEYDRIFT_HOME so the disk cache never leaks across tests
    (or touches the real ~/.veydrift)."""
    monkeypatch.setenv("VEYDRIFT_HOME", str(tmp_path))
    yield tmp_path


@respx.mock
def test_fetch_success_returns_json_and_writes_cache(tmp_path):
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"ok": True}))

    body = http.fetch("/health")

    assert body == {"ok": True}
    assert route.call_count == 1
    cache_dir = tmp_path / "cache"
    assert cache_dir.is_dir()
    assert len(list(cache_dir.glob("*.json"))) == 1


@respx.mock
def test_fetch_serves_from_cache_within_max_age():
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"ok": True}))

    first = http.fetch("/health", max_age=60)
    second = http.fetch("/health", max_age=60)

    assert first == second == {"ok": True}
    assert route.call_count == 1  # second call served entirely from disk cache


@respx.mock
def test_fetch_bypasses_cache_when_max_age_zero():
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"ok": True}))

    http.fetch("/health", max_age=0)
    http.fetch("/health", max_age=0)

    assert route.call_count == 2


@respx.mock
def test_fetch_refetches_once_cache_entry_expires():
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"ok": True}))

    http.fetch("/health", max_age=0.05)
    time.sleep(0.1)
    http.fetch("/health", max_age=0.05)

    assert route.call_count == 2


@respx.mock
def test_fetch_never_caches_a_non_200(tmp_path):
    route = respx.get(f"{BASE}/wallet/0xabc/infrastructure").mock(
        return_value=httpx.Response(503, json={"error": "indexed_read_not_ready"})
    )

    with pytest.raises(http.VeydriftServerError):
        http.fetch("/wallet/0xabc/infrastructure", max_age=0)

    cache_dir = tmp_path / "cache"
    assert not cache_dir.exists() or not list(cache_dir.glob("*.json"))
    assert route.call_count == 3  # 5xx is retried up to the attempt ceiling


@respx.mock
def test_fetch_raises_http_error_on_4xx_without_retrying():
    route = respx.get(f"{BASE}/wallet/0xabc/infrastructure").mock(
        return_value=httpx.Response(400, json={"error": "bad_request"})
    )

    with pytest.raises(http.VeydriftHTTPError) as excinfo:
        http.fetch("/wallet/0xabc/infrastructure")

    assert excinfo.value.status_code == 400
    assert route.call_count == 1  # a 4xx must never be retried


@respx.mock
def test_fetch_retries_5xx_then_raises_server_error():
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(502, text="bad gateway"))

    with pytest.raises(http.VeydriftServerError) as excinfo:
        http.fetch("/health", max_age=0)

    assert excinfo.value.status_code == 502
    assert route.call_count == 3  # 3 attempts total, per SPEC.md §5.2


@respx.mock
def test_fetch_succeeds_after_a_transient_5xx():
    route = respx.get(f"{BASE}/health").mock(
        side_effect=[httpx.Response(503, text="unavailable"), httpx.Response(200, json={"ok": True})]
    )

    body = http.fetch("/health", max_age=0)

    assert body == {"ok": True}
    assert route.call_count == 2


@respx.mock
def test_fetch_retries_network_errors_then_raises_network_error():
    route = respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(http.VeydriftNetworkError):
        http.fetch("/health", max_age=0)

    assert route.call_count == 3


def test_health_route_defaults_to_15s_ttl():
    assert http._route_max_age("/health", None) == http.HEALTH_MAX_AGE_S == 15.0


def test_other_routes_default_to_60s_ttl():
    assert http._route_max_age("/wallet/0xabc/infrastructure", None) == http.DEFAULT_MAX_AGE_S == 60.0


def test_explicit_max_age_overrides_the_route_default():
    assert http._route_max_age("/health", 5.0) == 5.0
    assert http._route_max_age("/wallet/0xabc/infrastructure", 5.0) == 5.0


@respx.mock
def test_different_params_do_not_collide_in_the_cache():
    respx.get(f"{BASE}/wallet/0xabc/infrastructure", params={"planetId": "664"}).mock(
        return_value=httpx.Response(200, json={"planetId": "664"})
    )
    respx.get(f"{BASE}/wallet/0xabc/infrastructure", params={"planetId": "665"}).mock(
        return_value=httpx.Response(200, json={"planetId": "665"})
    )

    body_664 = http.fetch("/wallet/0xabc/infrastructure", {"planetId": "664"})
    body_665 = http.fetch("/wallet/0xabc/infrastructure", {"planetId": "665"})

    assert body_664 == {"planetId": "664"}
    assert body_665 == {"planetId": "665"}


def test_veydrift_home_reads_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYDRIFT_HOME", str(tmp_path / "custom"))
    assert http.veydrift_home() == tmp_path / "custom"


def test_veydrift_home_defaults_to_dot_veydrift(monkeypatch):
    monkeypatch.delenv("VEYDRIFT_HOME", raising=False)
    from pathlib import Path

    assert http.veydrift_home() == Path("~/.veydrift").expanduser()
