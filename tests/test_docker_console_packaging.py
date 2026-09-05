"""Console is installed through plugins, not fetched during Docker builds."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative",
    [
        "dockerfile",
        "docker-compose.build.yml",
        ".github/workflows/docker-publish.yml",
        "env.example",
    ],
)
def test_docker_build_has_no_console_bundle_dependency(relative):
    content = (ROOT / relative).read_text().lower()
    for obsolete in (
        "openhop_console",
        "pymc_console",
        "console_release",
        "pymc-ui-",
        "/opt/pymc_console",
    ):
        assert obsolete not in content, (relative, obsolete)
