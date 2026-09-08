"""No updater execution: inspect templates and run only benign Python lookup probes."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from repeater.web import update_endpoints as ue

ROOT = Path(__file__).resolve().parents[1]


def wrappers():
    return re.findall(
        r"<<'UPGRADEEOF'\n(.*?)\nUPGRADEEOF", (ROOT / "manage.sh").read_text(), re.DOTALL
    )


@pytest.mark.parametrize("wrapper", wrappers(), ids=["install", "upgrade"])
def test_wrapper_isolates_every_python_invocation(wrapper, tmp_path):
    commands = [
        line.strip()
        for line in wrapper.splitlines()
        if re.match(r'(python3|/usr/bin/python3|"\$VENV_PYTHON") ', line.strip())
    ]
    assert commands
    for line in commands:
        args = shlex.split(line.rstrip("\\"))
        assert args[1] == "-I", line
    # Reproduce module lookup only, never run pip or the updater.
    (tmp_path / "pip.py").write_text("raise AssertionError('must not import caller module')\n")
    env = dict(os.environ, PYTHONPATH=str(tmp_path), PYTHONHOME=str(tmp_path))
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import importlib.util; spec = importlib.util.find_spec('pip'); "
            "print(spec.origin if spec else 'pip-not-installed')",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(tmp_path) not in probe.stdout


@pytest.mark.parametrize("wrapper", wrappers(), ids=["install", "upgrade"])
def test_wrapper_cleans_environment_and_requires_trusted_paths(wrapper):
    assert wrapper.startswith("#!/bin/bash -p\n")
    assert "/usr/bin/env -i" in wrapper
    assert "cd /" in wrapper
    assert "require_root_owned" in wrapper
    assert re.search(r"-perm (?:/?022|-022)", wrapper)
    assert '[[ "$#" -le 2 ]]' in wrapper


@pytest.mark.parametrize("wrapper", wrappers(), ids=["install", "upgrade"])
def test_wrapper_provisions_packaged_service_after_success(wrapper):
    install = wrapper.index("# ---- Install openhop_repeater from git ----")
    provision = wrapper.index("plugins/openhop-plugin-manager.service", install)
    assert provision > install
    assert "install -o root -g root -m 0644" in wrapper[install:]
    assert "systemctl enable openhop-plugin-manager" in wrapper[install:]
    assert "systemctl restart openhop-plugin-manager" in wrapper[install:]
    assert 'if [[ -f "$PLUGIN_UNIT" ]]; then' in wrapper[provision:]
    assert "does not ship a plugin-manager unit" in wrapper[provision:]


def test_built_wheel_contains_service_without_checkout(tmp_path):
    # Build locally with installed build tools only: no pip, network or install.
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "setup.py", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, project / name)
    shutil.copytree(
        ROOT / "repeater", project / "repeater", ignore=shutil.ignore_patterns("__pycache__")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_wheel; build_wheel('dist')",
        ],
        cwd=project,
        env=dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER="1.2.3"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    wheel = next((project / "dist").glob("*.whl"))
    with ZipFile(wheel) as archive:
        assert (
            archive.read("repeater/plugins/openhop-plugin-manager.service")
            == (ROOT / "openhop-plugin-manager.service").read_bytes()
        )


def test_package_delivers_identical_service_template():
    resource = ROOT / "repeater/plugins/openhop-plugin-manager.service"
    assert resource.is_file()
    assert resource.read_bytes() == (ROOT / "openhop-plugin-manager.service").read_bytes()
    assert '"plugins/*.service"' in (ROOT / "pyproject.toml").read_text()


def test_unprivileged_helper_never_provisions_services(monkeypatch):
    monkeypatch.setattr(ue.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ue.os.path, "isfile", lambda path: True)

    def forbidden(*args, **kwargs):
        pytest.fail("Unprivileged HTTP helper must not install or manage system units")

    monkeypatch.setattr(ue.shutil, "copy2", forbidden)
    monkeypatch.setattr(ue.subprocess, "run", forbidden)
    ue._ensure_plugin_manager_service()


def test_root_helper_reads_service_from_new_distribution(monkeypatch):
    monkeypatch.setattr(ue.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ue, "is_container", lambda: False)
    monkeypatch.setattr(ue, "is_buildroot", lambda: False)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(ue.subprocess, "run", run)
    assert ue._ensure_plugin_manager_service() is True
    cmd, kwargs = calls[0]
    assert cmd[:3] == ["/opt/openhop_repeater/venv/bin/python", "-I", "-c"]
    assert "plugins/openhop-plugin-manager.service" in cmd[3]
    assert kwargs["cwd"] == "/"
    assert kwargs["env"]["HOME"] == "/root"


def test_root_helper_reports_service_provision_failure(monkeypatch):
    monkeypatch.setattr(ue.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ue, "is_container", lambda: False)
    monkeypatch.setattr(ue, "is_buildroot", lambda: False)
    monkeypatch.setattr(
        ue.subprocess, "run", lambda *a, **kw: type("Result", (), {"returncode": 1})()
    )
    assert ue._ensure_plugin_manager_service() is False


def test_wrapper_templates_are_identical():
    assert len(wrappers()) == 2
    assert wrappers()[0] == wrappers()[1]


@pytest.mark.parametrize("enabled, expected", [(False, 1), (True, 0), (None, 0)])
def test_service_condition_respects_plugin_opt_out(tmp_path, enabled, expected):
    unit = (ROOT / "openhop-plugin-manager.service").read_text()
    condition = next(
        (line for line in unit.splitlines() if line.startswith("ExecCondition=")), None
    )
    assert condition is not None, "disabled plugins must not become a failed service"
    args = shlex.split(condition.split("=", 1)[1])
    config = tmp_path / "synthetic.yaml"
    config.write_text(
        "plugins:\n  enabled: " + {False: "false", True: "true", None: "null"}[enabled]
    )
    # Only run the condition against synthetic data, not systemd or the manager.
    result = subprocess.run(
        [sys.executable, *args[1:-1], str(config)], capture_output=True, check=False
    )
    assert result.returncode == expected, result.stderr


def test_trusted_path_guard_accepts_system_python():
    wrapper = wrappers()[0]
    guard = wrapper[wrapper.index("require_root_owned() {") : wrapper.index("\nfor path in")]
    # Exercise just the read-only path guard, never the wrapper body.
    result = subprocess.run(
        ["/bin/bash", "-c", guard + "\nrequire_root_owned /usr/bin/python3"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
