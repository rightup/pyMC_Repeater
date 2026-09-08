import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repeater.plugins.ipc import PluginIPCClient, PluginIPCServer
from repeater.plugins.manager import (
    PROGRESS_MAX_LINES,
    PROGRESS_MAX_LOGS,
    OperationProgress,
    PluginManager,
    PluginManagerError,
)
from repeater.plugins.runtime import PluginRuntime
from repeater.plugins.storage import PluginStorage
from repeater.web.plugin_endpoints import PluginAPIEndpoints


def test_runtime_hands_each_output_line_to_the_listener(tmp_path: Path):
    runtime = PluginRuntime(PluginStorage(tmp_path / "plugins"))
    heard: list[str] = []
    runtime.on_output = heard.append
    script = "import sys; print('Collecting demo'); print('Installing', end=''); sys.stdout.flush()"
    result = runtime._run_install_command([sys.executable, "-c", script], timeout=30)
    assert result.returncode == 0
    assert heard == ["Collecting demo", "Installing"]
    assert "Collecting demo" in result.stdout

    def broken(_line: str) -> None:
        raise RuntimeError("listener bug")

    runtime.on_output = broken
    assert (
        runtime._run_install_command([sys.executable, "-c", "print('ok')"], timeout=30).returncode
        == 0
    )


def test_progress_log_is_bounded_and_pages_by_cursor():
    log = OperationProgress("openhop.demo", "update")
    for i in range(PROGRESS_MAX_LINES + 20):
        log.append(f"line {i}")
    snap = log.snapshot()
    assert len(snap["lines"]) == PROGRESS_MAX_LINES
    assert snap["lines"][0] == "line 20"
    assert snap["next"] == PROGRESS_MAX_LINES + 20
    log.append("tail")
    assert log.snapshot(since=snap["next"])["lines"] == ["tail"]
    log.finish("boom")
    assert (log.snapshot()["state"], log.snapshot()["error"]) == ("error", "boom")


def test_manager_records_the_outcome_and_restores_the_listener(tmp_path: Path):
    mgr = PluginManager(PluginStorage(tmp_path / "plugins"))
    assert mgr.progress("missing.plugin")["state"] == "idle"
    with pytest.raises(PluginManagerError):
        mgr.update_plugin("missing.plugin")
    snap = mgr.progress("missing.plugin")
    assert (snap["operation"], snap["state"]) == ("update", "error")
    assert "plugin not found" in snap["error"]
    assert mgr.runtime.on_output is None


def test_manager_keeps_a_bounded_number_of_logs(tmp_path: Path):
    mgr = PluginManager(PluginStorage(tmp_path / "plugins"))
    for i in range(PROGRESS_MAX_LOGS + 10):
        with pytest.raises(PluginManagerError):
            mgr.update_plugin(f"nobody.{i:03d}")
    assert len(mgr._progress) == PROGRESS_MAX_LOGS
    assert mgr.progress("nobody.000")["state"] == "idle"


def test_ipc_round_trips_progress(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    manager = PluginManager(storage, PluginRuntime(storage, popen_factory=lambda *a, **k: None))
    sock_path = Path(f"/tmp/oh-pm-progress-{tmp_path.name}.sock")
    server = PluginIPCServer(sock_path, manager)
    server.start()
    try:
        client = PluginIPCClient(sock_path)
        assert client.progress("openhop.demo")["state"] == "idle"
        with pytest.raises(PluginManagerError):
            manager.update_plugin("openhop.demo")
        assert client.progress("openhop.demo", since=0)["state"] == "error"
    finally:
        server.stop()


def _snap(state, lines=(), nxt=0, started=None, error=None):
    return {
        "state": state,
        "operation": "update",
        "lines": list(lines),
        "next": nxt,
        "error": error,
        "started": started,
    }


def _events(tmp_path, snapshots, *, ticks=None, fresh=False):
    api = PluginAPIEndpoints({"storage": {"storage_dir": str(tmp_path)}})
    answers = iter(snapshots)
    client = SimpleNamespace(progress=lambda _id, since=0: next(answers, snapshots[-1]))
    clock = iter(ticks or [0.0] * 1000)
    with patch.object(api, "_client_or_raise", return_value=client):
        chunks = api._progress_events(
            "openhop.demo", 0, fresh=fresh, sleep=lambda _s: None, clock=lambda: next(clock)
        )
        return [json.loads(chunk[len("data: ") : -2]) for chunk in chunks]


def test_stream_plays_lines_status_and_one_done(tmp_path):
    events = _events(
        tmp_path,
        [_snap("running", ["a"], 1), _snap("running", ["b", "c"], 3), _snap("complete", nxt=3)],
    )
    assert [e["type"] for e in events] == [
        "connected",
        "line",
        "status",
        "line",
        "line",
        "status",
        "done",
    ]
    assert [e["line"] for e in events if e["type"] == "line"] == ["a", "b", "c"]
    assert events[-1] == {"type": "done", "state": "complete", "error": None, "started": None}

    events = _events(tmp_path, [_snap("error", ["pip failed"], 1, error="pip")])
    assert events[-1]["state"] == "error" and events[-1]["error"] == "pip"


def test_idle_stream_keeps_alive_then_ends(tmp_path):
    events = _events(tmp_path, [_snap("idle")], ticks=[0.0] + [1.0] * 12 + [100.0])
    types = [e["type"] for e in events]
    assert types[:2] == ["connected", "status"]
    assert types.count("keepalive") == 2
    assert events[-1] == {"type": "done", "state": "idle", "error": None}

    events = _events(tmp_path, [_snap("running", started=5.0)], ticks=[0.0, 100.0, 500.0, 901.0])
    assert events[-1]["state"] == "timeout"


def test_fresh_stream_ignores_a_previous_operations_log(tmp_path):
    previous = _snap("complete", ["old"], 1, started=100.0)
    current = _snap("running", ["new"], 1, started=200.0)
    events = _events(
        tmp_path, [previous, previous, current, _snap("complete", nxt=1, started=200.0)], fresh=True
    )
    assert [e["type"] for e in events] == [
        "connected",
        "status",
        "line",
        "status",
        "status",
        "done",
    ]
    assert events[1]["state"] == "idle"
    assert events[2]["line"] == "new"
    assert events[-1]["started"] == 200.0

    events = _events(tmp_path, [previous])
    assert [e["type"] for e in events] == ["connected", "line", "status", "done"]
