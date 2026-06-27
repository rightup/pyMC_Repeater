from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import cherrypy
import pytest

import repeater.web.update_endpoints as ue


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    request = SimpleNamespace(method="GET", json={}, params={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request, response


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    channel_file = tmp_path / "update_channel"
    monkeypatch.setattr(ue, "_CHANNELS_FILE", str(channel_file), raising=False)
    monkeypatch.setattr(ue, "_detect_channel_from_dist_info", lambda: None)
    monkeypatch.setattr(ue, "_get_installed_version", lambda force_refresh=False: "1.0.0")
    st = ue._UpdateState()
    monkeypatch.setattr(ue, "_state", st, raising=False)
    return st


def _fake_thread(*args, **kwargs):
    return SimpleNamespace(start=lambda: None, name=kwargs.get("name", "t"))


def test_jwt_warning_fix_guard():
    # Guard test file import path and ensure this module executes in suite.
    assert ue.PACKAGE_NAME == "openhop_repeater"


def test_has_update_paths():
    assert ue._has_update("1.2.3", "1.2.3") is False
    assert ue._has_update("1.2.3", "1.2.4") is True
    assert ue._has_update("1.2.4.dev10", "1.2.4.dev12") is True


def test_fetch_url_success_and_rate_limit(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"ok"

    monkeypatch.setattr(ue.urllib.request, "urlopen", lambda *args, **kwargs: _Resp())
    assert ue._fetch_url("https://api.github.com/test") == "ok"

    reset = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    hdrs = {"X-RateLimit-Reset": str(reset)}

    def _raise(*args, **kwargs):
        raise ue.urllib.error.HTTPError("u", 403, "forbidden", hdrs, None)

    monkeypatch.setattr(ue.urllib.request, "urlopen", _raise)
    with pytest.raises(ue._RateLimitError) as exc:
        ue._fetch_url("https://api.github.com/test")
    assert "rate limit" in str(exc.value).lower()


def test_update_state_snapshot_and_mutators(isolated_state, monkeypatch):
    st = isolated_state
    monkeypatch.setattr(ue, "_get_installed_version", lambda force_refresh=False: "1.0.1")

    st.latest_version = "1.0.2"
    snap = st.snapshot()
    assert snap["current_version"] == "1.0.1"
    assert snap["has_update"] is True

    st.set_channel("dev")
    assert st.channel == "dev"
    assert st.latest_version is None
    assert st.has_update is False

    assert st._set_checking() is True
    assert st._set_checking() is False

    t = _fake_thread(name="install")
    assert st.start_install(t) is True
    assert st.start_install(t) is False

    st.finish_install(True, "done")
    assert st.state == "complete"


def test_update_state_append_line_trim(isolated_state):
    st = isolated_state
    for i in range(510):
        st.append_line(f"l-{i}")
    assert len(st.progress_lines) == 500
    assert st.progress_lines[0].startswith("l-")


def test_status_endpoint_options_and_ok(cherrypy_ctx, isolated_state):
    request, _ = cherrypy_ctx
    api = ue.UpdateAPIEndpoints()

    request.method = "OPTIONS"
    assert api.status() == ""

    request.method = "GET"
    out = api.status()
    assert out["success"] is True
    assert out["current_version"] == "1.0.0"


def test_check_endpoint_paths(cherrypy_ctx, isolated_state, monkeypatch):
    request, _ = cherrypy_ctx
    api = ue.UpdateAPIEndpoints()

    request.method = "OPTIONS"
    assert api.check() == ""

    request.method = "PUT"
    with pytest.raises(cherrypy.HTTPError):
        api.check()

    request.method = "GET"
    isolated_state.state = "checking"
    busy = api.check()
    assert busy["success"] is True
    assert busy["state"] == "checking"

    isolated_state.state = "idle"
    isolated_state.latest_version = "1.0.2"
    isolated_state.last_checked = datetime.now(timezone.utc)
    cached = api.check()
    assert cached["success"] is True
    assert "cached" in cached["message"].lower()

    isolated_state.last_checked = None
    isolated_state.latest_version = None
    request.method = "POST"
    request.json = {"force": True}
    monkeypatch.setattr(ue.threading, "Thread", _fake_thread)
    started = api.check()
    assert started["success"] is True
    assert started["state"] == "checking"


def test_check_endpoint_rate_limit_window(cherrypy_ctx, isolated_state):
    request, _ = cherrypy_ctx
    api = ue.UpdateAPIEndpoints()
    request.method = "POST"
    request.json = {}

    isolated_state.rate_limit_until = datetime.now(timezone.utc) + timedelta(minutes=1)
    out = api.check()
    assert out["success"] is True
    assert "rate limit" in out["message"].lower()


def test_install_endpoint_paths(cherrypy_ctx, isolated_state, monkeypatch):
    request, response = cherrypy_ctx
    api = ue.UpdateAPIEndpoints()

    request.method = "GET"
    with pytest.raises(cherrypy.HTTPError):
        api.install()

    request.method = "POST"
    request.json = {}
    isolated_state.state = "installing"
    out = api.install()
    assert out["success"] is False
    assert response.status == 409

    isolated_state.state = "idle"
    isolated_state.latest_version = "1.0.0"
    isolated_state.has_update = False
    up_to_date = api.install()
    assert up_to_date["success"] is False
    assert response.status == 409

    request.json = {"force": True}
    monkeypatch.setattr(ue.threading, "Thread", _fake_thread)
    isolated_state.state = "idle"
    isolated_state.latest_version = None
    isolated_state.has_update = False
    ok = api.install()
    assert ok["success"] is True
    assert ok["state"] == "installing"


def test_progress_endpoint_stream(cherrypy_ctx, isolated_state):
    _, response = cherrypy_ctx
    api = ue.UpdateAPIEndpoints()

    isolated_state.state = "complete"
    isolated_state.progress_lines = ["line-1"]

    stream = api.progress()
    chunks = list(stream)

    assert response.headers["Content-Type"] == "text/event-stream"
    joined = "".join(chunks)
    assert "connected" in joined
    assert "line-1" in joined
    assert "done" in joined


def test_channels_set_channel_and_changelog(cherrypy_ctx, isolated_state, monkeypatch):
    request, response = cherrypy_ctx
    api = ue.UpdateAPIEndpoints()

    request.method = "OPTIONS"
    assert api.channels() == ""
    assert api.set_channel() == ""
    assert api.changelog() == ""

    request.method = "GET"
    monkeypatch.setattr(ue, "_fetch_branches", lambda: ["main", "dev"])
    ch = api.channels()
    assert ch["success"] is True
    assert ch["channels"][0] == "main"

    request.method = "POST"
    request.json = {}
    bad = api.set_channel()
    assert bad["success"] is False
    assert response.status == 400

    request.json = {"channel": "dev"}
    isolated_state.state = "installing"
    blocked = api.set_channel()
    assert blocked["success"] is False
    assert response.status == 409

    isolated_state.state = "idle"
    ok = api.set_channel()
    assert ok["success"] is True
    assert ok["channel"] == "dev"

    request.method = "GET"
    monkeypatch.setattr(
        ue, "_fetch_changelog", lambda channel, installed, max_commits: [{"title": "t"}]
    )
    c = api.changelog(channel="dev", max="5")
    assert c["success"] is True
    assert c["commits"][0]["title"] == "t"


def test_changelog_guard_when_github_unavailable(cherrypy_ctx, isolated_state, monkeypatch):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = ue.UpdateAPIEndpoints()

    def _raise_fetch(*_args, **_kwargs):
        raise RuntimeError("HTTP Error 404: Not Found")

    monkeypatch.setattr(ue, "_fetch_changelog", _raise_fetch)

    out = api.changelog(channel="dev", max="5")
    assert out["success"] is True
    assert out["channel"] == "dev"
    assert out["commits"] == []


def test_cors_headers_and_error_helpers(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = ue.UpdateAPIEndpoints()

    api._set_cors_headers({"web": {"cors_enabled": True}})
    assert response.headers["Access-Control-Allow-Origin"] == "*"

    response.status = 200
    err = api._err("nope", status=418)
    assert err["success"] is False
    assert response.status == 418


def test_do_check_success_rate_limit_and_generic_error(isolated_state, monkeypatch):
    st = isolated_state

    monkeypatch.setattr(ue, "_fetch_latest_version", lambda _channel: "1.0.2")
    ue._do_check()
    assert st.latest_version == "1.0.2"
    assert st.state == "idle"
    assert st.has_update is True

    reset_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    monkeypatch.setattr(
        ue,
        "_fetch_latest_version",
        lambda _channel: (_ for _ in ()).throw(ue._RateLimitError("limited", reset_at=reset_at)),
    )
    ue._do_check()
    assert st.state == "idle"
    assert st.rate_limit_until == reset_at
    assert "limited" in (st.error_message or "")

    monkeypatch.setattr(
        ue,
        "_fetch_latest_version",
        lambda _channel: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    ue._do_check()
    assert st.state == "error"
    assert "boom" in (st.error_message or "")


def test_fetch_branches_priority_and_fallback(monkeypatch):
    monkeypatch.setattr(
        ue,
        "_fetch_url",
        lambda _url, timeout=8: '[{"name":"feature"},{"name":"dev"},{"name":"main"}]',
    )
    out = ue._fetch_branches()
    assert out[:2] == ["main", "dev"]
    assert "feature" in out

    monkeypatch.setattr(
        ue,
        "_fetch_url",
        lambda _url, timeout=8: (_ for _ in ()).throw(RuntimeError("net down")),
    )
    out2 = ue._fetch_branches()
    assert out2 == ["main"]


def test_fetch_latest_version_dynamic_and_static(monkeypatch):
    monkeypatch.setattr(ue, "_get_latest_tag", lambda: "1.0.5")

    # Dynamic branch path uses compare ahead_by -> next dev version.
    monkeypatch.setattr(ue, "_branch_is_dynamic", lambda _ch: True)
    monkeypatch.setattr(ue, "_fetch_url", lambda _url, timeout=10: '{"ahead_by": 3}')
    dyn = ue._fetch_latest_version("dev")
    assert dyn == "1.0.6.dev3"

    # Static branch path parses version from pyproject content.
    monkeypatch.setattr(ue, "_branch_is_dynamic", lambda _ch: False)
    monkeypatch.setattr(
        ue,
        "_fetch_url",
        lambda _url, timeout=8: 'name = "x"\nversion = "2.3.4"\n',
    )
    stat = ue._fetch_latest_version("main")
    assert stat == "2.3.4"


def test_do_install_non_root_wrapper_missing_finishes_error(isolated_state, monkeypatch):
    st = isolated_state
    st.channel = "main"
    st.latest_version = "1.2.3"

    monkeypatch.setattr(ue.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ue, "is_buildroot", lambda: False)
    monkeypatch.setattr(ue.os.path, "isfile", lambda p: False)

    ue._do_install()

    assert st.state == "error"
    assert "Upgrade wrapper not found" in (st.error_message or "")


def test_do_install_root_buildroot_helper_missing(isolated_state, monkeypatch):
    st = isolated_state
    st.channel = "dev"
    st.latest_version = "2.0.0"

    monkeypatch.setattr(ue.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ue, "is_buildroot", lambda: True)
    monkeypatch.setattr(ue, "_find_buildroot_upgrade_helper", lambda: None)

    ue._do_install()

    assert st.state == "error"
    assert "Buildroot upgrade helper not found" in (st.error_message or "")


def test_enable_openhop_service_root_and_non_root(isolated_state, monkeypatch):
    calls = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ue.os.path, "exists", lambda p: False)
    monkeypatch.setattr(ue.subprocess, "run", _run)

    monkeypatch.setattr(ue.os, "geteuid", lambda: 0)
    ue._enable_openhop_service()
    assert calls[-1][0] == [ue._SYSTEMCTL_BIN, "enable", "openhop-repeater.service"]

    monkeypatch.setattr(ue.os, "geteuid", lambda: 1000)
    ue._enable_openhop_service()
    assert calls[-1][0] == [
        ue._SUDO_BIN,
        ue._SUDO_SYSTEMCTL_BIN,
        "enable",
        "openhop-repeater.service",
    ]
    assert any("starts on boot" in line for line in isolated_state.progress_lines)


def test_do_install_root_install_command_failure_sets_error(isolated_state, monkeypatch):
    st = isolated_state
    st.channel = "main"
    st.latest_version = "3.1.4"

    monkeypatch.setattr(ue.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ue, "is_buildroot", lambda: False)
    monkeypatch.setattr(ue, "_migrate_service_unit", lambda: None)
    monkeypatch.setattr(ue, "_disable_legacy_services", lambda: None)
    monkeypatch.setattr(ue, "_enable_openhop_service", lambda: None)
    monkeypatch.setattr(ue.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(ue.os.path, "isdir", lambda p: False)

    class _Proc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.stdout = []
            self.returncode = (
                1 if any(isinstance(x, str) and "git+https://github.com" in x for x in cmd) else 0
            )

        def wait(self):
            return None

    monkeypatch.setattr(ue.subprocess, "Popen", lambda cmd, **kwargs: _Proc(cmd))

    ue._do_install()

    assert st.state == "error"
    assert "pip install failed" in (st.error_message or "")


def test_do_install_wrapper_success_then_restart_failure(isolated_state, monkeypatch):
    st = isolated_state
    st.channel = "main"
    st.latest_version = "4.0.0"

    monkeypatch.setattr(ue.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ue, "is_buildroot", lambda: False)
    monkeypatch.setattr(ue, "_cleanup_stale_dist_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(ue, "_enable_openhop_service", lambda: None)
    monkeypatch.setattr(ue.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ue.os.path, "isfile", lambda p: p == "/usr/local/bin/pymc-do-upgrade")
    monkeypatch.setattr(
        "repeater.service_utils.restart_service", lambda: (False, "systemctl failed")
    )

    class _Proc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.stdout = ["ok\n"]
            self.returncode = 0

        def wait(self):
            return None

    monkeypatch.setattr(ue.subprocess, "Popen", lambda cmd, **kwargs: _Proc(cmd))

    ue._do_install()

    assert st.state == "error"
    assert "restart failed" in (st.error_message or "")


def test_do_install_wrapper_success_container_path(isolated_state, monkeypatch):
    st = isolated_state
    st.channel = "main"
    st.latest_version = "5.0.0"

    monkeypatch.setattr(ue.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ue, "is_buildroot", lambda: False)
    monkeypatch.setattr(ue, "is_container", lambda: True)
    monkeypatch.setattr(ue, "get_container_restart_message", lambda: "container will restart")
    monkeypatch.setattr(ue, "_cleanup_stale_dist_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(ue, "_enable_openhop_service", lambda: None)
    monkeypatch.setattr(ue.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ue.os.path, "isfile", lambda p: p == "/usr/local/bin/pymc-do-upgrade")
    monkeypatch.setattr("repeater.service_utils.restart_service", lambda: (True, "ok"))

    class _Proc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.stdout = ["ok\n"]
            self.returncode = 0

        def wait(self):
            return None

    monkeypatch.setattr(ue.subprocess, "Popen", lambda cmd, **kwargs: _Proc(cmd))

    ue._do_install()

    assert st.state == "complete"
    assert st.error_message is None
    assert any("container will restart" in line for line in st.progress_lines)
