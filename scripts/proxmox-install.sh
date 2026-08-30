#!/usr/bin/env bash
# openHop Repeater - Proxmox LXC Installer
# Creates an LXC container with USB passthrough and installs openHop Repeater
#
# Usage (run on the Proxmox host):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/openhop-dev/openhop_repeater/main/scripts/proxmox-install.sh)"
#
# License: MIT
# Source: https://github.com/openhop-dev/openhop_repeater

set -euo pipefail

# ── Defaults ───────────────────────────────────────────────────────────────
REPO="${OPENHOP_REPO:-https://github.com/openhop-dev/openhop_repeater.git}"
BRANCH="${OPENHOP_BRANCH:-dev}"
CT_TEMPLATE="debian-13-standard"
CT_RAM=1024
CT_SWAP=512
CT_DISK=4
CT_CORES=2
CT_HOSTNAME="openhop-repeater"
CT_BRIDGE="vmbr0"
CT_VLAN=""
CT_STORAGE="local-lvm"
CT_TEMPLATE_STORAGE="local"
CT_PASSWORD_DEFAULT="openHop1!"
CH341_VID="1a86"
CH341_PID="5512"
CONSOLE_RELEASE_URL="https://github.com/Treehouse-00/pymc_console-dist/releases/latest/download/pymc-ui-latest.tar.gz"
CONSOLE_REPO="https://github.com/Treehouse-00/pymc_console-dist.git"
INSTALL_CH341_UDEV=false
INSTALL_CONSOLE=false

# ── Colors ─────────────────────────────────────────────────────────────────
RD="\033[01;31m" GN="\033[1;92m" YW="\033[33m" BL="\033[36m" BLD="\033[1m" CL="\033[m"

msg_info()  { echo -e " ${BL}ℹ${CL}  ${1}"; }
msg_ok()    { echo -e " ${GN}✓${CL}  ${1}"; }
msg_warn()  { echo -e " ${YW}⚠${CL}  ${1}"; }
msg_error() { echo -e " ${RD}✗${CL}  ${1}"; }

is_safe_git_ref() {
    local ref="${1:-}"
    [[ "$ref" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,79}$ ]] \
        && [[ "$ref" != *..* ]] \
        && [[ "$ref" != *//* ]] \
        && [[ "$ref" != */ ]] \
        && [[ "$ref" != *. ]] \
        && [[ "$ref" != *.lock ]]
}

header() {
    clear
    echo -e "${BLD}"
    echo "═══════════════════════════════════════════════════════════════"
    echo "         openHop Repeater - Proxmox LXC Installer"
    echo "═══════════════════════════════════════════════════════════════"
    echo -e "${CL}"
}

cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ] && [ -n "${CTID:-}" ] && pct status "$CTID" &>/dev/null; then
        echo ""
        read -p "  Delete the failed container ${CTID}? [y/N]: " -r
        if [[ "$REPLY" =~ ^[Yy]$ ]]; then
            pct stop "$CTID" 2>/dev/null || true
            pct destroy "$CTID" 2>/dev/null || true
            msg_ok "Container ${CTID} removed"
        fi
    fi
}
trap cleanup EXIT

# ── Preflight checks ──────────────────────────────────────────────────────
header

if ! command -v pct &>/dev/null; then
    msg_error "This script must be run on a Proxmox VE host."
    exit 1
fi

if [ "$EUID" -ne 0 ]; then
    msg_error "Please run as root"
    exit 1
fi

msg_ok "Running on Proxmox host as root"

CT_ARCH=$(dpkg --print-architecture)
if [[ ! "$CT_ARCH" =~ ^(amd64|arm64)$ ]]; then
    msg_error "Unsupported Proxmox host architecture: ${CT_ARCH}"
    exit 1
fi
msg_ok "Detected Proxmox host architecture: ${CT_ARCH}"

# Default to the next available container ID, but allow the user to choose.
DEFAULT_CTID=$(pvesh get /cluster/nextid)

# ── Interactive settings ──────────────────────────────────────────────────
echo ""
echo -e "${BLD}Container Settings${CL} (press Enter for defaults):"
echo ""

while true; do
    read -p "  Container ID [${DEFAULT_CTID}]: " -r input
    CTID="${input:-$DEFAULT_CTID}"
    if [[ ! "$CTID" =~ ^[0-9]+$ ]]; then
        msg_warn "Container ID must be a number"
        continue
    fi
    if pct status "$CTID" &>/dev/null; then
        msg_warn "Container ID ${CTID} already exists"
        continue
    fi
    break
done
read -p "  Hostname [${CT_HOSTNAME}]: " -r input; CT_HOSTNAME="${input:-$CT_HOSTNAME}"
read -p "  RAM in MB [${CT_RAM}]: " -r input; CT_RAM="${input:-$CT_RAM}"
read -p "  Disk in GB [${CT_DISK}]: " -r input; CT_DISK="${input:-$CT_DISK}"
read -p "  CPU cores [${CT_CORES}]: " -r input; CT_CORES="${input:-$CT_CORES}"
read -p "  Bridge [${CT_BRIDGE}]: " -r input; CT_BRIDGE="${input:-$CT_BRIDGE}"
while true; do
    read -p "  VLAN ID [none]: " -r input
    CT_VLAN="${input:-}"
    if [[ -z "$CT_VLAN" ]]; then
        break
    fi
    if [[ "$CT_VLAN" =~ ^[0-9]+$ ]] && (( CT_VLAN >= 1 && CT_VLAN <= 4094 )); then
        break
    fi
    msg_warn "VLAN ID must be blank or a number from 1 to 4094"
done

AVAILABLE_STORAGES=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 {print $1}' || echo "local-lvm")
echo "  Available storages: ${AVAILABLE_STORAGES}"
read -p "  Storage [${CT_STORAGE}]: " -r input; CT_STORAGE="${input:-$CT_STORAGE}"
read -p "  Git branch [${BRANCH}]: " -r input; BRANCH="${input:-$BRANCH}"
if ! is_safe_git_ref "$BRANCH"; then
    msg_error "Invalid Git branch: ${BRANCH}"
    exit 1
fi
read -p "  Install host-side CH341 udev rule? [y/N]: " -r input
[[ "${input:-n}" =~ ^[Yy]([Ee][Ss])?$ ]] && INSTALL_CH341_UDEV=true
read -p "  Install optional openHop Console WebUI? [y/N]: " -r input
[[ "${input:-n}" =~ ^[Yy]([Ee][Ss])?$ ]] && INSTALL_CONSOLE=true
read -rsp "  Root password [${CT_PASSWORD_DEFAULT}]: " CT_PASSWORD; echo
CT_PASSWORD="${CT_PASSWORD:-$CT_PASSWORD_DEFAULT}"

CH341_UDEV_SUMMARY="No"
CONSOLE_SUMMARY="No"
VLAN_SUMMARY="${CT_VLAN:-none}"
if [[ "$INSTALL_CH341_UDEV" == "true" ]]; then
    CH341_UDEV_SUMMARY="Yes"
    if lsusb -d "${CH341_VID}:${CH341_PID}" &>/dev/null; then
        msg_ok "CH341 USB device detected"
    else
        msg_warn "CH341 USB device not found — plug it in before starting the repeater"
    fi
fi
[[ "$INSTALL_CONSOLE" == "true" ]] && CONSOLE_SUMMARY="Yes"

# ── Confirmation ──────────────────────────────────────────────────────────
echo ""
echo -e "${BLD}Summary:${CL}"
echo "  CTID: ${CTID}  Host: ${CT_HOSTNAME}  RAM: ${CT_RAM}MB  Disk: ${CT_DISK}GB"
echo "  Cores: ${CT_CORES}  Storage: ${CT_STORAGE}  Bridge: ${CT_BRIDGE}  VLAN: ${VLAN_SUMMARY}"
echo "  Branch: ${BRANCH}"
echo "  CH341 host udev rule: ${CH341_UDEV_SUMMARY}"
echo "  openHop Console WebUI: ${CONSOLE_SUMMARY}"
echo "  Mode: privileged"
echo ""
read -p "  Proceed? [Y/n]: " -r
[[ "${REPLY:-Y}" =~ ^[Nn]$ ]] && { msg_warn "Aborted"; exit 0; }

# ── Download template ─────────────────────────────────────────────────────
echo ""
msg_info "Downloading Debian 13 template..."
TEMPLATE_FILE=$(pveam available -section system 2>/dev/null \
    | awk -v template="$CT_TEMPLATE" -v arch="$CT_ARCH" \
        '$2 ~ ("^" template "_") && $2 ~ ("_" arch "\\.tar\\.") {print $2}' \
    | sort -V \
    | tail -1)
[ -z "$TEMPLATE_FILE" ] && {
    msg_error "Debian 13 template for ${CT_ARCH} not found. Run: pveam update"
    exit 1
}

pveam list "$CT_TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE_FILE" || \
    pveam download "$CT_TEMPLATE_STORAGE" "$TEMPLATE_FILE"
msg_ok "Template ready"

# ── Create container ──────────────────────────────────────────────────────
msg_info "Creating LXC container ${CTID}..."
CT_NET0="name=eth0,bridge=${CT_BRIDGE},ip=dhcp"
if [[ -n "$CT_VLAN" ]]; then
    CT_NET0+=",tag=${CT_VLAN}"
fi
pct create "$CTID" "${CT_TEMPLATE_STORAGE}:vztmpl/${TEMPLATE_FILE}" \
    --hostname "$CT_HOSTNAME" \
    --memory "$CT_RAM" \
    --swap "$CT_SWAP" \
    --cores "$CT_CORES" \
    --rootfs "${CT_STORAGE}:${CT_DISK}" \
    --net0 "$CT_NET0" \
    --unprivileged 0 \
    --features nesting=1 \
    --onboot 1 \
    --start 0 \
    --password "$CT_PASSWORD" \
    --ostype debian
msg_ok "Container created"

# ── USB passthrough ───────────────────────────────────────────────────────
msg_info "Configuring USB passthrough..."
cat >> "/etc/pve/lxc/${CTID}.conf" <<'EOF'

# USB passthrough for openHop Repeater
lxc.cgroup2.devices.allow: c 189:* rwm
lxc.mount.entry: /dev/bus/usb dev/bus/usb none bind,optional,create=dir 0 0
EOF
msg_ok "USB passthrough configured"

# ── Host udev rule ────────────────────────────────────────────────────────
if [[ "$INSTALL_CH341_UDEV" == "true" ]]; then
    msg_info "Installing CH341 udev rule on host..."
    echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1a86", ATTR{idProduct}=="5512", MODE="0666"' \
        > /etc/udev/rules.d/99-ch341.rules
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=usb --action=change
    msg_ok "Host udev rule installed"
else
    msg_info "Skipping host-side CH341 udev rule"
fi

# ── Start container & wait for network ────────────────────────────────────
msg_info "Starting container..."
pct start "$CTID"
sleep 3
NETWORK_READY=false
for _ in $(seq 1 30); do
    if pct exec "$CTID" -- ping -c1 -W1 8.8.8.8 &>/dev/null; then
        NETWORK_READY=true
        break
    fi
    sleep 1
done
if [[ "$NETWORK_READY" != "true" ]]; then
    msg_error "Container network did not become ready after 30 seconds"
    exit 1
fi
msg_ok "Container running with network"

# ── Bootstrap: install git, clone repo ────────────────────────────────────
msg_info "Installing git inside container..."
pct exec "$CTID" -- bash -c "
    export DEBIAN_FRONTEND=noninteractive

    # Fix locale warnings
    apt-get update -qq
    apt-get install -y locales >/dev/null 2>&1
    sed -i 's/# en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen
    locale-gen >/dev/null 2>&1
    echo 'LANG=en_US.UTF-8' > /etc/default/locale

    apt-get install -y curl git whiptail >/dev/null 2>&1

    # Enable auto-login on console (no password prompt in Proxmox web console)
    mkdir -p /etc/systemd/system/container-getty@1.service.d
    cat > /etc/systemd/system/container-getty@1.service.d/override.conf <<'AUTOLOGIN'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud tty%I 115200,38400,9600 \$TERM
AUTOLOGIN
    systemctl daemon-reload

    # Login banner with system info
    cat > /etc/profile.d/pymc-motd.sh <<'MOTD'
#!/bin/sh
HOSTNAME=\$(hostname)
IP=\$(hostname -I | awk '{print \$1}')
OS=\$(. /etc/os-release && echo \"\$NAME\")
VER=\$(. /etc/os-release && echo \"\$VERSION_ID\")
echo \"\"
echo \"    openHop Repeater LXC Container\"
echo \"    🌐  GitHub: https://github.com/openhop-dev/openhop_repeater\"
echo \"\"
echo \"    🖥️   OS: \$OS - Version: \$VER\"
echo \"    🏠  Hostname: \$HOSTNAME\"
echo \"    💡  IP Address: \$IP\"
echo \"    📡  Dashboard: http://\$IP:8000\"
echo \"\"
echo \"    Management: cd /opt/openhop_repeater && bash manage.sh\"
echo \"    Update: update\"
echo \"\"
MOTD
    chmod +x /etc/profile.d/pymc-motd.sh
"
msg_ok "curl/git installed, locale fixed, console auto-login enabled"

msg_info "Cloning openhop-repeater (branch: ${BRANCH})..."
pct exec "$CTID" -- git clone --branch "$BRANCH" "$REPO" /root/openhop-repeater
msg_ok "Repository cloned"

msg_info "Installing LXC update command..."
pct exec "$CTID" -- bash -c "
    install -m 0755 /root/openhop-repeater/scripts/openhop-update /usr/local/bin/openhop-update
    ln -sfn /usr/local/bin/openhop-update /usr/local/bin/update
"
msg_ok "Update command installed"

# Pre-seed config with CH341 radio type and correct GPIO pins
pct exec "$CTID" -- bash -c "
    mkdir -p /etc/openhop_repeater
    if [ -f /root/openhop-repeater/config.yaml.example ]; then
        cp /root/openhop-repeater/config.yaml.example /etc/openhop_repeater/config.yaml
        # Set radio type to CH341
        sed -i 's/^radio_type: sx1262$/radio_type: sx1262_ch341/' /etc/openhop_repeater/config.yaml
        # Replace Pi BCM GPIO pins with CH341 GPIO pin numbers (0-7)
        sed -i 's/cs_pin: 21/cs_pin: 0/'       /etc/openhop_repeater/config.yaml
        sed -i 's/reset_pin: 18/reset_pin: 2/'  /etc/openhop_repeater/config.yaml
        sed -i 's/busy_pin: 20/busy_pin: 4/'    /etc/openhop_repeater/config.yaml
        sed -i 's/irq_pin: 16/irq_pin: 6/'      /etc/openhop_repeater/config.yaml
        sed -i 's/rxen_pin: -1/rxen_pin: 1/'     /etc/openhop_repeater/config.yaml
        # Enable TCXO and DIO2 RF switch for E22 module
        sed -i 's/use_dio3_tcxo: false/use_dio3_tcxo: true/' /etc/openhop_repeater/config.yaml
        sed -i 's/use_dio2_rf: false/use_dio2_rf: true/'     /etc/openhop_repeater/config.yaml
    fi
"

# ── Run manage.sh install ─────────────────────────────────────────────────
msg_info "Running manage.sh install (this will take several minutes)..."
echo ""
# Use lxc-attach with a pty so manage.sh gets an interactive terminal
lxc-attach -n "$CTID" -- bash -c "cd /root/openhop-repeater && TERM=xterm bash manage.sh install"
echo ""
msg_ok "manage.sh install completed"

# ── Optional openHop Console WebUI ─────────────────────────────────────────
if [[ "$INSTALL_CONSOLE" == "true" ]]; then
    msg_info "Cloning openHop Console distribution repository..."
    pct exec "$CTID" -- git clone --branch main --single-branch \
        "$CONSOLE_REPO" /root/pymc_console
    msg_ok "Console repository cloned to /root/pymc_console"

    msg_info "Installing optional openHop Console WebUI..."
    pct exec "$CTID" -- bash -c "
        set -e
        console_archive=\$(mktemp)
        trap 'rm -f \"\$console_archive\"' EXIT
        curl -fL '${CONSOLE_RELEASE_URL}' -o \"\$console_archive\"
        rm -rf /opt/pymc_console/web/html
        install -d -m 0755 -o repeater -g repeater /opt/pymc_console/web/html
        tar -xzf \"\$console_archive\" -C /opt/pymc_console/web/html
        test -f /opt/pymc_console/web/html/index.html
        chown -R repeater:repeater /opt/pymc_console
    "
    msg_ok "Console is installed but not enabled"
    msg_info "Complete the Repeater setup wizard before selecting openHop Console in Web Settings"
fi

# ── Container notes ───────────────────────────────────────────────────────
PROXMOX_NOTES_DIR="/var/lib/pve-manager/notes"
if [[ -d "$PROXMOX_NOTES_DIR" ]]; then
    cat > "${PROXMOX_NOTES_DIR}/${CTID}.html" <<'NOTES'
<div align="center">
  <a href="https://openhop.dev/" target="_blank" rel="noopener noreferrer">
    <img
      src="https://avatars.githubusercontent.com/u/295154237?s=100&v=4"
      width="100"
      height="100"
      alt="openHop Logo"
    />
  </a>

  <h2 style="font-size: 24px; margin: 20px 0;">
    openHop Repeater LXC
  </h2>

  <p style="margin: 16px 0;">
    <a
      href="https://buymeacoffee.com/rightup"
      target="_blank"
      rel="noopener noreferrer"
    >
      <img
        src="https://img.shields.io/badge/❤️-Support%20openHop-FF5E5B"
        alt="Support openHop"
      />
    </a>
  </p>

  <p style="margin: 12px 0;">
    <a
      href="https://docs.openhop.dev/projects/openhop-repeater/"
      target="_blank"
      rel="noopener noreferrer"
    >
      <img
        src="https://img.shields.io/badge/📡-Open%20Repeater%20Docs-00617f"
        alt="Open openHop Repeater documentation"
      />
    </a>
  </p>

  <span style="margin: 0 10px;">
    <i class="fa fa-globe fa-fw" style="color: #f5f5f5;"></i>
    <a
      href="https://openhop.dev/"
      target="_blank"
      rel="noopener noreferrer"
      style="text-decoration: none; color: #00617f;"
    >Website</a>
  </span>

  <span style="margin: 0 10px;">
    <i class="fa fa-github fa-fw" style="color: #f5f5f5;"></i>
    <a
      href="https://github.com/openhop-dev/openhop_repeater"
      target="_blank"
      rel="noopener noreferrer"
      style="text-decoration: none; color: #00617f;"
    >GitHub</a>
  </span>

  <span style="margin: 0 10px;">
    <i class="fa fa-comments fa-fw" style="color: #f5f5f5;"></i>
    <a
      href="https://discord.gg/3s8MMaSTzq"
      target="_blank"
      rel="noopener noreferrer"
      style="text-decoration: none; color: #00617f;"
    >Discord</a>
  </span>

  <span style="margin: 0 10px;">
    <i class="fa fa-comment-o fa-fw" style="color: #f5f5f5;"></i>
    <a
      href="https://github.com/openhop-dev/openhop_repeater/discussions"
      target="_blank"
      rel="noopener noreferrer"
      style="text-decoration: none; color: #00617f;"
    >Discussions</a>
  </span>

  <span style="margin: 0 10px;">
    <i class="fa fa-exclamation-circle fa-fw" style="color: #f5f5f5;"></i>
    <a
      href="https://github.com/openhop-dev/openhop_repeater/issues"
      target="_blank"
      rel="noopener noreferrer"
      style="text-decoration: none; color: #00617f;"
    >Issues</a>
  </span>
</div>
NOTES
fi

# ── Get container IP ──────────────────────────────────────────────────────
sleep 2
CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')

# ── Done ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLD}"
echo "═══════════════════════════════════════════════════════════════"
echo "        ✓ openHop Repeater Installation Complete!"
echo "═══════════════════════════════════════════════════════════════"
echo -e "${CL}"
echo -e "  Container:   ${GN}${CTID}${CL} (${CT_HOSTNAME})"
echo -e "  IP Address:  ${GN}${CT_IP:-unknown}${CL}"
echo -e "  Dashboard:   ${GN}http://${CT_IP:-<ip>}:8000${CL}"
echo ""
echo "  Next: open the dashboard and complete the setup wizard"
echo "  Management: pct enter ${CTID}, then: cd /opt/openhop_repeater && bash manage.sh"
echo "  Update: pct enter ${CTID}, then run: update"
if [[ "$INSTALL_CONSOLE" == "true" ]]; then
    echo "  Console: installed but not enabled; select it in Web Settings after setup"
fi
echo ""
echo "═══════════════════════════════════════════════════════════════"
