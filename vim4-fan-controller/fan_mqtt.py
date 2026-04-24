#!/usr/bin/env python3
"""VIM4 Fan Controller <-> MQTT bridge.

Reads /sys/class/fan/* through the container's read-only bind-mount
(reads work fine through that). Writes go via SSH to the HAOS host on
port 22222, which is the one sanctioned way to break out of the
container's mount-namespace jail. Supervisor's /sys bind-mount into
add-on containers is locked read-only and can't be remounted even with
full_access + CAP_SYS_ADMIN.

Entities, when SSH control is wired up:

  - sensor.vim4_cpu_temp
  - select.vim4_fan_mode    (auto | manual)
  - select.vim4_fan_level   (off | low | mid | high)
  - switch.vim4_fan_enable  (master gate on /sys/class/fan/enable)
  - fan.vim4_fan            (HA fan platform: on/off + preset)

When SSH isn't set up yet the add-on publishes only read-only sensors
so HA keeps getting temperature data while you finish the setup.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

FAN_SYSFS = Path("/sys/class/fan")
THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0")

LEVEL_NAMES: Dict[int, str] = {0: "off", 1: "low", 2: "mid", 3: "high"}
NAME_TO_LEVEL: Dict[str, int] = {v: k for k, v in LEVEL_NAMES.items()}
MODE_NAMES: Dict[int, str] = {0: "auto", 1: "manual"}
NAME_TO_MODE: Dict[str, int] = {v: k for k, v in MODE_NAMES.items()}

DEVICE_INFO = {
    "identifiers": ["vim4_fan_controller"],
    "name": "Khadas VIM4",
    "manufacturer": "Khadas",
    "model": "VIM4 (A311D2)",
    "sw_version": "0.4.0",
}

log = logging.getLogger("vim4_fan")


# ---------------------------------------------------------------------------
# SSH writer
# ---------------------------------------------------------------------------


class SshWriter:
    """Execute `echo V > /sys/class/fan/X` on the HAOS host over SSH.

    Uses OpenSSH ControlMaster so the second and subsequent writes reuse
    one TCP/TLS connection — keeps latency in the tens of milliseconds.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        key_path: str,
        control_path: str = "/tmp/vim4_fan_ssh.ctl",
        known_hosts_path: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path
        self.control_path = control_path
        self.known_hosts_path = known_hosts_path or f"{key_path}.known_hosts"

    def _ssh_cmd(self, remote_cmd: str) -> list:
        return [
            "ssh",
            "-i", self.key_path,
            "-p", str(self.port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts_path}",
            "-o", "ConnectTimeout=5",
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60s",
            f"{self.user}@{self.host}",
            remote_cmd,
        ]

    def run(self, remote_cmd: str, timeout: float = 10.0) -> None:
        cmd = self._ssh_cmd(remote_cmd)
        log.debug("SSH exec: %s", " ".join(shlex.quote(c) for c in cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise OSError(
                f"SSH {remote_cmd!r} failed (rc={proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )

    def write_fan_node(self, name: str, value: int) -> None:
        # name is a constant-ish identifier and value is int; no interpolation risk
        self.run(f"echo {int(value)} > /sys/class/fan/{name}")

    def probe(self) -> bool:
        """Return True if SSH works and /sys/class/fan/enable is writable."""
        try:
            self.run("test -w /sys/class/fan/enable", timeout=10.0)
            return True
        except Exception as exc:
            log.warning("SSH probe failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Sysfs reads (always via /sys in the container — ro bind-mount reads are fine)
# ---------------------------------------------------------------------------


def read_sysfs(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError as exc:
        log.debug("read %s failed: %s", path, exc)
        return None


def read_int(path: Path) -> Optional[int]:
    raw = read_sysfs(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        log.debug("non-integer at %s: %r", path, raw)
        return None


# ---------------------------------------------------------------------------
# MQTT bridge
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self, args: argparse.Namespace, writer: Optional[SshWriter]) -> None:
        self.args = args
        self.writer = writer  # None => monitor-only mode
        self.base = args.base_topic.rstrip("/")
        self.disco = args.discovery_prefix.rstrip("/")
        self.stop_event = threading.Event()

        client_id = f"vim4_fan_{os.getpid()}"
        self.client = mqtt.Client(client_id=client_id, clean_session=True)
        if args.mqtt_user:
            self.client.username_pw_set(args.mqtt_user, args.mqtt_pass or None)

        self.availability_topic = f"{self.base}/availability"
        self.client.will_set(self.availability_topic, "offline", retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    @property
    def supports_control(self) -> bool:
        return self.writer is not None

    # ---- Topic helpers --------------------------------------------------
    @property
    def temp_state_topic(self) -> str:          return f"{self.base}/temp"
    @property
    def mode_state_topic(self) -> str:          return f"{self.base}/mode"
    @property
    def mode_command_topic(self) -> str:        return f"{self.base}/mode/set"
    @property
    def level_state_topic(self) -> str:         return f"{self.base}/level"
    @property
    def level_command_topic(self) -> str:       return f"{self.base}/level/set"
    @property
    def enable_state_topic(self) -> str:        return f"{self.base}/enable"
    @property
    def enable_command_topic(self) -> str:      return f"{self.base}/enable/set"
    @property
    def fan_state_topic(self) -> str:           return f"{self.base}/fan/state"
    @property
    def fan_command_topic(self) -> str:         return f"{self.base}/fan/state/set"
    @property
    def fan_preset_state_topic(self) -> str:    return f"{self.base}/fan/preset"
    @property
    def fan_preset_command_topic(self) -> str:  return f"{self.base}/fan/preset/set"

    # ---- Lifecycle ------------------------------------------------------
    def run(self) -> None:
        log.info(
            "Connecting to MQTT broker %s:%s (user=%s)",
            self.args.mqtt_host, self.args.mqtt_port,
            self.args.mqtt_user or "<anonymous>",
        )
        self.client.connect_async(self.args.mqtt_host, self.args.mqtt_port, keepalive=60)
        self.client.loop_start()

        # Apply startup state (mode or fixed level) if we can.
        if self.supports_control:
            try:
                level = (self.args.startup_level or "").strip().lower() or None
                if level == "off":
                    self.writer.write_fan_node("mode", NAME_TO_MODE["manual"])
                    self.writer.write_fan_node("level", 0)
                    self.writer.write_fan_node("enable", 0)
                    log.info("Startup: fan disabled")
                elif level in ("low", "mid", "high"):
                    self.writer.write_fan_node("enable", 1)
                    self.writer.write_fan_node("mode", NAME_TO_MODE["manual"])
                    self.writer.write_fan_node("level", NAME_TO_LEVEL[level])
                    log.info("Startup: manual level=%s", level)
                else:
                    self.writer.write_fan_node("enable", 1)
                    self.writer.write_fan_node("mode", NAME_TO_MODE[self.args.default_mode])
                    log.info("Startup: fan mode=%s", self.args.default_mode)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to apply startup state: %s", exc)

        try:
            while not self.stop_event.is_set():
                self._publish_state()
                self.stop_event.wait(self.args.poll)
        finally:
            self._shutdown()

    def stop(self) -> None:
        log.info("Shutdown requested")
        self.stop_event.set()

    def _shutdown(self) -> None:
        try:
            self.client.publish(self.availability_topic, "offline", retain=True)
            time.sleep(0.1)
        finally:
            self.client.loop_stop()
            self.client.disconnect()

    # ---- MQTT callbacks -------------------------------------------------
    def _on_connect(self, client: mqtt.Client, _userdata, _flags, rc: int) -> None:
        if rc != 0:
            log.error("MQTT connect failed with rc=%s", rc)
            return
        log.info("MQTT connected")
        if self.supports_control:
            client.subscribe([
                (self.mode_command_topic, 0),
                (self.level_command_topic, 0),
                (self.enable_command_topic, 0),
                (self.fan_command_topic, 0),
                (self.fan_preset_command_topic, 0),
            ])
        self._publish_discovery()
        client.publish(self.availability_topic, "online", retain=True)
        self._publish_state()

    def _on_disconnect(self, _client, _userdata, rc: int) -> None:
        if rc != 0:
            log.warning("MQTT disconnected (rc=%s); paho will reconnect", rc)

    def _on_message(self, _client, _userdata, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic
        payload = msg.payload.decode(errors="replace").strip()
        log.debug("MQTT <- %s %r", topic, payload)

        if not self.supports_control:
            log.warning("Ignoring command on read-only backend: %s", topic)
            return

        try:
            if topic == self.mode_command_topic:
                self._handle_mode(payload)
            elif topic == self.level_command_topic:
                self._handle_level(payload)
            elif topic == self.enable_command_topic:
                self._handle_enable(payload)
            elif topic == self.fan_command_topic:
                self._handle_fan_state(payload)
            elif topic == self.fan_preset_command_topic:
                self._handle_fan_preset(payload)
            else:
                log.debug("Unhandled topic %s", topic)
                return
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to handle %s=%r: %s", topic, payload, exc)
            return

        # Immediately mirror back the change so HA's UI is snappy.
        self._publish_state()

    # ---- Command handlers (self-healing) --------------------------------
    def _handle_mode(self, payload: str) -> None:
        mode = NAME_TO_MODE.get(payload.lower())
        if mode is None:
            raise ValueError(f"invalid mode payload: {payload!r}")
        self.writer.write_fan_node("enable", 1)
        self.writer.write_fan_node("mode", mode)
        log.info("Fan mode -> %s", payload.lower())

    def _handle_level(self, payload: str) -> None:
        level = NAME_TO_LEVEL.get(payload.lower())
        if level is None:
            raise ValueError(f"invalid level payload: {payload!r}")
        if level == 0:
            self.writer.write_fan_node("mode", NAME_TO_MODE["manual"])
            self.writer.write_fan_node("level", 0)
            self.writer.write_fan_node("enable", 0)
            log.info("Fan level -> off (disabled)")
        else:
            self.writer.write_fan_node("enable", 1)
            self.writer.write_fan_node("mode", NAME_TO_MODE["manual"])
            self.writer.write_fan_node("level", level)
            log.info("Fan level -> %s (manual)", payload.lower())

    def _handle_enable(self, payload: str) -> None:
        value = payload.lower()
        if value in ("on", "1", "true"):
            self.writer.write_fan_node("enable", 1)
            log.info("Fan enable -> on")
        elif value in ("off", "0", "false"):
            self.writer.write_fan_node("enable", 0)
            log.info("Fan enable -> off")
        else:
            raise ValueError(f"invalid enable payload: {payload!r}")

    def _handle_fan_state(self, payload: str) -> None:
        value = payload.lower()
        if value in ("on", "1", "true"):
            self.writer.write_fan_node("enable", 1)
            log.info("Fan entity -> ON")
        elif value in ("off", "0", "false"):
            self.writer.write_fan_node("enable", 0)
            log.info("Fan entity -> OFF")
        else:
            raise ValueError(f"invalid fan state payload: {payload!r}")

    def _handle_fan_preset(self, payload: str) -> None:
        preset = payload.lower()
        if preset == "auto":
            self.writer.write_fan_node("enable", 1)
            self.writer.write_fan_node("mode", NAME_TO_MODE["auto"])
            log.info("Fan preset -> auto")
        elif preset in ("low", "mid", "high"):
            self.writer.write_fan_node("enable", 1)
            self.writer.write_fan_node("mode", NAME_TO_MODE["manual"])
            self.writer.write_fan_node("level", NAME_TO_LEVEL[preset])
            log.info("Fan preset -> %s", preset)
        else:
            raise ValueError(f"invalid preset payload: {payload!r}")

    # ---- Discovery ------------------------------------------------------
    def _publish_discovery(self) -> None:
        avail = [{"topic": self.availability_topic}]

        self._pub_disco("sensor", "temp", {
            "name": "VIM4 CPU Temp",
            "unique_id": "vim4_cpu_temp",
            "state_topic": self.temp_state_topic,
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "value_template": "{{ (value | float) / 1000 }}",
            "availability": avail, "device": DEVICE_INFO,
        })

        if not self.supports_control:
            # Monitor-only: publish sensors instead of selects/switches.
            self._pub_disco("sensor", "level", {
                "name": "VIM4 Fan Level", "unique_id": "vim4_fan_level",
                "state_topic": self.level_state_topic, "icon": "mdi:fan",
                "availability": avail, "device": DEVICE_INFO,
            })
            self._pub_disco("sensor", "mode", {
                "name": "VIM4 Fan Mode", "unique_id": "vim4_fan_mode",
                "state_topic": self.mode_state_topic, "icon": "mdi:fan-auto",
                "availability": avail, "device": DEVICE_INFO,
            })
            self._pub_disco("binary_sensor", "enable", {
                "name": "VIM4 Fan Enabled", "unique_id": "vim4_fan_enable",
                "state_topic": self.enable_state_topic,
                "payload_on": "on", "payload_off": "off",
                "device_class": "running",
                "availability": avail, "device": DEVICE_INFO,
            })
            log.info("Published monitor-only MQTT Discovery config")
            return

        # Full control: selects, switch, fan.
        self._pub_disco("select", "mode", {
            "name": "VIM4 Fan Mode", "unique_id": "vim4_fan_mode",
            "state_topic": self.mode_state_topic,
            "command_topic": self.mode_command_topic,
            "options": list(MODE_NAMES.values()),
            "availability": avail, "device": DEVICE_INFO,
        })
        self._pub_disco("select", "level", {
            "name": "VIM4 Fan Level", "unique_id": "vim4_fan_level",
            "state_topic": self.level_state_topic,
            "command_topic": self.level_command_topic,
            "options": list(LEVEL_NAMES.values()),
            "availability": avail, "device": DEVICE_INFO,
        })
        self._pub_disco("switch", "enable", {
            "name": "VIM4 Fan Enable", "unique_id": "vim4_fan_enable",
            "state_topic": self.enable_state_topic,
            "command_topic": self.enable_command_topic,
            "payload_on": "on", "payload_off": "off",
            "availability": avail, "device": DEVICE_INFO,
        })
        self._pub_disco("fan", "fan", {
            "name": "VIM4 Fan", "unique_id": "vim4_fan",
            "state_topic": self.fan_state_topic,
            "command_topic": self.fan_command_topic,
            "payload_on": "ON", "payload_off": "OFF",
            "preset_mode_state_topic": self.fan_preset_state_topic,
            "preset_mode_command_topic": self.fan_preset_command_topic,
            "preset_modes": ["auto", "low", "mid", "high"],
            "availability": avail, "device": DEVICE_INFO,
        })
        log.info("Published full-control MQTT Discovery config")

    def _pub_disco(self, component: str, object_id: str, cfg: dict) -> None:
        topic = f"{self.disco}/{component}/vim4/{object_id}/config"
        self.client.publish(topic, json.dumps(cfg), retain=True)

    # ---- State publishing -----------------------------------------------
    def _publish_state(self) -> None:
        temp = read_int(FAN_SYSFS / "temp")
        if temp is None:
            temp = read_int(THERMAL_ZONE / "temp")
        if temp is not None:
            self.client.publish(self.temp_state_topic, str(temp), retain=True)

        level = read_int(FAN_SYSFS / "level")
        mode = read_int(FAN_SYSFS / "mode")
        enable = read_int(FAN_SYSFS / "enable")

        if mode is not None and mode in MODE_NAMES:
            self.client.publish(self.mode_state_topic, MODE_NAMES[mode], retain=True)
        if level is not None and level in LEVEL_NAMES:
            self.client.publish(self.level_state_topic, LEVEL_NAMES[level], retain=True)
        if enable is not None:
            self.client.publish(self.enable_state_topic, "on" if enable else "off", retain=True)

        # High-level fan entity state (only meaningful when control is enabled,
        # but harmless to publish regardless).
        if enable is not None:
            self.client.publish(
                self.fan_state_topic, "ON" if enable else "OFF", retain=True,
            )
        if mode is not None:
            if mode == NAME_TO_MODE["auto"]:
                preset = "auto"
            elif level in (1, 2, 3):
                preset = LEVEL_NAMES[level]
            else:
                preset = None
            if preset is not None:
                self.client.publish(self.fan_preset_state_topic, preset, retain=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VIM4 fan <-> MQTT bridge (SSH writes)")
    p.add_argument("--mqtt-host", required=True)
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--mqtt-user", default="")
    p.add_argument("--mqtt-pass", default="")
    p.add_argument("--poll", type=int, default=10)
    p.add_argument("--default-mode", default="auto", choices=sorted(NAME_TO_MODE))
    p.add_argument("--startup-level", default="")
    p.add_argument("--discovery-prefix", default="homeassistant")
    p.add_argument("--base-topic", default="vim4/fan")
    p.add_argument("--ssh-enabled", default="false")
    p.add_argument("--ssh-host", default="172.30.32.1")
    p.add_argument("--ssh-port", type=int, default=22222)
    p.add_argument("--ssh-user", default="root")
    p.add_argument("--ssh-key", default="/data/ssh/id_ed25519")
    p.add_argument("--ssh-known-hosts", default="/data/ssh/known_hosts")
    p.add_argument(
        "--log-level", default="info",
        choices=["trace", "debug", "info", "notice", "warning", "error", "fatal"],
    )
    return p.parse_args(argv)


def configure_logging(level: str) -> None:
    mapping = {
        "trace": logging.DEBUG, "debug": logging.DEBUG, "info": logging.INFO,
        "notice": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR,
        "fatal": logging.CRITICAL,
    }
    logging.basicConfig(
        level=mapping.get(level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)

    if not FAN_SYSFS.is_dir():
        log.critical("/sys/class/fan is not present; is the kernel fan driver loaded?")
        return 2

    writer: Optional[SshWriter] = None
    if args.ssh_enabled.lower() in ("1", "true", "yes"):
        writer = SshWriter(
            host=args.ssh_host, port=args.ssh_port, user=args.ssh_user,
            key_path=args.ssh_key, known_hosts_path=args.ssh_known_hosts,
        )
        log.info(
            "SSH writer: %s@%s:%s key=%s",
            writer.user, writer.host, writer.port, writer.key_path,
        )
    else:
        log.warning(
            "SSH control disabled — publishing read-only sensors only. "
            "Follow the README to install the addon's public key on the host."
        )

    bridge = Bridge(args, writer=writer)

    def _signal(signum, _frame) -> None:
        log.info("Caught signal %s", signum)
        bridge.stop()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    try:
        bridge.run()
    except Exception as exc:  # noqa: BLE001
        log.critical("Fatal error: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
