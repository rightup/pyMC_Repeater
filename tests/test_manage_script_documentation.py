from pathlib import Path

MANAGE_SCRIPT = Path(__file__).resolve().parents[1] / "manage.sh"
CH341_HOST_SETUP_URL = (
    "https://docs.openhop.dev/projects/openhop-repeater/hardware-setup/#ch341-usb-spi-hosts"
)


def test_container_upgrade_note_links_to_ch341_host_setup():
    script = MANAGE_SCRIPT.read_text(encoding="utf-8")

    assert f"CH341 host-side setup: {CH341_HOST_SETUP_URL}" in script
    assert "See documentation for CH341 host-side setup." not in script
