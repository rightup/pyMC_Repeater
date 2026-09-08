"""Exercise the upgrade summary without running installation/service commands."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("service_running", [0, 1])
def test_silent_upgrade_container_warning_renders_newlines(service_running):
    script = (Path(__file__).resolve().parents[1] / "manage.sh").read_text()
    start = script.index('    local container_note=""')
    end = script.index("\n    return 0\n}", start)
    summary = script[start:end]
    # The nonempty container variable selects the warning without depending on
    # the test host. Only the final output block runs, not manage.sh itself.
    probe = (
        "set -eu\nrender() {\n"
        "local silent=true container=test current_version=old new_version=new\n"
        f"local service_was_running={service_running} service_was_enabled=0\n"
        f"{summary}\n}}\nrender\n"
    )
    env = os.environ.copy()
    env.pop("BASH_ENV", None)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc"],
        input=probe,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    expected = "Upgrade completed successfully!\nVersion: old -> new\n"
    if service_running:
        expected += "✓ Service is running\n"
    expected += (
        "✓ Configuration preserved\n\n\n"
        "⚠ CONTAINER DETECTED:\n"
        "USB udev rules must be set on the HOST, not here.\n"
        "CH341 host-side setup: "
        "https://docs.openhop.dev/projects/openhop-repeater/hardware-setup/#ch341-usb-spi-hosts\n"
        "=== Upgrade Complete ===\n"
    )
    assert result.stdout == expected
    assert result.stderr == ""
