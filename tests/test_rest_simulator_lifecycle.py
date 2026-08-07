"""Lifecycle checks for the process-global real-HTTP simulator."""

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from companion_client import rest_simulator


def _harness(*, loop=None, thread=None):
    return rest_simulator.RestHarness(
        base_url="http://127.0.0.1:1",
        handler=object(),
        bridge=object(),
        companion_name="test",
        companion_hash="0x01",
        token_manager=object(),
        jwt_handler=object(),
        event_loop=loop,
        event_loop_thread=thread,
    )


def test_rest_simulator_stop_joins_and_closes_its_event_loop(monkeypatch):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ready = threading.Event()
    loop.call_soon_threadsafe(ready.set)
    assert ready.wait(timeout=2)
    harness = _harness(loop=loop, thread=thread)
    monkeypatch.setattr(rest_simulator, "_active_harness", harness)
    monkeypatch.setattr(rest_simulator.cherrypy.engine, "exit", lambda: None)

    rest_simulator.stop_rest_harness(harness)

    assert not thread.is_alive()
    assert loop.is_closed()
    assert rest_simulator._active_harness is None

    # Explicit repeat cleanup is harmless once this exact loop is closed.
    rest_simulator.stop_rest_harness(harness)


def test_rest_simulator_rejects_wrong_harness_before_global_shutdown(monkeypatch):
    active = _harness()
    wrong = _harness()
    exit_engine = MagicMock()
    monkeypatch.setattr(rest_simulator, "_active_harness", active)
    monkeypatch.setattr(rest_simulator.cherrypy.engine, "exit", exit_engine)

    with pytest.raises(ValueError, match="not active"):
        rest_simulator.stop_rest_harness(wrong)

    exit_engine.assert_not_called()
    assert rest_simulator._active_harness is active


def test_rest_simulator_startup_failure_stops_its_event_loop(
    tmp_path,
    monkeypatch,
):
    existing_threads = {
        thread.ident for thread in threading.enumerate() if thread.name == "rest-harness-loop"
    }
    monkeypatch.setattr(rest_simulator, "_free_port", lambda: 12345)
    monkeypatch.setattr(
        rest_simulator,
        "MobileAPIEndpoints",
        MagicMock(side_effect=RuntimeError("endpoint setup failed")),
    )
    monkeypatch.setattr(rest_simulator.cherrypy.engine, "exit", lambda: None)

    with pytest.raises(RuntimeError, match="endpoint setup failed"):
        rest_simulator.start_rest_harness(tmp_path)

    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.name == "rest-harness-loop"
        and thread.ident not in existing_threads
        and thread.is_alive()
    ]
    assert leaked == []
    assert rest_simulator._active_harness is None
