import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_unwritable_merge_never_switches_to_ephemeral_config(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    config_dir = tmp_path / "config"
    install_dir = tmp_path / "install"
    fake_bin = tmp_path / "bin"
    config_dir.mkdir()
    install_dir.mkdir()
    fake_bin.mkdir()

    config_path = config_dir / "config.yaml"
    original = "repeater:\n  node_name: durable\n"
    config_path.write_text(original, encoding="utf-8")
    (config_dir / "config.yaml.example").write_text(
        "http:\n  enabled: true\n",
        encoding="utf-8",
    )

    _write_executable(
        fake_bin / "cp",
        """#!/bin/sh
if [ "${2:-}" = "${FAIL_CP_DEST:-}" ]; then
    case "${1:-}" in
        */config.merged.yaml) exit 1 ;;
    esac
fi
exec /bin/cp "$@"
""",
    )
    _write_executable(
        fake_bin / "yq",
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo "mikefarah/yq version test"
    exit 0
fi
if [ "${1:-}" = "eval-all" ]; then
    printf 'http:\\n  enabled: true\\nmerged: true\\n'
    exit 0
fi
if [ "${1:-}" = "eval" ] && [ "${2:-}" = "." ]; then
    exit 0
fi
last=""
for argument in "$@"; do
    last="${argument}"
done
exec /bin/cat "${last}"
""",
    )
    _write_executable(
        fake_bin / "python3",
        """#!/bin/sh
printf 'python3 %s\\n' "$*"
""",
    )

    environment = {
        **os.environ,
        "CONFIG_DIR": str(config_dir),
        "INSTALL_DIR": str(install_dir),
        "OPENHOP_REPEATER_CONFIG": str(config_path),
        "YQ_CMD": str(fake_bin / "yq"),
        "FAIL_CP_DEST": str(config_path),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["/bin/sh", str(project_root / "docker-entrypoint.sh")],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert config_path.read_text(encoding="utf-8") == original
    assert result.stdout.strip().endswith(f"--config {config_path}")
    assert "Using merged config from" not in result.stderr
    assert "security credentials must never be generated" in result.stderr
