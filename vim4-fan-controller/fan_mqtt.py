#!/usr/bin/env python3
"""VIM4 Fan Controller <-> MQTT bridge.

Reads and writes /sys/class/fan/* on the Khadas VIM4 host (or a thermal-zone
fallback on mainline kernels) and exposes the fan as Home Assistant MQTT
Discovery entities:

  - sensor.vim4_cpu_temp     (°C, read-only)
  - select.vim4_fan_mode     (auto | manual)
  - select.vim4_fan_level    (off | low | mid | high)
  - switch.vim4_fan_enable   (master gate on /sys/class/fan/enable)
  - fan.vim4_fan             (HA fan platform: on/off + preset auto/low/mid/high)

Two ways to drive the fan are exposed for convenience:

  1. "High-level" via fan.vim4_fan — fan.turn_off disables the fan completely
     (enable=0), and fan.set_preset_mode picks auto/low/mid/high.

  2. "Low-level" via the select/switch entities — pick a level from the
     select, or flip the master switch off to cut power. Commands are
     self-healing: setting level=low/mid/high automatically enables the fan
     and forces manual mode, setting level=off cuts the master switch, and
     setting mode=auto or manual re-enables the fan.

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
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_FAN_SYSFS = Path("/sys/class/fan")
DEFAULT_THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0")

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
    """Legacy Khadas vendor-kernel driver at /sys/class/fan/*.

    Reads go through the (container's) fan_path directly. Writes use one of:

      - "direct": plain open()+write() on fan_path. Works if the container's
        /sys is rw.
      - "nsenter": shell out to `nsenter -t 1 -m -- sh -c 'echo V > /sys/...'`
        to enter the host's mount namespace and write to the host's real sysfs.
        Required on HAOS, where the container's /sys is a locked-ro bind mount.
    """

    def __init__(
        self,
        fan_path: Path = DEFAULT_FAN_SYSFS,
        write_method: str = "direct",
        host_fan_path: str = "/sys/class/fan",
    ) -> None:
        super().__init__("khadas")
        self.fan_path = fan_path
        self.write_method = write_method
        # Path as seen from INSIDE the host's mount namespace. Independent
        # of fan_path (which is the container-visible read path).
        self.host_fan_path = host_fan_path
        if write_method not in ("direct", "nsenter"):
            raise ValueError(f"unknown write_method {write_method!r}")

    def _read(self, name: str) -> str:
        return (self.fan_path / name).read_text().strip()

    def _write(self, name: str, value: str) -> None:
        if self.write_method == "nsenter":
            self._write_nsenter(name, value)
        else:
            self._write_direct(name, value)

    def _write_direct(self, name: str, value: str) -> None:
        path = self.fan_path / name
        with path.open("w") as fh:
            fh.write(str(value))

    def _write_nsenter(self, name: str, value: str) -> None:
        # Shell out because nsenter can't proxy a plain file-descriptor write;
        # it has to enter the target namespace and run a command there.
        target = f"{self.host_fan_path}/{name}"
        cmd = ["nsenter", "-t", "1", "-m", "--", "sh", "-c", f"echo {value} > {target}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            raise OSError(
                f"nsenter write {target}={value!r} failed "
                f"(rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

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

    def __init__(self, thermal_path: Path = DEFAULT_THERMAL_ZONE) -> None:
        super().__init__("thermal")
        self.thermal_path = thermal_path

    def read_temp_millicelsius(self) -> Optional[int]:
        try:
            return int((self.thermal_path / "temp").read_text().strip())
        except (OSError, ValueError) as exc:
            log.debug("thermal temp read failed: %s", exc)
            return None


def make_backend(
    requested: str,
    fan_path: Optional[str] = None,
    thermal_path: Optional[str] = None,
    write_method: str = "direct",
) -> FanBackend:
    if requested == "khadas":
        path = Path(fan_path) if fan_path else DEFAULT_FAN_SYSFS
        if not path.is_dir():
            raise RuntimeError(
                f"{path} missing; either the host lacks the Khadas fan "
                "driver or full_access/host_pid is not enabled for this add-on."
            )
        log.info(
            "KhadasFanBackend: read path=%s, write method=%s", path, write_method
        )
        return KhadasFanBackend(path, write_method=write_method)
    if requested == "thermal":
        path = Path(thermal_path) if thermal_path else DEFAULT_THERMAL_ZONE
        if not (path / "temp").exists():
            raise RuntimeError(f"{path / 'temp'} missing; no thermal fallback available")
        log.info("ThermalFanBackend using path: %s", path)
        return ThermalFanBackend(path)
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

    # Topics for the high-level HA `fan` entity. These translate fan.turn_off
    # / fan.set_preset_mode calls into writes against mode/level/enable.
    @property
    def fan_state_topic(self) -> str:
        return f"{self.base}/fan/state"

    @property
    def fan_command_topic(self) -> str:
        return f"{self.base}/fan/state/set"

    @property
    def fan_preset_state_topic(self) -> str:
        return f"{self.base}/fan/preset"

    @property
    def fan_preset_command_topic(self) -> str:
        return f"{self.base}/fan/preset/set"

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

        # Apply default mode (and optionally a manual startup level) once on
        # startup. Sequence: enable=1, mode, level — so the fan actually runs.
        if self.backend.supports_control:
            try:
                startup_level = (self.args.startup_level or "").strip().lower() or None
                if startup_level == "off":
                    # Explicit "off" overrides default_mode — kill the fan.
                    self.backend.write_mode(NAME_TO_MODE["manual"])
                    self.backend.write_level(0)
                    self.backend.write_enable(0)
                    log.info("Startup: fan disabled (startup_level=off)")
                elif startup_level in ("low", "mid", "high"):
                    self.backend.write_enable(1)
                    self.backend.write_mode(NAME_TO_MODE["manual"])
                    self.backend.write_level(NAME_TO_LEVEL[startup_level])
                    log.info("Startup: manual mode at level=%s", startup_level)
                else:
                    default = NAME_TO_MODE[self.args.default_mode]
                    self.backend.write_enable(1)
                    self.backend.write_mode(default)
                    log.info("Startup: fan mode = %s", self.args.default_mode)
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
                    (self.fan_command_topic, 0),
                    (self.fan_preset_command_topic, 0),
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
            elif topic == self.fan_command_topic:
                self._handle_fan_state_cmd(payload)
            elif topic == self.fan_preset_command_topic:
                self._handle_fan_preset_cmd(payload)
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
        # Re-enable the fan — picking a mode implies "I want the fan running".
        self.backend.write_enable(1)
        self.backend.write_mode(mode)
        log.info("Fan mode -> %s (enable=1)", payload.lower())

    def _handle_level_cmd(self, payload: str) -> None:
        level = NAME_TO_LEVEL.get(payload.lower())
        if level is None:
            raise ValueError(f"invalid level payload: {payload!r}")
        # Self-heal: picking a specific speed should "just work". off disables
        # the master switch too; any non-zero level forces manual mode AND
        # enables the fan so it actually starts spinning.
        if level == 0:
            self.backend.write_mode(NAME_TO_MODE["manual"])
            self.backend.write_level(0)
            self.backend.write_enable(0)
            log.info("Fan level -> off (enable=0)")
        else:
            self.backend.write_enable(1)
            self.backend.write_mode(NAME_TO_MODE["manual"])
            self.backend.write_level(level)
            log.info("Fan level -> %s (mode=manual, enable=1)", payload.lower())

    def _handle_enable_cmd(self, payload: str) -> None:
        value = payload.strip().lower()
        if value in ("on", "1", "true"):
            self.backend.write_enable(1)
            log.info("Fan enable -> on")
        elif value in ("off", "0", "false"):
            self.backend.write_enable(0)
            log.info("Fan enable -> off (all fan behaviour disabled)")
        else:
            raise ValueError(f"invalid enable payload: {payload!r}")

    def _handle_fan_state_cmd(self, payload: str) -> None:
        """HA fan.turn_off / fan.turn_on. OFF fully disables the fan."""
        value = payload.strip().lower()
        if value in ("on", "1", "true"):
            self.backend.write_enable(1)
            log.info("Fan entity -> ON (enable=1)")
        elif value in ("off", "0", "false"):
            self.backend.write_enable(0)
            log.info("Fan entity -> OFF (enable=0, all fan behaviour disabled)")
        else:
            raise ValueError(f"invalid fan state payload: {payload!r}")

    def _handle_fan_preset_cmd(self, payload: str) -> None:
        """HA fan.set_preset_mode: auto / low / mid / high."""
        preset = payload.strip().lower()
        if preset == "auto":
            self.backend.write_enable(1)
            self.backend.write_mode(NAME_TO_MODE["auto"])
            log.info("Fan preset -> auto (enable=1)")
        elif preset in ("low", "mid", "high"):
            self.backend.write_enable(1)
            self.backend.write_mode(NAME_TO_MODE["manual"])
            self.backend.write_level(NAME_TO_LEVEL[preset])
            log.info("Fan preset -> %s (mode=manual, enable=1)", preset)
        else:
            raise ValueError(f"invalid fan preset payload: {payload!r}")

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

            # High-level fan entity: the obvious way to "turn the fan off" or
            # "run the fan at a specific speed" from the HA UI or scripts.
            fan_cfg = {
                "name": "VIM4 Fan",
                "unique_id": "vim4_fan",
                "state_topic": self.fan_state_topic,
                "command_topic": self.fan_command_topic,
                "payload_on": "ON",
                "payload_off": "OFF",
                "preset_mode_state_topic": self.fan_preset_state_topic,
                "preset_mode_command_topic": self.fan_preset_command_topic,
                "preset_modes": ["auto", "low", "mid", "high"],
                "availability": availability,
                "device": DEVICE_INFO,
            }
            self._publish_disco("fan", "fan", fan_cfg)

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

        # Derive the high-level fan entity state from the raw sysfs values.
        if enable is not None:
            self.client.publish(
                self.fan_state_topic, "ON" if enable else "OFF", retain=True
            )
        if mode is not None:
            if mode == NAME_TO_MODE["auto"]:
                preset = "auto"
            elif level in (1, 2, 3):
                preset = LEVEL_NAMES[level]
            else:
                preset = None  # manual + level=0: ambiguous; leave preset alone
            if preset is not None:
                self.client.publish(self.fan_preset_state_topic, preset, retain=True)


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
    parser.add_argument(
        "--startup-level",
        default="",
        help=(
            "Override default_mode on startup. One of off|low|mid|high. "
            "'off' disables the fan entirely; low/mid/high force manual mode "
            "at that speed. Empty string = honor --default-mode."
        ),
    )
    parser.add_argument("--discovery-prefix", default="homeassistant")
    parser.add_argument("--base-topic", default="vim4/fan")
    parser.add_argument("--sysfs-mode", default="khadas", choices=["khadas", "thermal"])
    parser.add_argument(
        "--fan-path",
        default="",
        help="Override path to the Khadas fan sysfs directory (default /sys/class/fan).",
    )
    parser.add_argument(
        "--thermal-path",
        default="",
        help="Override path to the thermal-zone directory (default /sys/class/thermal/thermal_zone0).",
    )
    parser.add_argument(
        "--write-method",
        default="direct",
        choices=["direct", "nsenter"],
        help=(
            "How to persist writes to /sys/class/fan/*. 'direct' opens the file; "
            "'nsenter' shells out into the host's mount namespace via nsenter(1). "
            "Use nsenter on HAOS where the container's /sys is locked ro."
        ),
    )
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
        backend = make_backend(
            args.sysfs_mode,
            fan_path=args.fan_path or None,
            thermal_path=args.thermal_path or None,
            write_method=args.write_method,
        )
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
