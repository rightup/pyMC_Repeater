# pyMC_repeater

Repeater Daemon in Python using the `pymc_core` Lib.

---

I started **pyMC_core** as a way to really get under the skin of **MeshCore** — to see how it ticked and why it behaved the way it did.
After a few late nights of tinkering, testing, and head-scratching, I shared what I’d learned with the community.
The response was honestly overwhelming — loads of encouragement, great feedback, and a few people asking if I could spin it into a lightweight **repeater daemon** that would run happily on low-power, Pi-class hardware.

That challenge shaped much of what followed:
- I went with a lightweight HTTP server (**CherryPy**) instead of a full-fat framework.
- I stuck with simple polling over WebSockets — it’s more reliable, has fewer dependencies, and is far less resource hungry.
- I kept the architecture focused on being **clear, modular, and hackable** rather than chasing performance numbers.

There’s still plenty of room for this project to grow and improve — but you’ve got to start somewhere!
My hope is that **pyMC_repeater** serves as a solid, approachable foundation that others can learn from, build on, and maybe even have a bit of fun with along the way.

> **I’d love to see these repeaters out in the wild — actually running in real networks and production setups.**
> My own testing so far has been in a very synthetic environment with little to no other users in my area,
> so feedback from real-world deployments would be incredibly valuable!

---

## Overview

The repeater daemon runs continuously as a background process, forwarding LoRa packets using `pymc_core`'s Dispatcher and packet routing.

---

## Supported Hardware (Out of the Box)

The repeater supports two radio backends:

- **SX1262 (SPI)** — Direct connection to LoRa modules (HATs, etc.) as listed below.
- **KISS modem** — Serial TNC using the KISS protocol. Set `radio_type: kiss` in config and configure `kiss.port` and `kiss.baud_rate`.

> [!CAUTION]
> ## Compatibility
>
> ### Supported Radio Interfaces
>
> | Interface | Supported |
> |------------|------------|
> | Native SPI radio SX1262 | ✅ Yes |
> | USB–SPI bridge (CH341F) | ✅ Yes |
> | UART-based HATs | ❌ No |
> | SX1302 concentrator boards | ❌ No |
> | SX1303 concentrator boards | ❌ No |
>
> This project supports **single-radio SPI transceivers only**, either:
> - Connected directly via SPI
> - Connected via a CH341F USB–SPI adapter
> - Connected using hardware that supports Meshcore Kiss Modem firmware

The following hardware is currently supported out-of-the-box:

HackerGadgets uConsole

    Hardware: uConsole RTL-SDR/LoRa/GPS/RTC/USB Hub
    Platform: Clockwork uConsole (Raspberry Pi CM4/CM5)
    Frequency: 433/915MHz (configurable)
    TX Power: Up to 22dBm
    SPI Bus: SPI1
    GPIO Pins: CS=-1, Reset=25, Busy=24, IRQ=26
    Additional Setup: Requires SPI1 overlay and GPS/RTC configuration (see uConsole setup guide)

Frequency Labs meshadv-mini

    Hardware: FrequencyLabs meshadv-mini Hat
    Platform: Raspberry Pi (or compatible single-board computer)
    Frequency: 868MHz (EU) or 915MHz (US)
    TX Power: Up to 22dBm
    SPI Bus: SPI0
    GPIO Pins: CS=8, Reset=24, Busy=20, IRQ=16

Frequency Labs meshadv

    Hardware: FrequencyLabs meshadv-mini Hat
    Platform: Raspberry Pi (or compatible single-board computer)
    Frequency: 868MHz (EU) or 915MHz (US)
    TX Power: Up to 22dBm
    SPI Bus: SPI0
    GPIO Pins: CS=21, Reset=18, Busy=20, IRQ=16, TXEN=13, RXEN=12, use_dio3_tcxo=True

HT-RA62 module
    
    Hardware: Heltec HT-RA62 LoRa module
    Platform: Raspberry Pi (or compatible single-board computer)
    Frequency: 868MHz (EU) or 915MHz (US)
    TX Power: Up to 22dBm
    SPI Bus: SPI0
    GPIO Pins: CS=21, Reset=18, Busy=20, IRQ=16, use_dio3_tcxo=True, use_dio2_rf=True

Zindello Industries UltraPeater 

    Hardware: EBYTE E22/P 1W Module
    Platform: Luckfox Pico Ultra/W  (NOT A PI DEVICE)
    Frequency: 868MHz (EU) or 915Mhz (US/AU)
    Tx Power: Up to 30dBm
    SPI Bus: SPI0
    GPIO Pins: CS=16, Reset=22, Busy=11, IRQ=10, TXEN=20 , RXEN=21 (E22 Only), EN=21 (E22P Only), TXLED=9, RXLED=1, use_dio2_rf=False, use_dio3_tcxo=True, use_gpiod_backend=True, gpio_chip=1

Waveshare LoRaWAN/GNSS HAT (SPI Version Only)

    NO LONGER RECOMMENDED
    Note: May experience issues on "Narrow" (62.5KHz) settings due to a lack of TCXO
    Hardware: Waveshare SX1262 LoRa HAT (SPI interface - UART version not supported)
    Platform: Raspberry Pi (or compatible single-board computer)
    Frequency: 868MHz (EU) or 915MHz (US)
    TX Power: Up to 22dBm
    SPI Bus: SPI0
    GPIO Pins: CS=21, Reset=18, Busy=20, IRQ=16
    Note: Only the SPI version is supported. The UART version will not work.

...

## Screenshots

### Dashboard
![Dashboard](docs/dashboard.png)
*Real-time monitoring dashboard showing packet statistics, neighbor discovery, and system status*

### Statistics
![Statistics](docs/stats.png)
*statistics and performance metrics*

## Installation

Before You Begin

Make sure SPI is switched on using raspi-config:

```bash 
sudo raspi-config
```

1. Go to Interface Options
2. Select SPI
3. Choose Enable
4. Reboot when prompted:

```bash
sudo reboot
```

After reboot, you can confirm SPI is active:
```bash 
ls /dev/spi*
```

You should see something like:
```bash 
/dev/spidev0.0  /dev/spidev0.1
```

**Install Git (if not already installed):**
```bash
sudo apt update
sudo apt install git -y
```

**Clone the Repository:**
```bash
git clone https://github.com/rightup/pyMC_Repeater.git
cd pyMC_Repeater
```

**Quick Install:**
```bash
sudo ./manage.sh
```

This script will:
- Create a dedicated `repeater` service user with hardware access
- Install files to `/opt/pymc_repeater`
- Create configuration directory at `/etc/pymc_repeater`
- Setup log directory at `/var/log/pymc_repeater`
- **Launch interactive radio & hardware configuration wizard**
- Install and enable systemd service

**After Installation:**
```bash
# View live logs
sudo journalctl -u pymc-repeater -f

# Access web dashboard
http://<repeater-ip>:8000
```

**Development Install:**
```bash
pip install -e .
```

## Configuration

The configuration file is created and configured during installation at:
```
/etc/pymc_repeater/config.yaml
```

### Optional pyMC_Glass integration
The repeater now supports an additive `glass` config section for central control-plane integration.
When enabled, it sends periodic `/inform` payloads to pyMC_Glass, receives queued commands, and reports command results on the next inform cycle.

Minimal example:
```yaml
glass:
  enabled: true
  base_url: "http://localhost:8080"
  inform_interval_seconds: 30
```

To reconfigure radio and hardware settings after installation, run:
```bash
sudo bash setup-radio-config.sh /etc/pymc_repeater
# or
sudo ./manage.sh
# then restart the service
sudo systemctl restart pymc-repeater

```
## Upgrading

To upgrade an existing installation to the latest version:

```bash
# Navigate to your pyMC_Repeater directory
cd pyMC_Repeater

# Run the upgrade script
sudo ./manage.sh
```

The upgrade script will:
- Pull the latest code from the main branch
- Update all application files
- Upgrade Python dependencies if needed
- Restart the service automatically
- Preserve your existing configuration

---

## Installing on Proxmox (LXC Container)

pyMC Repeater can run inside a Proxmox LXC container using a **CH341 USB-to-SPI adapter** for radio communication. This is ideal for headless, always-on deployments without dedicating a full Raspberry Pi.

### Requirements

- **Proxmox VE 7.x or 8.x** host
- **CH341 USB-to-SPI adapter** (VID `1a86`, PID `5512`) connected to the Proxmox host
- **SX1262-based LoRa module** (e.g. Ebyte E22-900M30S) wired to the CH341 adapter
- Internet connectivity for the container

### One-Line Install

Run this on the **Proxmox host** (not inside a container):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/rightup/pyMC_Repeater/feat/newRadios/scripts/proxmox-install.sh)"
```

> **Tip:** Replace `feat/newRadios` in the URL with whichever branch you want to install.

The installer will interactively prompt you for container settings (hostname, RAM, disk, bridge, etc.) and then:

1. Download a Debian 12 LXC template
2. Create a **privileged** container with USB passthrough
3. Install a host-side udev rule for the CH341 device
4. Clone the repository and pre-seed the config with CH341 GPIO pin mappings
5. Run `manage.sh install` inside the container
6. Display the dashboard URL when finished

### Default Container Settings

| Setting   | Default         |
|-----------|-----------------|
| Hostname  | `pymc-repeater` |
| RAM       | 1024 MB         |
| Disk      | 4 GB            |
| CPU cores | 2               |
| Bridge    | `vmbr0`         |
| Storage   | `local-lvm`     |
| Password  | `pymc`          |

### After Installation

```bash
# Enter the container
pct enter <CTID>

# View service logs
journalctl -u pymc-repeater -f

# Access web dashboard
http://<container-ip>:8000

# Manage the repeater
cd /opt/pymc_repeater && bash manage.sh
```

### CH341 GPIO Pin Mapping

The installer pre-configures the CH341 GPIO pins for an E22 module. These differ from the Raspberry Pi BCM pin numbers:

| Function | CH341 GPIO | Pi BCM (default) |
|----------|-----------|-------------------|
| CS       | 0         | 21                |
| RXEN     | 1         | -1                |
| Reset    | 2         | 18                |
| Busy     | 4         | 20                |
| IRQ      | 6         | 16                |

The installer also enables `use_dio3_tcxo` and `use_dio2_rf` for E22 modules.

### Troubleshooting (Proxmox)

- **USB device not found**: Make sure the CH341 is plugged into the Proxmox host and shows up with `lsusb -d 1a86:5512`
- **Permission denied on USB**: The installer creates a host udev rule (`/etc/udev/rules.d/99-ch341.rules`). Run `udevadm trigger` on the host if needed
- **Container can't see USB**: Verify USB passthrough lines exist in `/etc/pve/lxc/<CTID>.conf`:
  ```
  lxc.cgroup2.devices.allow: c 189:* rwm
  lxc.mount.entry: /dev/bus/usb dev/bus/usb none bind,optional,create=dir 0 0
  ```
- **NoBackendError (libusb)**: The installer installs `libusb-1.0-0` automatically. If you see this error, run `apt-get install libusb-1.0-0` inside the container




## Uninstallation

```bash
sudo ./manage.sh
```

This script will:
- Stop and disable the systemd service
- Remove the installation directory
- Optionally remove configuration, logs, and user data
- Optionally remove the service user account

The script will prompt you for each optional removal step.


## Docker Compose

You can now run pyMC Repeater from within a [Docker Container](https://www.docker.com/). Checkout the example [Docker Compose](./docker-compose.yml) file before you get started. It will need some configuration changes based on what hardware you're using (USB vs SPI). Look at the commented out lines to see which hardware requires what lines and only enable what you need.

Here is what you'll need to do in order to get the container running:

1. Copy the `config.yaml.example` to `config.yaml`

```bash
cp ./config.yaml.example ./config.yaml
```

2. Run the configuration script and follow the prompts.

```bash
sudo bash ./setup-radio-config.sh
```

3. Modify the `config.yaml` file with a unique web UI password. This allows you to bypass the `/setup` page when logging for the first time. You can find the value under `repeater.security.admin_password`. Change to _anything_ besides the default of `admin123`.

4. Configure the [docker compose](./docker-compose.yml) to your specific hardware and file paths. Be sure to comment-out or delete lines that aren't required for your hardware. Please note that your hardware devices might be at a different path than those listed in the docker compose file.

5. Build and start the container.

```bash
docker compose up -d --force-recreate --build
```

## Roadmap / Planned Features

- [ ] **Public Map Integration** - Submit repeater location and details to public map for discovery
- [ ] **Remote Administration over LoRa** - Manage repeater configuration remotely via LoRa mesh
- [ ] **Trace Request Handling** - Respond to trace/diagnostic requests from mesh network


## Contributing

I welcome contributions! To contribute to pyMC_repeater:

1. **Fork the repository** and clone your fork
2. **Create a feature branch** from the `dev` branch:
   ```bash
   git checkout -b feature/your-feature-name dev
   ```
3. **Make your changes** and test with **real** hardware
4. **Commit with clear messages**:
   ```bash
   git commit -m "feat: description of changes"
   ```
5. **Push to your fork** and submit a **Pull Request to the `dev` branch**
   - Include a clear description of the changes
   - Reference any related issues

### Development Setup

```bash
# Install in development mode with dev tools (black, pytest, isort, mypy, etc)
pip install -e ".[dev]"

# Setup pre-commit hooks for code quality
pip install pre-commit
pre-commit install

# Manually run pre-commit checks on all files
pre-commit run --all-files
```

**Note:** Hardware support (LoRa radio drivers) is included in the base installation automatically via `pymc_core[hardware]`.

Pre-commit hooks will automatically:
- Format code with Black
- Sort imports with isort
- Lint with flake8
- Fix trailing whitespace and other file issues

## Support

- [Core Lib Documentation](https://rightup.github.io/pyMC_core/)
- [Meshcore Discord](https://discordapp.com/channels/1343693475589263471/1431414286974189639)




## Disclaimer

**⚠️ Important Notice**

This software has been tested on actual hardware, but is provided "as is" without warranty of any kind, express or implied. While care has been taken to ensure stability and reliability, I make no guarantees about the software's performance, compatibility, or suitability for any particular purpose.

**By using this software, you acknowledge and agree that:**
- You use this software entirely at your own risk
- I hold no responsibility for any damage to hardware, data loss, or system failures
- You are responsible for ensuring compliance with local radio regulations and licensing requirements
- No support or warranty is guaranteed, though community assistance is available

This software is intended for educational and experimental purposes. Always test in a controlled environment before deploying to production.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
