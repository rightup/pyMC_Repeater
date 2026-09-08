"""Exercise entrypoint upgrade defaults without starting the repeater."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"


@pytest.mark.parametrize("bundled", [True, False], ids=["image-upgrade", "stored-fallback"])
def test_upgrade_defaults_preserve_user_config_and_stored_example(tmp_path, bundled):
    install = tmp_path / "install"
    config = tmp_path / "config"
    bin_dir = tmp_path / "bin"
    for directory in (install, config, bin_dir):
        directory.mkdir()
    stored = {"http": {"port": 8000, "old_default": True}}
    current = {"http": {"port": 9000, "new_default": True}}
    user = {"http": {"port": 1234}, "user_only": "keep"}
    example = config / "config.yaml.example"
    example.write_text(json.dumps(stored))
    original_example = example.read_bytes()
    config_path = config / "config.yaml"
    config_path.write_text(json.dumps(user))
    if bundled:
        (install / "config.yaml.example").write_text(json.dumps(current))

    # Only the daemon launcher is stubbed; the real shell entrypoint runs.
    launcher = bin_dir / "python3"
    launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$LAUNCH_ARGS"\n')
    launcher.chmod(0o755)
    # A JSON-only yq double keeps normal unit tests independent of Docker.
    # Set ENTRYPOINT_TEST_YQ to exercise the identical regression with real yq.
    yq = bin_dir / "yq"
    yq.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1] == '--version':\n"
        "    print('mikefarah/yq test double'); sys.exit(0)\n"
        "def merge(a, b):\n"
        "    if isinstance(a, dict) and isinstance(b, dict):\n"
        "        return {k: merge(a.get(k), b[k]) if k in b else a[k] "
        "for k in a.keys() | b.keys()}\n"
        "    return b\n"
        "with open(sys.argv[3]) as f: result = json.load(f)\n"
        "if sys.argv[1] == 'eval-all':\n"
        "    with open(sys.argv[4]) as f: result = merge(result, json.load(f))\n"
        "print(json.dumps(result))\n"
    )
    yq.chmod(0o755)
    args_path = tmp_path / "launch-args"
    env = {
        **os.environ,
        "INSTALL_DIR": str(install),
        "CONFIG_DIR": str(config),
        "OPENHOP_REPEATER_CONFIG": str(config_path),
        "YQ_CMD": os.environ.get("ENTRYPOINT_TEST_YQ", str(yq)),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCH_ARGS": str(args_path),
    }
    result = subprocess.run(
        ["sh", str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    merged = yaml.safe_load(config_path.read_text())
    defaults = current if bundled else stored
    assert merged == {"http": {**defaults["http"], "port": 1234}, "user_only": "keep"}
    assert example.read_bytes() == original_example
    assert args_path.read_text().splitlines() == [
        "-m",
        "repeater.plugins.container_supervisor",
        "--config",
        str(config_path),
    ]
