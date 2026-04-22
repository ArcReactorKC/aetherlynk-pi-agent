# AetherLynk Pi Agent

A lightweight edge agent for Raspberry Pi that reads industrial sensor data over Modbus TCP and publishes it to the AetherLynk cloud platform via MQTT.

---

## Setup Instructions

### 1. Flash Raspberry Pi OS Lite

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to flash **Raspberry Pi OS Lite** (64-bit recommended) to your SD card.

In the Imager's **Advanced Options** (gear icon):
- Set a hostname (e.g. `aetherlynk-01`)
- Enable **SSH** and set a password or public key
- Configure **Wi-Fi** SSID and password if not using Ethernet

### 2. Boot and SSH into the Pi

Insert the SD card, power on the Pi, and wait ~60 seconds for first boot. Then connect:

```bash
ssh pi@<hostname>.local
# or
ssh pi@<ip-address>
```

### 3. Install the Agent

Run the one-line installer as root:

```bash
curl -sSL https://raw.githubusercontent.com/ArcReactorKC/aetherlynk-pi-agent/main/install.sh | sudo bash
```

The installer will:
- Install Python dependencies
- Download the latest agent from GitHub
- Create a systemd service that starts on boot
- Tail the log for 15 seconds

### 4. Record the Device Key

After the agent starts, a **Device Key** will print in the console:

```
+----------------------------+
|   AetherLynk Device Key    |
|      AL-A1B2-C3D4          |
+----------------------------+
```

**Write this key on a label and stick it to the physical device.**

If you miss it, retrieve it anytime with:

```bash
sudo journalctl -u aetherlynk --no-pager | grep "Device Key"
```

### 5. Claim the Device

1. Log in to [portal.aetherlynk.com](https://portal.aetherlynk.com)
2. Navigate to **Devices → Add Device**
3. Enter the Device Key printed above
4. The Pi will detect the claim within 30 seconds and begin operating

### 6. Configure Modbus Registers

In the portal, navigate to your device and add Modbus registers:

- **Register Address** — the holding register number to read
- **Friendly Name** — human-readable label (e.g. "Pump Pressure")
- **Unit** — engineering unit (e.g. `PSI`, `°F`, `A`)
- **Read Interval** — how often to poll (seconds)
- **Modbus Host / Port** — IP address and port of your Modbus device
- **Slave ID** — Modbus unit ID (default `1`)

Data will begin flowing to the platform within 60 seconds of saving the configuration.

---

## Updating

Re-run the installer at any time to pull the latest version:

```bash
curl -sSL https://raw.githubusercontent.com/ArcReactorKC/aetherlynk-pi-agent/main/install.sh | sudo bash
```

The installer re-downloads all files and restarts the service. Device identity and credentials in `/etc/aetherlynk/device.conf` are preserved.

---

## File Locations

| Path | Purpose |
|------|---------|
| `/opt/aetherlynk/aetherlynk_agent.py` | Agent script |
| `/opt/aetherlynk/venv/` | Python virtual environment |
| `/etc/aetherlynk/device.conf` | Device identity and bearer token |
| `/var/log/aetherlynk/agent.log` | Agent log (rotated at 5 MB, 5 copies kept) |
| `/etc/systemd/system/aetherlynk.service` | Systemd unit file |

---

## Service Management

```bash
sudo systemctl status aetherlynk     # check status
sudo systemctl restart aetherlynk    # restart
sudo journalctl -u aetherlynk -f     # live log stream
```

---

## MQTT Data Format

Each register reading is published to topic `aetherlynk/{device_key}/data`:

```json
{
  "device_key": "AL-A1B2-C3D4",
  "register_address": 100,
  "register_friendly_name": "Pump Pressure",
  "value": 142,
  "unit": "PSI",
  "timestamp_utc": "2026-04-22T14:30:00.123456+00:00"
}
```
