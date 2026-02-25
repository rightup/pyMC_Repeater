# pyMC_repeater

Repeater Daemon in Python using the `pymc_core` Lib.

---


My AI slop created driver to make a SX1302 based concentrator as commonly found in the now semi dormant "LoRa Helium" network hotspots.

These hotspots are commonly a PI4 (full or compute) a PiHat and a SX1302 or SX1301 concentrator. 

I only have access to a SX1302 based Sensecap  M1 at the moment. (happy to work with people to add in SX1301 support).
In addition each manufacture may have used different GPIO's to trigger the restart of the SX1302 concentrator needed to enable the device. 

A Major limitation of the SX1302 is that it CANNOT work on 62.5Khz (or narrow) settings. as the Lora decoders on chip simply do not accept it/have filters for it.
It will work with 125/250/500Khz.
I am continuing to try and make it work with 62.5Khz. But have had no success yet. 

I am also attempting to make it listen to a second frequency set. But this will be limited to 125Khz as a primary, and 125/250/500Khz as a secondary. this is a hardware limitation.

What’s working.
Packet repeating,   
Adverts,   
Dropping CRC fail packets,   
Noise floor based on spectral analysis (on chip sx1261)   



I have altered the manage.sh script to allow for the SX1302,
And when selected during install it will build the SX1302_HAL library from Semtech as required. 


For NSW Wide settings please use the following. (edit /etc/pymc_repeater/config.yaml)
  frequency: 915800000   
  tx_power: 26   
  bandwidth: 250000   
  spreading_factor: 11   
  coding_rate: 5   
  preamble_length: 16   
  sync_word: 18   

---


  Clean & Deploy — pyMC Repeater (dev_merge)

### 1. Clean existing install

  Run manage.sh and select Uninstall, or manually:

  sudo systemctl stop pymc-repeater
  sudo systemctl disable pymc-repeater
  sudo rm -f /etc/systemd/system/pymc-repeater.service
  sudo systemctl daemon-reload
  sudo rm -rf /opt/pymc_repeater /etc/pymc_repeater /var/log/pymc_repeater /var/lib/pymc_repeater
  sudo userdel repeater 2>/dev/null || true

  Also remove any previous source clone:
  rm -rf ~/pyMC_Repeater* ~/rightup_pymc_rep_dev 2>/dev/null || true

  
###  2. Clone the branch

  git clone -b dev_merge https://github.com/l34rn3d/pyMC_Repeater_WM1302.git ~/pymc_dev
  cd ~/pymc_dev

  
###  3. Install

  sudo ./manage.sh

  Select Install. The installer will:
  - Install system dependencies (gcc, make, swig, etc.)
  - Copy files to /opt/pymc_repeater/
  - Clone and build sx1302_hal from source for your architecture
  - Create reset_lgw.sh (pinctrl-based GPIO reset)
  - Install Python packages
  - Start the service

  
###  4. Complete the web wizard

  Navigate to http://<device-ip>:8000 and complete setup. Select SX1302 as your hardware.

  
###  5. Fix radio_type in config (known gap — web wizard does not write this yet)

sudo tee -a /etc/pymc_repeater/config.yaml << 'EOF'

radio_type: "sx1302"

sx1302:
  com_path: "/dev/spidev0.0"
  sx1261_spi_path: "/dev/spidev0.1"
EOF

  
###  6. Restart and verify

  sudo systemctl restart pymc-repeater
  sudo journalctl -u pymc-repeater -f

  Expected: concentrator starts, TX adverts appear within ~30 seconds.

  
  Note: Step 5 is a manual workaround. The web wizard not writing radio_type is documented in the merge proposal as Gap 1 — the upstream web UI needs fixing before this
  becomes a clean install-only flow.




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

The following hardware is currently supported out-of-the-box:

Waveshare LoRaWAN/GNSS HAT (SPI Version Only)

    Hardware: Waveshare SX1262 LoRa HAT (SPI interface - UART version not supported)
    Platform: Raspberry Pi (or compatible single-board computer)
    Frequency: 868MHz (EU) or 915MHz (US)
    TX Power: Up to 22dBm
    SPI Bus: SPI0
    GPIO Pins: CS=21, Reset=18, Busy=20, IRQ=16
    Note: Only the SPI version is supported. The UART version will not work.

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




