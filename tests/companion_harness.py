"""Re-export of the simulator harness.

The harness itself lives in :mod:`companion_client.simulator` because the web
demo needs it too. It stays out of ``companion_client.protocol`` /
``client`` / ``push_listener``, which are deliberately free of any
``repeater.*`` import so they exercise the server purely over the wire.
"""

from companion_client.simulator import (  # noqa: F401
    FakeBridge,
    FakeChannel,
    FakeMessageQueue,
    FakePrefs,
    Harness,
    start_harness,
    wait_for,
)
