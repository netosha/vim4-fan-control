# Changelog

## 0.2.1

- Fix: `/sys/class/fan/*` writes failed with `[Errno 30] Read-only file
  system` even with `full_access: true`, because Supervisor mounts `/sys`
  read-only inside add-on containers. `run.sh` now remounts `/sys` rw at
  startup.
- Disabled the custom AppArmor profile (`apparmor: false`). Supervisor's
  AppArmor parser rejects profiles with `mount` clauses, which were needed
  for the remount. The profile is still shipped for reference.

## 0.2.0

- Added a proper HA `fan.vim4_fan` entity (MQTT fan platform) with on/off
  state and preset modes (`auto`/`low`/`mid`/`high`). `fan.turn_off` now
  fully disables the fan; `fan.set_preset_mode` picks a speed.
- Level / mode writes now self-heal: picking a non-off level automatically
  flips to manual mode and enables the fan, so `select.select_option` on
  `select.vim4_fan_level` actually starts the fan spinning.
- Added `startup_level` config option — set to `off` to disable the fan on
  every boot, or `low`/`mid`/`high` to force a fixed manual speed.

## 0.1.0 — initial release

- Privileged add-on (`full_access: true`) that reads/writes
  `/sys/class/fan/*` on Khadas VIM4 HAOS (`vim4-haos-15.2.img.xz` and
  compatible Khadas builds).
- MQTT Discovery entities:
  - `sensor.vim4_cpu_temp`
  - `select.vim4_fan_mode` (auto/manual)
  - `select.vim4_fan_level` (off/low/mid/high)
  - `switch.vim4_fan_enable`
- Custom AppArmor profile scoping the privileged access to fan and thermal
  sysfs paths.
- Auto-mode trigger temps (`trigger_temp_low/mid/high`) configurable via
  add-on options and pushed to sysfs on each start.
- Read-only fallback via `/sys/class/thermal/thermal_zone0/temp` when the
  Khadas fan driver is absent (mainline-kernel builds).
