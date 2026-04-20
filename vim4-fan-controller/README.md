# VIM4 Fan Controller

Home Assistant add-on that controls the Khadas VIM4 internal fan by writing
directly to `/sys/class/fan/*` on the host and exposing the fan as MQTT
Discovery entities.

## What you get

Once the add-on is running you'll see these entities appear automatically in
Home Assistant (no `configuration.yaml` edits required):

- `sensor.vim4_cpu_temp` — CPU temperature in °C.
- `select.vim4_fan_mode` — `auto` (kernel trigger-temp driven) or `manual`.
- `select.vim4_fan_level` — `off`, `low`, `mid`, `high`. Writing this
  automatically flips the mode to `manual`.
- `switch.vim4_fan_enable` — master on/off for the fan.

All entities are grouped under a single `Khadas VIM4` device in HA.

## Requirements

- **Home Assistant OS on a Khadas VIM4.** Khadas ships its own HAOS image
  (`vim4-haos-15.2.img.xz`) built on the legacy 5.4/5.15 kernel that exposes
  `/sys/class/fan/`. Generic upstream HAOS does not support the VIM4.
- **An MQTT broker** installed and configured. The Mosquitto add-on from the
  official add-on store is the simplest option.
- **aarch64 architecture** — this add-on does not build for other arches.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ (top right) → **Repositories**.
2. Paste the URL of *this* repository (e.g.
   `https://github.com/netosha/vim4-fan-control`) and click **Add**.
3. Find **VIM4 Fan Controller** in the store, click **Install**.
4. Open the add-on, review **Configuration**, then click **Start**.

The add-on auto-starts on boot (`boot: auto`).

### ⚠️ Security note

This add-on runs with `full_access: true`, the equivalent of Docker
`--privileged`. That's required because the Home Assistant Core container's
`/sys` is mounted read-only and does not include the host's
`/sys/class/fan/` nodes — there is no narrower Supervisor primitive that
grants write access to host sysfs. An AppArmor profile is shipped that limits
writes to `/sys/class/fan/**` and `/sys/devices/**/fan/**`.

Home Assistant displays a yellow warning on install because of
`full_access`; that's expected.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `poll_seconds` | `10` | How often (seconds) sysfs is polled and state republished. |
| `default_mode` | `auto` | Fan mode applied on add-on start. `auto` lets the kernel drive speed from trigger temps; `manual` holds whatever level was last set. |
| `trigger_temp_low` | `45` | Auto-mode threshold (°C) for low speed. |
| `trigger_temp_mid` | `55` | Auto-mode threshold (°C) for mid speed. |
| `trigger_temp_high` | `65` | Auto-mode threshold (°C) for high speed. |
| `mqtt_discovery_prefix` | `homeassistant` | Discovery topic prefix. Match whatever your MQTT integration uses. |
| `mqtt_base_topic` | `vim4/fan` | Prefix for state and command topics. |
| `log_level` | `info` | One of `trace`, `debug`, `info`, `notice`, `warning`, `error`, `fatal`. |

Trigger temps are written to `/sys/class/fan/trigger_temp_{low,mid,high}` on
every start, so editing the options and restarting the add-on is enough to
update them. They only take effect while the fan is in `auto` mode.

## Automations

### Hands-off: let the kernel do it

Leave `default_mode: auto` and tune the trigger temps via the add-on options.
The vendor fan driver handles speed switching on its own, and the fan keeps
reacting to load even when Home Assistant itself is restarting.

### HA-driven hysteresis

Switch to manual mode and write automations against the entities. Example:

```yaml
alias: VIM4 fan – turn HIGH under sustained load
mode: single
trigger:
  - platform: numeric_state
    entity_id: sensor.vim4_cpu_temp
    above: 70
    for: "00:00:30"
action:
  - service: select.select_option
    target:
      entity_id: select.vim4_fan_level
    data:
      option: high
```

```yaml
alias: VIM4 fan – back to LOW when cool
mode: single
trigger:
  - platform: numeric_state
    entity_id: sensor.vim4_cpu_temp
    below: 55
    for: "00:02:00"
action:
  - service: select.select_option
    target:
      entity_id: select.vim4_fan_level
    data:
      option: low
```

### Hybrid: auto floor + HA override

Keep `default_mode: auto` so the kernel manages a sensible baseline, and add
HA automations that flip to `manual`/`high` in edge cases (e.g. long video
transcodes, summer ambient spikes).

## Debugging

If entities don't appear, check the add-on log (Settings → Add-ons → VIM4 Fan
Controller → Log). The most common issues:

- **`/sys/class/fan` missing.** Confirm on the host via port-22222 SSH:
  `ls /sys/class/fan/`. If the directory truly isn't there, the kernel has
  been rebased away from the Khadas vendor tree. The add-on will log a
  warning and fall back to `/sys/class/thermal/thermal_zone0/temp` — you'll
  get a temperature sensor but not the select entities.
- **`No MQTT service configured`.** Install the Mosquitto add-on and
  configure the MQTT integration first.
- **Entities show `unavailable`.** The add-on publishes `online`/`offline` on
  `vim4/fan/availability`. If HA shows `unavailable`, the add-on probably
  crashed — check the log.

## How it works

```
 Home Assistant            MQTT broker            This add-on            Host kernel
 ──────────────            ───────────            ────────────           ───────────
 select.vim4_fan_level ─▶  vim4/fan/level/set ─▶  write /sys/.../level ─▶  fan driver
                   ◀─── vim4/fan/level ◀───────   read  /sys/.../level ◀──
 sensor.vim4_cpu_temp ◀── vim4/fan/temp ◀──────── read  /sys/.../temp  ◀──
```

The `fan_mqtt.py` bridge runs inside the privileged add-on container. Writes
to `/sys/class/fan/*` are permitted because `full_access: true` exposes the
host's real sysfs read-write, and the custom AppArmor profile scopes that
access to just the fan-related nodes.

## License

MIT.
