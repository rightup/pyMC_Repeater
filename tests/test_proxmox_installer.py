from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "proxmox-install.sh"
UPDATER = ROOT / "scripts" / "openhop-update"
README = ROOT / "README.md"


def test_proxmox_installer_uses_debian_13() -> None:
    script = INSTALLER.read_text()

    assert 'CT_TEMPLATE="debian-13-standard"' in script
    assert "Downloading Debian 13 template" in script
    assert "sort -V" in script
    assert "Debian 12" not in script


def test_template_selection_matches_the_proxmox_host_architecture() -> None:
    script = INSTALLER.read_text()

    assert "CT_ARCH=$(dpkg --print-architecture)" in script
    assert '[[ ! "$CT_ARCH" =~ ^(amd64|arm64)$ ]]' in script
    assert '-v arch="$CT_ARCH"' in script
    assert '$2 ~ ("_" arch "\\\\.tar\\\\.")' in script


def test_installer_allows_explicit_repository_and_branch_for_fork_testing() -> None:
    script = INSTALLER.read_text()

    assert 'REPO="${OPENHOP_REPO:-https://github.com/openhop-dev/openhop_repeater.git}"' in script
    assert 'BRANCH="${OPENHOP_BRANCH:-dev}"' in script


def test_ch341_host_rule_is_opt_in() -> None:
    script = INSTALLER.read_text()

    prompt = "Install host-side CH341 udev rule? [y/N]"
    rule = "/etc/udev/rules.d/99-ch341.rules"
    rule_section = script.split("# ── Host udev rule", 1)[1].split("# ── Start container", 1)[0]

    assert prompt in script
    assert 'if [[ "$INSTALL_CH341_UDEV" == "true" ]]; then' in rule_section
    assert rule in rule_section
    assert "CH341 host udev rule: ${CH341_UDEV_SUMMARY}" in script


def test_git_branch_is_validated_and_not_evaluated_by_a_shell() -> None:
    script = INSTALLER.read_text()

    assert 'is_safe_git_ref "$BRANCH"' in script
    assert 'pct exec "$CTID" -- git clone --branch "$BRANCH" "$REPO"' in script
    assert 'bash -c "git clone --branch ${BRANCH}' not in script


def test_password_default_meets_proxmox_minimum_length() -> None:
    script = INSTALLER.read_text()

    assert 'CT_PASSWORD_DEFAULT="openHop1!"' in script
    assert 'CT_PASSWORD="${CT_PASSWORD:-$CT_PASSWORD_DEFAULT}"' in script
    assert "Root password [pymc]" not in script


def test_optional_vlan_is_validated_and_added_to_net0() -> None:
    script = INSTALLER.read_text()

    assert "VLAN ID [none]" in script
    assert "CT_VLAN >= 1 && CT_VLAN <= 4094" in script
    assert 'CT_NET0+=",tag=${CT_VLAN}"' in script
    assert '--net0 "$CT_NET0"' in script
    assert "VLAN: ${VLAN_SUMMARY}" in script


def test_installer_fails_when_container_network_never_becomes_ready() -> None:
    script = INSTALLER.read_text()

    assert "NETWORK_READY=false" in script
    assert "NETWORK_READY=true" in script
    assert "Container network did not become ready after 30 seconds" in script


def test_console_is_opt_in_installed_after_repeater_and_not_enabled() -> None:
    script = INSTALLER.read_text()

    assert "Install optional openHop Console WebUI? [y/N]" in script
    assert "openHop Console WebUI: ${CONSOLE_SUMMARY}" in script
    assert script.index("Installing optional openHop Console WebUI") > script.index(
        'msg_ok "manage.sh install completed"'
    )
    assert "pymc-ui-latest.tar.gz" in script
    assert "Console is installed but not enabled" in script
    assert "web.web_path" not in script


def test_installer_adds_update_command() -> None:
    script = INSTALLER.read_text()

    assert (
        "install -m 0755 /root/openhop-repeater/scripts/openhop-update "
        "/usr/local/bin/openhop-update"
    ) in script
    assert "ln -sfn /usr/local/bin/openhop-update /usr/local/bin/update" in script
    assert "Update: update" in script


def test_update_command_updates_apt_and_current_repeater_branch() -> None:
    updater = UPDATER.read_text()

    assert "apt-get update" in updater
    assert "apt-get upgrade -y" in updater
    assert 'BRANCH=$(git -C "$REPO_DIR" branch --show-current)' in updater
    assert 'git -C "$REPO_DIR" status --porcelain' in updater
    assert 'merge-base --is-ancestor HEAD "origin/$BRANCH"' in updater
    assert 'git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"' in updater
    assert 'OPENHOP_UPGRADE_REF="$BRANCH"' in updater
    assert "bash manage.sh upgrade" in updater
    assert 'install -m 0755 "$REPO_DIR/manage.sh" /opt/openhop_repeater/manage.sh' in updater
    assert 'install -m 0755 "$REPO_DIR/scripts/openhop-update"' in updater


def test_readme_documents_new_installer_behavior() -> None:
    readme = README.read_text()

    assert "Download a Debian 13 LXC template" in readme
    assert "host-side CH341 udev rule" in readme
    assert "openHop Console WebUI" in readme
    assert "Run `update` inside the LXC" in readme
