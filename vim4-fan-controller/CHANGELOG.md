# Changelog

## 0.4.0 — full control via SSH to the HAOS host

Fan control is back. All writes are routed through `ssh root@172.30.32.1:22222`
(the HAOS debug SSH port), which is the one sanctioned way for an
add-on to reach the host's real `/sys` — the mount propagation lock
on the container's `/sys` bind-mount is finally sidestepped.

### Setup flow

1. Install the add-on. It generates an ed25519 keypair in
   `/data/ssh/` on first run.
2. Copy the public key from the add-on log (also mirrored to
   `/share/vim4-fan-ssh-pubkey.txt`).
3. Append it to `/root/.ssh/authorized_keys` on the HAOS host via an
   existing port-22222 SSH session.
4. Restart the add-on. The log will confirm `SSH control: ENABLED` and
   publish the control entities.

Until the public key is authorized, the add-on runs in monitor-only
mode (same entities as 0.3.0) so HA keeps getting temperature data.

### Added
- `fan.vim4_fan`, `select.vim4_fan_mode`, `select.vim4_fan_level`,
  `switch.vim4_fan_enable` are back, now actually working.
- `startup_level` option restored — set `off` / `low` / `mid` / `high`
  to force a fixed state on every add-on start.
- SSH ControlMaster keeps latency ≤50 ms per write after the first
  connection.

### Changed
- Dropped `util-linux` (no longer need `nsenter`); added
  `openssh-client`.
- AppArmor profile relaxed to allow writes to `/data/` (ssh key +
  known_hosts).

## 0.3.0 — monitor-only

Writing to `/sys/class/fan/*` from inside a HAOS add-on turned out to be
unreachable with the primitives Supervisor exposes. This release drops
the write path entirely and focuses on what works reliably: exposing
the VIM4 CPU temperature and fan state as read-only Home Assistant
sensors. The kernel's built-in auto mode continues to handle the fan
speed automatically.

### Changed
- Entities are now read-only sensors instead of selects/switches.
- Dropped `full_access`, `host_pid`, and `util-linux`. The add-on runs
  with no special privileges and an AppArmor profile that restricts it
  to reading the fan and thermal sysfs nodes.
- Removed the MQTT `fan.vim4_fan` entity, `select.vim4_fan_mode`,
  `select.vim4_fan_level`, and `switch.vim4_fan_enable`. Their state is
  still visible as `sensor.vim4_fan_*` / `binary_sensor.vim4_fan_enable`.

### Added
- `sensor.vim4_trigger_temp_low` / `_mid` / `_high` diagnostic sensors
  showing the thresholds the kernel is using for auto-mode switching.

### Removed
- `startup_level`, `trigger_temp_{low,mid,high}` config options — they
  required write access to sysfs and no longer apply.

## 0.2.3

- Fix: writes via `/proc/1/root/sys/...` still hit `EROFS` because
  `/proc/<pid>/root` only exposes the target process's root directory —
  file operations still go through the caller's mount namespace. The
  add-on now ships `util-linux` (for `nsenter`), probes whether writes
  succeed via the container's /sys or only via the host's mount
  namespace, and uses `nsenter -t 1 -m -- sh -c 'echo V > /sys/…'` for
  every write when the container's sysfs is locked read-only.
- Added startup diagnostics: the log shows PID 1's identity (to confirm
  `host_pid: true` is in effect) and the mount flags on `/sys` inside
  the container. If writes still fail, the log now also prints the host's
  `mount | grep sys` output for further debugging.

## 0.2.2

- Fix: writes to `/sys/class/fan/*` still failed with `EROFS` even after
  remount, because Supervisor's bind-mount of `/sys` is locked read-only
  and the ro flag propagates through `remount,rw`. The add-on now enables
  `host_pid: true` and routes writes through `/proc/1/root/sys/class/fan`,
  which is the host's real (rw) sysfs.
- `run.sh` probes both paths at startup with a round-trip write test and
  picks the one that actually works. Falls back to the other automatically
  if either is unavailable.
- `fan_mqtt.py` now accepts `--fan-path` / `--thermal-path` so the sysfs
  location is resolved in the shell wrapper rather than hard-coded.

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
