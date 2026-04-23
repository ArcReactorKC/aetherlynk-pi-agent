#!/usr/bin/env python3
"""AetherLynk Pi edge agent — reads Modbus registers and publishes to MQTT."""

import configparser
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from pymodbus.client import ModbusTcpClient

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
CONF_DIR = Path("/etc/aetherlynk")
CONF_FILE = CONF_DIR / "device.conf"
LOG_DIR = Path("/var/log/aetherlynk")
LOG_FILE = LOG_DIR / "agent.log"
CPUINFO = Path("/proc/cpuinfo")

API_BASE = "https://api.aetherlynk.com"
CONFIG_POLL_INTERVAL = 60   # seconds between config refreshes
CLAIM_POLL_INTERVAL = 30    # seconds between unclaimed-device polls

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stdout_handler)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CPU serial → device key
# ---------------------------------------------------------------------------
def read_cpu_serial() -> str:
    text = CPUINFO.read_text()
    for line in text.splitlines():
        if line.strip().lower().startswith("serial"):
            serial = line.split(":")[-1].strip()
            if serial and serial != "0000000000000000":
                return serial
    raise RuntimeError("Could not read CPU serial from /proc/cpuinfo")


def derive_device_key(serial: str) -> str:
    hex_only = re.sub(r"[^0-9a-fA-F]", "", serial)
    last8 = hex_only[-8:].upper()
    return f"AL-{last8[:4]}-{last8[4:]}"

# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------
def load_conf() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONF_FILE.exists():
        cfg.read(CONF_FILE)
    return cfg


def save_conf(cfg: configparser.ConfigParser):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONF_FILE, "w") as fh:
        cfg.write(fh)

# ---------------------------------------------------------------------------
# ASCII box for device key
# ---------------------------------------------------------------------------
def print_device_key_box(device_key: str):
    line1 = "  AetherLynk Device Key  "
    line2 = f"  {device_key}  "
    width = max(len(line1), len(line2)) + 2
    border = "+" + "-" * width + "+"
    msg = (
        "\n"
        f"{border}\n"
        f"|{line1.center(width)}|\n"
        f"|{line2.center(width)}|\n"
        f"{border}\n"
        "\n"
        "Write this key on the device label and claim it at portal.aetherlynk.com\n"
    )
    print(msg)
    log.info("Device key: %s", device_key)

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def pre_register(cpu_serial: str, device_key: str):
    url = f"{API_BASE}/devices/pre-register"
    try:
        resp = requests.post(url, json={"cpu_serial": cpu_serial, "device_key": device_key}, timeout=15)
        resp.raise_for_status()
        log.info("Pre-registration successful")
    except requests.RequestException as exc:
        log.warning("Pre-registration request failed (will retry later): %s", exc)


def fetch_device_config(device_key: str) -> dict | None:
    url = f"{API_BASE}/devices/{device_key}/config"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.warning("Config fetch failed: %s", exc)
        return None

# ---------------------------------------------------------------------------
# Claim flow
# ---------------------------------------------------------------------------
def wait_for_claim(device_key: str) -> dict:
    """Block until the device is claimed; returns the config payload."""
    print_device_key_box(device_key)
    log.info("Waiting for device to be claimed (polling every %ds)…", CLAIM_POLL_INTERVAL)
    while True:
        cfg_data = fetch_device_config(device_key)
        if cfg_data and cfg_data.get("claimed"):
            log.info("Device claimed!")
            return cfg_data
        time.sleep(CLAIM_POLL_INTERVAL)

# ---------------------------------------------------------------------------
# Modbus polling
# ---------------------------------------------------------------------------
class RegisterPoller:
    def __init__(self, device_key: str, mqtt_client: mqtt.Client):
        self.device_key = device_key
        self.mqtt = mqtt_client
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._stopped = False

    def update_registers(self, registers: list[dict]):
        with self._lock:
            if self._stopped:
                return
            current_keys = set(self._timers.keys())
            new_keys = {str(r["register_address"]) for r in registers if r.get("enabled")}

            for key in current_keys - new_keys:
                self._timers[key].cancel()
                del self._timers[key]

            for reg in registers:
                if not reg.get("enabled"):
                    continue
                key = str(reg["register_address"])
                if key not in self._timers:
                    self._schedule(reg)

    def _schedule(self, reg: dict):
        key = str(reg["register_address"])
        interval = reg.get("read_interval_seconds", 10)
        timer = threading.Timer(interval, self._poll_and_reschedule, args=(reg,))
        timer.daemon = True
        self._timers[key] = timer
        timer.start()

    def _poll_and_reschedule(self, reg: dict):
        if self._stopped:
            return
        self._do_poll(reg)
        with self._lock:
            if not self._stopped:
                self._schedule(reg)

    def _do_poll(self, reg: dict):
        host = reg.get("modbus_host", "localhost")
        port = reg.get("modbus_port", 502)
        address = reg["register_address"]
        count = reg.get("register_count", 1)
        friendly = reg.get("register_friendly_name", str(address))
        unit = reg.get("unit", "")

        client = ModbusTcpClient(host, port=port)
        try:
            if not client.connect():
                log.warning("Modbus connect failed for %s:%s", host, port)
                return
            result = client.read_holding_registers(address, count=count, slave=reg.get("slave_id", 1))
            if result.isError():
                log.warning("Modbus read error on register %s: %s", address, result)
                return
            value = result.registers[0] if count == 1 else result.registers

            payload = json.dumps({
                "device_key": self.device_key,
                "register_address": address,
                "register_friendly_name": friendly,
                "value": value,
                "unit": unit,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            })
            topic = f"aetherlynk/{self.device_key}/data"
            self.mqtt.publish(topic, payload, qos=1)
            log.debug("Published %s → %s", friendly, value)
        except Exception as exc:
            log.warning("Error polling register %s: %s", address, exc)
        finally:
            client.close()

    def stop(self):
        with self._lock:
            self._stopped = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------
def build_mqtt_client(device_key: str, bearer_token: str, host: str, port: int) -> mqtt.Client:
    client = mqtt.Client(client_id=device_key, protocol=mqtt.MQTTv311)
    client.username_pw_set(username=device_key, password=bearer_token)
    client.tls_set()
    client.on_connect = lambda c, u, f, rc: log.info("MQTT connected (rc=%s)", rc)
    client.on_disconnect = lambda c, u, rc: log.warning("MQTT disconnected (rc=%s)", rc)
    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    setup_logging()
    log.info("AetherLynk agent starting")

    shutdown_event = threading.Event()

    def handle_signal(signum, _frame):
        log.info("Signal %s received — shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # --- Identify device ---
    cfg = load_conf()

    if cfg.has_option("device", "cpu_serial"):
        cpu_serial = cfg["device"]["cpu_serial"]
        device_key = cfg["device"]["device_key"]
        log.info("Loaded device identity: %s", device_key)
    else:
        cpu_serial = read_cpu_serial()
        device_key = derive_device_key(cpu_serial)
        cfg["device"] = {"cpu_serial": cpu_serial, "device_key": device_key}
        save_conf(cfg)
        log.info("Derived device key: %s", device_key)
        pre_register(cpu_serial, device_key)

    # --- Claim flow ---
    if not cfg.has_option("auth", "device_bearer_token"):
        config_data = wait_for_claim(device_key)
        bearer_token = config_data["device_bearer_token"]
        if "auth" not in cfg:
            cfg["auth"] = {}
        cfg["auth"]["device_bearer_token"] = bearer_token
        save_conf(cfg)
    else:
        bearer_token = cfg["auth"]["device_bearer_token"]
        config_data = fetch_device_config(device_key) or {}

    # --- MQTT ---
    mqtt_host = config_data.get("mqtt_host", "mqtt.aetherlynk.com")
    mqtt_port = int(config_data.get("mqtt_port", 1883))
    mqtt_client = build_mqtt_client(device_key, bearer_token, mqtt_host, mqtt_port)

    # --- Register poller ---
    poller = RegisterPoller(device_key, mqtt_client)
    registers = config_data.get("modbus_registers", [])
    poller.update_registers(registers)

    # --- Config refresh loop ---
    last_refresh = time.monotonic()
    log.info("Agent running — config refresh every %ds", CONFIG_POLL_INTERVAL)

    while not shutdown_event.is_set():
        now = time.monotonic()
        if now - last_refresh >= CONFIG_POLL_INTERVAL:
            fresh = fetch_device_config(device_key)
            if fresh:
                new_token = fresh.get("device_bearer_token")
                if new_token and new_token != bearer_token:
                    bearer_token = new_token
                    cfg["auth"]["device_bearer_token"] = bearer_token
                    save_conf(cfg)
                    mqtt_client.username_pw_set(username=device_key, password=bearer_token)
                new_host = fresh.get("mqtt_host", mqtt_host)
                new_port = int(fresh.get("mqtt_port", mqtt_port))
                if new_host != mqtt_host or new_port != mqtt_port:
                    mqtt_host, mqtt_port = new_host, new_port
                    mqtt_client.disconnect()
                    mqtt_client.connect(mqtt_host, mqtt_port, keepalive=60)
                poller.update_registers(fresh.get("modbus_registers", []))
                log.info("Config refreshed")
            last_refresh = now
        shutdown_event.wait(timeout=1)

    log.info("Shutting down")
    poller.stop()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    log.info("Agent stopped")


if __name__ == "__main__":
    main()
