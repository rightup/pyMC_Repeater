"""Atomic admission tests for radio-action token buckets."""

import cherrypy
import pytest

from repeater.web.companion_endpoints import CompanionAPIEndpoints
from repeater.web.mobile_endpoints import CompanionsV1
from repeater.web.rate_limit import PrincipalTokenBucket, SSEAdmission, consume_all


def _limiter(capacity):
    return PrincipalTokenBucket(
        capacity=capacity,
        refill_per_second=0.001,
    )


def test_composite_denial_consumes_no_other_budget(monkeypatch):
    monkeypatch.setattr("repeater.web.rate_limit.time.monotonic", lambda: 100.0)
    principal = _limiter(2)
    global_limit = _limiter(1)
    assert global_limit.consume("all") is None

    retry = consume_all(
        (
            (principal, "phone", 1.0),
            (global_limit, "all", 1.0),
        )
    )

    assert retry is not None
    assert principal.consume("phone") is None
    assert principal.consume("phone") is None


def test_composite_success_consumes_every_budget(monkeypatch):
    monkeypatch.setattr("repeater.web.rate_limit.time.monotonic", lambda: 100.0)
    principal = _limiter(1)
    global_limit = _limiter(1)

    assert (
        consume_all(
            (
                (principal, "phone", 1.0),
                (global_limit, "all", 1.0),
            )
        )
        is None
    )
    assert principal.consume("phone") is not None
    assert global_limit.consume("all") is not None


def test_mobile_rf_denial_does_not_spend_principal_quota(monkeypatch):
    monkeypatch.setattr("repeater.web.rate_limit.time.monotonic", lambda: 100.0)
    endpoint = CompanionsV1()
    endpoint._rf_limiter = _limiter(2)
    endpoint._rf_global_limiter = _limiter(1)
    assert endpoint._rf_global_limiter.consume("mobile-api") is None
    cherrypy.serving.request.user = {
        "username": "operator",
        "client_id": "chat",
        "auth_type": "jwt",
    }
    cherrypy.serving.response.headers = {}

    with pytest.raises(cherrypy.HTTPError) as exc:
        endpoint._admit_rf()

    assert exc.value.status == 429
    assert endpoint._rf_limiter.consume("jwt:operator:chat") is None
    assert endpoint._rf_limiter.consume("jwt:operator:chat") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rf_burst", 0),
        ("rf_burst", "6"),
        ("rf_per_minute", 0),
        ("rf_per_minute", float("nan")),
        ("rf_per_minute", float("inf")),
        ("rf_global_burst", False),
        ("rf_global_per_minute", float("-inf")),
        ("rf_burst", 10_001),
        ("rf_per_minute", 60_001),
        ("rf_global_burst", 10_001),
        ("rf_global_per_minute", 60_001),
        ("sse_max_connections", 0),
        ("sse_max_connections", "8"),
        ("sse_max_connections", 257),
    ],
)
def test_mobile_api_rejects_unsafe_admission_config(field, value):
    with pytest.raises(ValueError, match=rf"mobile_api\.{field}"):
        CompanionsV1(config={"mobile_api": {field: value}})


def test_mobile_api_uses_explicit_stricter_global_rf_caps():
    endpoint = CompanionsV1(
        config={
            "mobile_api": {
                "rf_burst": 6,
                "rf_per_minute": 12,
                "rf_global_burst": 2,
                "rf_global_per_minute": 3,
            }
        }
    )

    assert endpoint._rf_global_limiter.capacity == 2
    assert endpoint._rf_global_limiter.refill_per_second == pytest.approx(
        3 / 60.0
    )


def test_mobile_api_rejects_huge_integer_rf_rate_cleanly():
    with pytest.raises(
        ValueError,
        match=r"mobile_api\.rf_per_minute must be a finite positive number",
    ):
        CompanionsV1(
            config={"mobile_api": {"rf_per_minute": 10**1000}}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sse_queue_maxsize", 0),
        ("sse_queue_maxsize", "64"),
        ("sse_keepalive_sec", False),
        ("sse_keepalive_sec", -1),
        ("sse_queue_maxsize", 4097),
        ("sse_keepalive_sec", 61),
    ],
)
def test_mobile_sse_rejects_opaque_integer_config(field, value):
    with pytest.raises(ValueError, match=rf"http\.{field}"):
        CompanionsV1(config={"http": {field: value}})

    with pytest.raises(ValueError, match=rf"http\.{field}"):
        CompanionAPIEndpoints(config={"http": {field: value}})


def test_sse_config_applies_documented_minimums_at_construction():
    mobile = CompanionsV1(
        config={"http": {"sse_queue_maxsize": 1, "sse_keepalive_sec": 1}}
    )
    legacy = CompanionAPIEndpoints(
        config={"http": {"sse_queue_maxsize": 1, "sse_keepalive_sec": 1}}
    )

    assert mobile._sse_settings() == (32, 5)
    assert legacy._sse_queue_maxsize == 32
    assert legacy._sse_keepalive_sec == 5


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("capacity", 0),
        ("capacity", False),
        ("refill_per_second", 0),
        ("refill_per_second", float("nan")),
        ("refill_per_second", float("inf")),
        ("idle_ttl_seconds", float("nan")),
        ("max_principals", 0),
    ],
)
def test_token_bucket_rejects_invalid_constructor_values(parameter, value):
    kwargs = {
        "capacity": 1,
        "refill_per_second": 1.0,
        "idle_ttl_seconds": 60.0,
        "max_principals": 32,
    }
    kwargs[parameter] = value

    with pytest.raises(ValueError, match=parameter):
        PrincipalTokenBucket(**kwargs)


@pytest.mark.parametrize("cost", [0, -1, float("nan"), float("inf"), False])
def test_token_bucket_rejects_invalid_cost(cost):
    limiter = PrincipalTokenBucket(capacity=1, refill_per_second=1.0)

    with pytest.raises(ValueError, match="cost"):
        limiter.consume("phone", cost=cost)
    with pytest.raises(ValueError, match="cost"):
        consume_all(((limiter, "phone", cost),))


@pytest.mark.parametrize("value", [0, -1, False, 1.0, "8"])
def test_sse_admission_rejects_opaque_or_nonpositive_limits(value):
    with pytest.raises(ValueError, match="sse_max_connections"):
        SSEAdmission(value)


def test_sse_admission_is_shared_across_legacy_and_v1_surfaces():
    admission = SSEAdmission(2)
    config = {"mobile_api": {"sse_max_connections": 2}}
    legacy = CompanionAPIEndpoints(config=config, sse_admission=admission)
    mobile = CompanionsV1(config=config, sse_admission=admission)
    identity = "ab" * 32

    assert legacy._begin_sse(("jwt:operator:chat", identity)) is True
    assert mobile._begin_sse("jwt:operator:chat", identity) is False
    assert mobile._begin_sse("jwt:other:chat", identity) is True
    assert legacy._sse_total == mobile._sse_total == 2

    legacy._sse_admission.release("jwt:operator:chat", identity)
    mobile._end_sse("jwt:other:chat", identity)
    assert legacy._sse_total == mobile._sse_total == 0


def test_sse_admission_replaces_only_replaceable_leases():
    admission = SSEAdmission(1)

    first = admission.replace("jwt:operator:chat", "companion")
    second = admission.replace("jwt:operator:chat", "companion")

    assert first is not None
    assert second is not None
    assert admission.is_current("jwt:operator:chat", "companion", first) is False
    assert admission.is_current("jwt:operator:chat", "companion", second) is True
    admission.release("jwt:operator:chat", "companion", first)
    assert admission.active_count == 1
    admission.release("jwt:operator:chat", "companion", second)
    assert admission.active_count == 0

    assert admission.acquire("jwt:operator:chat", "companion") is True
    assert admission.replace("jwt:operator:chat", "companion") is None


def test_legacy_and_v1_use_the_same_jwt_sse_principal(monkeypatch):
    monkeypatch.setattr(
        cherrypy.request,
        "user",
        {
            "auth_type": "jwt",
            "username": "operator",
            "client_id": "chat",
        },
        raising=False,
    )

    assert (
        CompanionAPIEndpoints._sse_principal()
        == CompanionsV1._sse_principal()
        == "jwt:operator:chat"
    )


def test_sse_capacity_reserves_two_http_workers():
    accepted = {
        "http": {"thread_pool": 8, "thread_pool_max": 16},
        "mobile_api": {"sse_max_connections": 14},
    }
    assert CompanionsV1(config=accepted)._sse_admission.max_connections == 14

    rejected = {
        "http": {"thread_pool": 8, "thread_pool_max": 16},
        "mobile_api": {"sse_max_connections": 15},
    }
    with pytest.raises(
        ValueError,
        match=r"mobile_api\.sse_max_connections.*http\.thread_pool_max.*14|"
        r"mobile_api\.sse_max_connections.*at most 14",
    ):
        CompanionsV1(config=rejected)


def test_sse_capacity_rejects_a_two_worker_http_pool():
    config = {
        "http": {"thread_pool": 2, "thread_pool_max": 2},
        "mobile_api": {"sse_max_connections": 1},
    }
    with pytest.raises(
        ValueError,
        match=r"mobile_api\.sse_max_connections.*http\.thread_pool_max",
    ):
        CompanionsV1(config=config)
