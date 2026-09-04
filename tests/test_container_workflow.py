"""Checks for the container runtime quality gate."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_container_workflow_runs_plugin_runtime_smoke_test():
    workflow = (ROOT / ".github/workflows/container-checks.yml").read_text(encoding="utf-8")

    assert "docker build --build-arg PACKAGE_VERSION=0.0.0.dev0" in workflow
    assert "scripts/docker-plugin-smoke.sh openhop-repeater:plugin-smoke" in workflow


def test_container_smoke_script_checks_install_restart_and_shutdown():
    script = (ROOT / "scripts/docker-plugin-smoke.sh").read_text(encoding="utf-8")

    assert "local wheel upload" in script
    assert "container recreation persistence" in script
    assert "graceful plugin shutdown" in script
    assert "EXPECTED_PLUGIN_PID" in script


def test_compose_exposes_service_scoped_github_token():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    docs = (ROOT / "docs/plugins.md").read_text(encoding="utf-8")

    assert "OPENHOP_PLUGIN_GITHUB_TOKEN" in compose
    assert "OPENHOP_PLUGIN_GITHUB_TOKEN" in docs


def test_image_uses_tini_to_reap_orphaned_plugin_processes():
    dockerfile = (ROOT / "dockerfile").read_text(encoding="utf-8")

    assert "    tini \\\n" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/docker-entrypoint.sh"]'
        in dockerfile
    )
