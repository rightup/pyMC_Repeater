"""Shared test guards.

`RepeaterDaemon._shutdown` arms an exit watchdog: a `threading.Timer` that calls
`os._exit(0)` once `SHUTDOWN_EXIT_GRACE_S` has elapsed. That is correct in
production -- it guarantees SIGTERM is honoured -- but a test that reaches
`_shutdown` without patching it leaves a live timer behind. When it fires, five
seconds later, it takes the whole pytest process down **with status 0**, part
way through an unrelated test.

The damage is that it looks like success: pytest exits 0 with no summary line
and a truncated progress bar, so CI reports green on a run that never finished
and any failure after that point is invisible. The test blamed in the output is
whichever happened to be running when the timer fired, so it moves between runs
and does not point at the cause.

The autouse fixture below fails the *responsible* test instead, at the moment it
leaks the timer.
"""
import threading

import pytest

_WATCHDOG_NAME = "shutdown-watchdog"


def _live_watchdogs():
    return [t for t in threading.enumerate() if t.name == _WATCHDOG_NAME and t.is_alive()]


@pytest.fixture(autouse=True)
def no_leaked_exit_watchdog():
    """Fail any test that leaves a live shutdown-exit watchdog behind.

    Patch it out when the code under test reaches `_shutdown`:

        with patch.object(daemon, "_arm_exit_watchdog"):
            await daemon.run()

    or, when the watchdog itself is what you are testing, keep the grace short
    and assert on a patched `repeater.main.os._exit` before the test returns.
    """
    yield

    leaked = _live_watchdogs()
    for timer in leaked:
        timer.cancel()

    if leaked:
        pytest.fail(
            "test left %d live '%s' timer(s) running. Each one calls os._exit(0) "
            "when it fires and will kill the pytest process mid-run, with exit "
            "status 0 and no summary. Patch _arm_exit_watchdog for this test."
            % (len(leaked), _WATCHDOG_NAME),
            pytrace=False,
        )
