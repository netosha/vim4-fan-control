# Changelog

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
