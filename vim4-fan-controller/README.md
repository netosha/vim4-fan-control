# VIM4 Fan Controller

Home Assistant add-on that controls the Khadas VIM4 fan by talking to
the HAOS host over its built-in debug SSH (port 22222). Publishes fan
state and CPU temperature to Home Assistant via MQTT Discovery.

## What you get

- `sensor.vim4_cpu_temp` — CPU temperature in °C.
- `select.vim4_fan_mode` — `auto` (kernel trigger-temp driven) or `manual`.
- `select.vim4_fan_level` — `off` / `low` / `mid` / `high`. Picking
  anything but `off` forces manual mode and enables the fan.
- `switch.vim4_fan_enable` — master on/off for the fan.
- `fan.vim4_fan` — HA fan platform entity. `fan.turn_off` fully
  disables the fan; `fan.set_preset_mode` picks auto / low / mid / high.

If the SSH setup hasn't been completed yet, the add-on publishes the
same entities as read-only sensors so Home Assistant keeps getting
temperature data during setup.

## Why SSH

HAOS Supervisor bind-mounts `/sys` read-only into add-on containers,
and the ro flag is locked by mount propagation — even
`full_access: true` + `CAP_SYS_ADMIN` can't remount it rw. The only
sanctioned way to reach the host's real sysfs from an add-on is port
22222 debug SSH, which runs on the HAOS host itself. The add-on
connects to `172.30.32.1:22222` (the Supervisor-network gateway to the
host) and executes `echo N > /sys/class/fan/…` as root.

## One-time setup

You'll need port-22222 SSH already working on your VIM4 (this requires
a USB `CONFIG` stick with your public key in `network/authorized_keys`
— see the HAOS developer docs if you haven't done it yet).

### 1. Install the add-on

**Settings → Add-ons → Add-on Store → ⋮ → Repositories** →
paste `https://github.com/netosha/vim4-fan-control` → **Add**.

Find **VIM4 Fan Controller** in the store → **Install** → **Start**.

### 2. Get the add-on's public key

Open the add-on's **Log** tab. Look for a banner like:

```
=================================================================
  VIM4 fan add-on SSH public key (paste on HAOS host):

  ssh-ed25519 AAAAC3Nza... vim4-fan-addon

  Also saved to: /share/vim4-fan-ssh-pubkey.txt
=================================================================
```

Copy the `ssh-ed25519 …` line. If you prefer the File Editor add-on,
the same key is at `/share/vim4-fan-ssh-pubkey.txt`.

### 3. Authorize the key on the HAOS host

In an existing port-22222 SSH session to your VIM4 host:

```bash
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cat >> /root/.ssh/authorized_keys <<'EOF'
ssh-ed25519 AAAAC3Nza... vim4-fan-addon
EOF
chmod 600 /root/.ssh/authorized_keys
```

Replace the `ssh-ed25519 …` line with the actual public key you copied.

### 4. Restart the add-on

**Settings → Add-ons → VIM4 Fan Controller → Restart**.

The log should now show:

```
SSH control: ENABLED (host /sys/class/fan/enable is writable)
Published full-control MQTT Discovery config
```

The control entities appear under **Settings → Devices & Services →
MQTT → Khadas VIM4** within a few seconds.

## Configuration

| Option | Default | Description |
| --- | --- | --- |
| `poll_seconds` | `10` | How often sysfs is polled and state republished. |
| `default_mode` | `auto` | Fan mode at add-on start (ignored when `startup_level` is non-empty). |
| `startup_level` | `""` | `""` = honor `default_mode`. `off` disables the fan on start. `low` / `mid` / `high` forces a manual speed on start. |
| `mqtt_discovery_prefix` | `homeassistant` | Discovery topic prefix — match your MQTT integration. |
| `mqtt_base_topic` | `vim4/fan` | Prefix for state and command topics. |
| `ssh_host` | `172.30.32.1` | HAOS host reachable from the Supervisor network. Usually the gateway. |
| `ssh_port` | `22222` | HAOS debug SSH port. |
| `ssh_user` | `root` | Leave as `root`. |
| `log_level` | `info` | One of `trace` / `debug` / `info` / `notice` / `warning` / `error` / `fatal`. |

## Automations

Hands-off kernel-managed fan:

```yaml
# Leave default_mode: auto; the kernel's trigger temps handle it.
# No automation needed — the fan just tracks CPU temp.
```

HA-driven manual curve with hysteresis:

```yaml
alias: VIM4 fan – HIGH under sustained load
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

alias: VIM4 fan – back to LOW when cool
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

## Troubleshooting

**Log says `SSH control: DISABLED`.** Either the public key isn't on the
host yet (step 3 above), or port 22222 isn't reachable from addons on
this install. Verify from `ssh -p 22222 root@<vim4-ip>` on your host:

```bash
grep 'vim4-fan-addon' /root/.ssh/authorized_keys
```

If you don't see a matching line, step 3 didn't take. If you do,
compare the key fingerprint with the one in `/share/vim4-fan-ssh-pubkey.txt`.

**Fan doesn't actually respond.** Select `high` from
`select.vim4_fan_level`. The log should show a line like
`Fan level -> high (manual)`. If you see that but the fan doesn't spin,
SSH to the host yourself and check whether `echo 1 > /sys/class/fan/enable`
works from the host shell.

**Entities show `unavailable`.** The add-on publishes `online`/`offline`
on `vim4/fan/availability`. Check the add-on log — most likely the MQTT
broker connection dropped.

## License

MIT.
