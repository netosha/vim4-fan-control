# VIM4 Fan Controller — add-on documentation

See [README.md](README.md) for a full description. Short version:

1. Install an MQTT broker (Mosquitto add-on works).
2. Install this add-on from the repository and start it.
3. `sensor.vim4_cpu_temp`, `select.vim4_fan_mode`, `select.vim4_fan_level`,
   and `switch.vim4_fan_enable` appear automatically via MQTT Discovery.

Trigger temps and the default mode are set via the **Configuration** tab.

## Why this add-on needs `full_access`

The Khadas fan driver exposes its controls under `/sys/class/fan/`, but the
Home Assistant Core container mounts `/sys` read-only and does not include
those nodes. Supervisor does not offer a narrower primitive that gives a
custom add-on write access to host sysfs paths, so the add-on runs with
`full_access: true` (Docker `--privileged`). A custom AppArmor profile
(`apparmor.txt`) scopes writes to `/sys/class/fan/**` and
`/sys/devices/**/fan/**` to limit the blast radius.

## Automation recipes

See the README for the kernel-driven, HA-driven, and hybrid approaches.
