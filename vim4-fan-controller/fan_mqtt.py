#!/usr/bin/env python3
"""VIM4 Fan Controller <-> MQTT bridge.

Reads and writes /sys/class/fan/* on the Khadas VIM4 host (or a thermal-zone
fallback on mainline kernels) and exposes the fan as Home Assistant MQTT
Discovery entities:

  - sensor.vim4_cpu_temp     (°C, read-only)
  - select.vim4_fan_mode     (auto | manual)
  - select.vim4_fan_level    (off | low | mid | high)
  - binary_sensor.vim4_fan_enable (on/off mirror of /sys/class/fan/enable)

State flow:
    poll loop  -> read sysfs -> publish retained state topics
    HA command -> MQTT set topic -> write sysfs -> next poll confirms state

The add-on must be running with full_access: true for /sys writes to succeed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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
    "sw_version": "0.1.0",
}

log = logging.getLogger("vim4_fan")


# ---------------------------------------------------------------------------
# Sysfs backends
# ---------------------------------------------------------------------------


class FanBackend:
    """Abstracts the 'khadas' (/sys/class/fan) and 'thermal' fallbacks.

    Only the 'khadas' backend supports level/mode writes. The 'thermal' backend
    is read-only and only exposes the CPU temperature."""

    def __init__(self, mode: str) -> None:
        self.mode = mode

    # ---- reads ----------------------------------------------------------
    def read_temp_millicelsius(self) -> Optional[int]:
        raise NotImplementedError

    def read_level(self) -> Optional[int]:
        return None

    def read_mode(self) -> Optional[int]:
        return None

    def read_enable(self) -> Optional[int]:
        return None

    # ---- writes ---------------------------------------------------------
    def write_level(self, level: int) -> None:
        raise RuntimeError("Level writes not supported on this backend")

    def write_mode(self, mode: int) -> None:
        raise RuntimeError("Mode writes not supported on this backend")

    def write_enable(self, enable: int) -> None:
        raise RuntimeError("Enable writes not supported on this backend")

    @property
    def supports_control(self) -> bool:
        return False


class KhadasFanBackend(FanBackend):
    """Legacy Khadas vendor-kernel driver at /sys/class/fan/*."""

    def __init__(self) -> None:
        super().__init__("khadas")

    @staticmethod
    def _read(name: str) -> str:
        return (FAN_SYSFS / name).read_text().strip()

    @staticmethod
    def _write(name: str, value: str) -> None:
        path = FAN_SYSFS / name
        # sysfs nodes don't accept fancy writes; a plain open/write is fine.
        with path.open("w") as fh:
            fh.write(str(value))

    def read_temp_millicelsius(self) -> Optional[int]:
        try:
            return int(self._read("temp"))
        except (OSError, ValueError) as exc:
            log.debug("temp read failed: %s", exc)
            return None

    def read_level(self) -> Optional[int]:
        try:
            return int(self._read("level"))
        except (OSError, ValueError) as exc:
            log.debug("level read failed: %s", exc)
            return None

    def read_mode(self) -> Optional[int]:
        try:
            return int(self._read("mode"))
        except (OSError, ValueError) as exc:
            log.debug("mode read failed: %s", exc)
            return None

    def read_enable(self) -> Optional[int]:
        try:
            return int(self._read("enable"))
        except (OSError, ValueError) as exc:
            log.debug("enable read failed: %s", exc)
            return None

    def write_level(self, level: int) -> None:
        if level not in LEVEL_NAMES:
            raise ValueError(f"invalid level {level!r}")
        self._write("level", str(level))

    def write_mode(self, mode: int) -> None:
        if mode not in MODE_NAMES:
            raise ValueError(f"invalid mode {mode!r}")
        self._write("mode", str(mode))

    def write_enable(self, enable: int) -> None:
        if enable not in (0, 1):
            raise ValueError(f"invalid enable {enable!r}")
        self._write("enable", str(enable))

    @property
    def supports_control(self) -> bool:
        return True


class ThermalFanBackend(FanBackend):
    """Read-only fallback: reports CPU temp from the first thermal_zone.

    Used when the host kernel is mainline and the Khadas fan class is absent.
    Control is not possible without a kernel driver, but we still expose the
    temperature so automations have something to trigger on."""

    def __init__(self) -> None:
        super().__init__("thermal")

    def read_temp_millicelsius(self) -> Optional[int]:
        try:
            return int((THERMAL_ZONE / "temp").read_text().strip())
        except (OSError, ValueError) as exc:
            log.debug("thermal temp read failed: %s", exc)
            return None


def make_backend(requested: str) -> FanBackend:
    if requested == "khadas":
        if not FAN_SYSFS.is_dir():
            raise RuntimeError(
                f"{FAN_SYSFS} missing; either the host lacks the Khadas fan "
                "driver or full_access is not enabled for this add-on."
            )
        return KhadasFanBackend()
    if requested == "thermal":
        if not (THERMAL_ZONE / "temp").exists():
            raise RuntimeError(f"{THERMAL_ZONE / 'temp'} missing; no thermal fallback available")
        return ThermalFanBackend()
    raise ValueError(f"unknown sysfs mode {requested!r}")


# ---------------------------------------------------------------------------
# MQTT bridge
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self, args: argparse.Namespace, backend: FanBackend) -> None:
        self.args = args
        self.backend = backend
        self.base = args.base_topic.rstrip("/")
        self.disco = args.discovery_prefix.rstrip("/")
        self.stop_event = threading.Event()

        client_id = f"vim4_fan_{os.getpid()}"
        self.client = mqtt.Client(client_id=client_id, clean_session=True)
        if args.mqtt_user:
            self.client.username_pw_set(args.mqtt_user, args.mqtt_pass or None)

        # Last-will so HA marks entities unavailable if the add-on crashes.
        self.availability_topic = f"{self.base}/availability"
        self.client.will_set(self.availability_topic, "offline", retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---- MQTT topic helpers --------------------------------------------
    @property
    def temp_state_topic(self) -> str:
        return f"{self.base}/temp"

    @property
    def mode_state_topic(self) -> str:
        return f"{self.base}/mode"

    @property
    def mode_command_topic(self) -> str:
        return f"{self.base}/mode/set"

    @property
    def level_state_topic(self) -> str:
        return f"{self.base}/level"

    @property
    def level_command_topic(self) -> str:
        return f"{self.base}/level/set"

    @property
    def enable_state_topic(self) -> str:
        return f"{self.base}/enable"

    @property
    def enable_command_topic(self) -> str:
        return f"{self.base}/enable/set"

    # ---- Lifecycle ------------------------------------------------------
    def run(self) -> None:
        log.info(
            "Connecting to MQTT broker %s:%s (user=%s)",
            self.args.mqtt_host,
            self.args.mqtt_port,
            self.args.mqtt_user or "<anonymous>",
        )
        self.client.connect_async(self.args.mqtt_host, self.args.mqtt_port, keepalive=60)
        self.client.loop_start()

        # Apply default mode once on startup (if backend supports it).
        if self.backend.supports_control:
            try:
                default = NAME_TO_MODE[self.args.default_mode]
                self.backend.write_mode(default)
                log.info("Set initial fan mode to %s", self.args.default_mode)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to apply default_mode: %s", exc)

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
            # Give the broker a moment to flush.
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

        subs = []
        if self.backend.supports_control:
            subs.extend(
                [
                    (self.mode_command_topic, 0),
                    (self.level_command_topic, 0),
                    (self.enable_command_topic, 0),
                ]
            )
        if subs:
            client.subscribe(subs)
            log.debug("Subscribed to %s", [t for t, _ in subs])

        self._publish_discovery()
        client.publish(self.availability_topic, "online", retain=True)
        # Publish an immediate state snapshot so HA doesn't sit on stale retained values.
        self._publish_state()

    def _on_disconnect(self, _client, _userdata, rc: int) -> None:
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%s); paho will reconnect", rc)
        else:
            log.info("MQTT disconnected cleanly")

    def _on_message(self, _client, _userdata, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic
        payload = msg.payload.decode(errors="replace").strip()
        log.debug("MQTT <- %s %r", topic, payload)

        if not self.backend.supports_control:
            log.warning("Ignoring command on read-only backend: %s", topic)
            return

        try:
            if topic == self.mode_command_topic:
                self._handle_mode_cmd(payload)
            elif topic == self.level_command_topic:
                self._handle_level_cmd(payload)
            elif topic == self.enable_command_topic:
                self._handle_enable_cmd(payload)
            else:
                log.debug("Unhandled topic %s", topic)
                return
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to handle %s=%r: %s", topic, payload, exc)
            return

        # Reflect the change back on the state topic immediately rather than
        # waiting for the next poll tick, so HA's UI feels responsive.
        self._publish_state()

    # ---- Command handlers ----------------------------------------------
    def _handle_mode_cmd(self, payload: str) -> None:
        mode = NAME_TO_MODE.get(payload.lower())
        if mode is None:
            raise ValueError(f"invalid mode payload: {payload!r}")
        self.backend.write_mode(mode)
        log.info("Fan mode -> %s", payload.lower())

    def _handle_level_cmd(self, payload: str) -> None:
        level = NAME_TO_LEVEL.get(payload.lower())
        if level is None:
            raise ValueError(f"invalid level payload: {payload!r}")
        # A level write is only honored in manual mode, so switch automatically.
        self.backend.write_mode(NAME_TO_MODE["manual"])
        self.backend.write_level(level)
        log.info("Fan level -> %s (forced mode=manual)", payload.lower())

    def _handle_enable_cmd(self, payload: str) -> None:
        value = payload.strip().lower()
        if value in ("on", "1", "true"):
            self.backend.write_enable(1)
            log.info("Fan enable -> on")
        elif value in ("off", "0", "false"):
            self.backend.write_enable(0)
            log.info("Fan enable -> off")
        else:
            raise ValueError(f"invalid enable payload: {payload!r}")

    # ---- Discovery + state ---------------------------------------------
    def _publish_discovery(self) -> None:
        """Publish retained MQTT-Discovery config messages for each entity."""

        availability = [{"topic": self.availability_topic}]

        temp_cfg = {
            "name": "VIM4 CPU Temp",
            "unique_id": "vim4_cpu_temp",
            "state_topic": self.temp_state_topic,
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "value_template": "{{ (value | float) / 1000 }}",
            "availability": availability,
            "device": DEVICE_INFO,
        }
        self._publish_disco("sensor", "temp", temp_cfg)

        if self.backend.supports_control:
            mode_cfg = {
                "name": "VIM4 Fan Mode",
                "unique_id": "vim4_fan_mode",
                "state_topic": self.mode_state_topic,
                "command_topic": self.mode_command_topic,
                "options": list(MODE_NAMES.values()),
                "availability": availability,
                "device": DEVICE_INFO,
            }
            self._publish_disco("select", "mode", mode_cfg)

            level_cfg = {
                "name": "VIM4 Fan Level",
                "unique_id": "vim4_fan_level",
                "state_topic": self.level_state_topic,
                "command_topic": self.level_command_topic,
                "options": list(LEVEL_NAMES.values()),
                "availability": availability,
                "device": DEVICE_INFO,
            }
            self._publish_disco("select", "level", level_cfg)

            enable_cfg = {
                "name": "VIM4 Fan Enable",
                "unique_id": "vim4_fan_enable",
                "state_topic": self.enable_state_topic,
                "command_topic": self.enable_command_topic,
                "payload_on": "on",
                "payload_off": "off",
                "availability": availability,
                "device": DEVICE_INFO,
            }
            self._publish_disco("switch", "enable", enable_cfg)

        log.info("Published MQTT Discovery config under %s/…", self.disco)

    def _publish_disco(self, component: str, object_id: str, cfg: dict) -> None:
        topic = f"{self.disco}/{component}/vim4/{object_id}/config"
        self.client.publish(topic, json.dumps(cfg), retain=True)

    def _publish_state(self) -> None:
        """Read the backend once and publish retained state for every entity."""

        def safe(reader: Callable[[], Optional[int]]) -> Optional[int]:
            try:
                return reader()
            except Exception as exc:  # noqa: BLE001
                log.debug("sysfs read raised: %s", exc)
                return None

        temp = safe(self.backend.read_temp_millicelsius)
        if temp is not None:
            self.client.publish(self.temp_state_topic, str(temp), retain=True)

        if not self.backend.supports_control:
            return

        mode = safe(self.backend.read_mode)
        if mode is not None and mode in MODE_NAMES:
            self.client.publish(self.mode_state_topic, MODE_NAMES[mode], retain=True)

        level = safe(self.backend.read_level)
        if level is not None and level in LEVEL_NAMES:
            self.client.publish(self.level_state_topic, LEVEL_NAMES[level], retain=True)

        enable = safe(self.backend.read_enable)
        if enable is not None:
            self.client.publish(self.enable_state_topic, "on" if enable else "off", retain=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VIM4 fan <-> MQTT Discovery bridge")
    parser.add_argument("--mqtt-host", required=True)
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-user", default="")
    parser.add_argument("--mqtt-pass", default="")
    parser.add_argument("--poll", type=int, default=10, help="State-publish interval, seconds")
    parser.add_argument("--default-mode", default="auto", choices=sorted(NAME_TO_MODE))
    parser.add_argument("--discovery-prefix", default="homeassistant")
    parser.add_argument("--base-topic", default="vim4/fan")
    parser.add_argument("--sysfs-mode", default="khadas", choices=["khadas", "thermal"])
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["trace", "debug", "info", "notice", "warning", "error", "fatal"],
    )
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    # Map bashio levels onto Python's logging levels.
    mapping = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "notice": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
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

    try:
        backend = make_backend(args.sysfs_mode)
    except Exception as exc:  # noqa: BLE001
        log.critical("Failed to initialize fan backend: %s", exc)
        return 2

    log.info("Using %s backend; control=%s", backend.mode, backend.supports_control)
    bridge = Bridge(args, backend)

    def _signal_handler(signum, _frame) -> None:
        log.info("Caught signal %s", signum)
        bridge.stop()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        bridge.run()
    except Exception as exc:  # noqa: BLE001
        log.critical("Fatal error in bridge loop: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
