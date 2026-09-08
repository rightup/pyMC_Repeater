import pytest

from repeater.web.auth_endpoints import _LoginThrottle


@pytest.mark.parametrize("blocked", ["ip", "user", "global"])
def test_blocked_requests_do_not_allocate_states(blocked):
    throttle = _LoginThrottle(
        per_ip_threshold=1 if blocked == "ip" else 99,
        per_user_threshold=1 if blocked == "user" else 99,
        global_threshold=1 if blocked == "global" else 99,
        time_fn=lambda: 1000,
    )
    assert throttle.register_failure("ip", "user") > 0
    for i in range(100):
        ip = "ip" if blocked == "ip" else f"ip-{i}"
        user = "user" if blocked == "user" else f"user-{i}"
        assert throttle.get_retry_after(ip, user) > 0
    assert len(throttle._ip_states) == 1
    assert len(throttle._user_states) == 1


def test_retained_keys_have_bounded_size_and_success_clears_them():
    throttle = _LoginThrottle()
    ip, user = "i" * 100000, "u" * 100000
    assert throttle.register_failure(ip, user) == 0
    assert max(map(len, throttle._ip_states)) <= 64
    assert max(map(len, throttle._user_states)) <= 64
    throttle.register_success(ip, f" {user.upper()} ")
    assert not throttle._ip_states
    assert not throttle._user_states


def test_unfailed_lookups_do_not_allocate_states():
    throttle = _LoginThrottle()
    for i in range(100):
        assert throttle.get_retry_after(f"ip-{i}", f"user-{i}") == 0
    assert not throttle._ip_states
    assert not throttle._user_states


@pytest.mark.parametrize("start", [0, 1000])
def test_expired_states_are_removed_without_revisiting_keys(start):
    now = [start]
    throttle = _LoginThrottle(time_fn=lambda: now[0])
    throttle.register_failure("old-ip", "old-user")
    now[0] += throttle.window_sec + 1
    assert throttle.get_retry_after("new-ip", "new-user") == 0
    assert not throttle._ip_states
    assert not throttle._user_states
    assert throttle._global_state["failures"] == 0


def test_capacity_fails_closed_without_evicting_active_failures():
    now = [1000]
    throttle = _LoginThrottle(
        per_ip_threshold=2,
        per_user_threshold=2,
        global_threshold=10000,
        time_fn=lambda: now[0],
    )
    # A small cap exercises the same bounded-store path as the default limit.
    throttle.max_states = 3
    for i in range(3):
        assert throttle.register_failure(f"ip-{i}", f"user-{i}") == 0
    for i in range(3, 100):
        assert throttle.get_retry_after(f"ip-{i}", f"user-{i}") > 0
        # A failure already in flight must not bypass the capacity bound either.
        assert throttle.register_failure(f"ip-{i}", f"user-{i}") > 0
    assert len(throttle._ip_states) == 3
    assert len(throttle._user_states) == 3
    assert throttle.register_failure("ip-0", " USER-0 ") > 0
    now[0] += throttle.window_sec + 1
    assert throttle.get_retry_after("new-ip", "new-user") == 0
    assert throttle.register_failure("new-ip", "new-user") == 0
    assert len(throttle._ip_states) == 1
    assert len(throttle._user_states) == 1


def test_expiry_does_not_shorten_active_backoff():
    now = [1000]
    throttle = _LoginThrottle(
        per_ip_threshold=1,
        per_user_threshold=1,
        global_threshold=1,
        window_sec=5,
        base_backoff_sec=20,
        max_backoff_sec=20,
        time_fn=lambda: now[0],
    )
    assert throttle.register_failure("ip", "user") == 20
    now[0] += 6
    assert throttle.get_retry_after("ip", "user") == 14
    now[0] += 15
    assert throttle.get_retry_after("ip", "user") == 0
    assert not throttle._ip_states
    assert not throttle._user_states
