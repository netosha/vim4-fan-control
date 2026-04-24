#!/usr/bin/with-contenv bashio
# Entry point for the VIM4 Fan Controller add-on.
# Resolves MQTT broker details from Supervisor, pushes configured trigger temps
# into /sys, then hands control to fan_mqtt.py.

set -e

bashio::log.info "Starting VIM4 Fan Controller..."

# ----- Find a WRITABLE fan sysfs path ---------------------------------------
# Supervisor bind-mounts the container's /sys read-only, and that ro flag
# propagates through `mount -o remount,rw` (mount propagation lock), so we
# can't fix it from inside. Instead we reach through /proc/1/root — with
# host_pid: true, PID 1 is the host init, and /proc/1/root/sys is the host's
# real (rw) sysfs. We still try remount as a best-effort fallback for setups
# where propagation isn't locked.
CONTAINER_FAN="/sys/class/fan"
HOST_FAN="/proc/1/root/sys/class/fan"
CONTAINER_THERMAL="/sys/class/thermal/thermal_zone0"
HOST_THERMAL="/proc/1/root/sys/class/thermal/thermal_zone0"

# Try a best-effort remount first — harmless when it succeeds, logged when not.
remount_rw() {
    local target="$1"
    local out
    if mount | grep -E "on ${target} .*\(ro[, ]" >/dev/null 2>&1; then
        if out=$(mount -o remount,rw "$target" 2>&1); then
            bashio::log.info "Remounted ${target} rw"
        else
            bashio::log.debug "Remount ${target} rw not available (${out}); will use host PID fallback"
        fi
    fi
}
remount_rw /sys

# Probe: write a harmless existing value to find a path we can actually write.
probe_writable() {
    local path="$1"
    [ -w "${path}/enable" ] || return 1
    # Even if the permission bit says writable, the mount can still block us
    # with EROFS. Do a no-op write (read current value, write it back) to be
    # sure.
    local cur
    cur=$(cat "${path}/enable" 2>/dev/null) || return 1
    (echo "$cur" > "${path}/enable") 2>/dev/null || return 1
    return 0
}

FAN_PATH=""
THERMAL_PATH=""

if [ -d "$CONTAINER_FAN" ] && probe_writable "$CONTAINER_FAN"; then
    FAN_PATH="$CONTAINER_FAN"
    bashio::log.info "Using container sysfs path: $FAN_PATH"
elif [ -d "$HOST_FAN" ] && probe_writable "$HOST_FAN"; then
    FAN_PATH="$HOST_FAN"
    bashio::log.info "Using host sysfs via /proc/1/root: $FAN_PATH"
elif [ -d "$CONTAINER_FAN" ]; then
    # Directory exists but isn't writable from any path. Point at the host
    # version anyway so at least temp reads work.
    FAN_PATH="$HOST_FAN"
    bashio::log.warning "$CONTAINER_FAN present but not writable; using $HOST_FAN (may still fail)"
fi

if [ -n "$FAN_PATH" ]; then
    SYSFS_MODE="khadas"
    bashio::log.info "Detected legacy Khadas fan driver; FAN_PATH=$FAN_PATH"
elif [ -f "$CONTAINER_THERMAL/trip_point_3_temp" ]; then
    SYSFS_MODE="thermal"
    THERMAL_PATH="$CONTAINER_THERMAL"
    bashio::log.warning "Khadas fan driver not found; falling back to $THERMAL_PATH"
elif [ -f "$HOST_THERMAL/trip_point_3_temp" ]; then
    SYSFS_MODE="thermal"
    THERMAL_PATH="$HOST_THERMAL"
    bashio::log.warning "Khadas fan driver not found; falling back to $THERMAL_PATH"
else
    bashio::log.fatal "No supported fan control interface found on this host."
    bashio::log.fatal "Expected /sys/class/fan/ (Khadas vendor kernel) or"
    bashio::log.fatal "         /sys/class/thermal/thermal_zone0/trip_point_3_temp (mainline)."
    exit 1
fi

# ----- Resolve MQTT broker via Supervisor services API ----------------------
if ! bashio::services.available "mqtt"; then
    bashio::log.fatal "No MQTT service configured in Home Assistant."
    bashio::log.fatal "Install the Mosquitto add-on (or connect an external broker) first."
    exit 1
fi

MQTT_HOST=$(bashio::services mqtt "host")
MQTT_PORT=$(bashio::services mqtt "port")
MQTT_USER=$(bashio::services mqtt "username")
MQTT_PASS=$(bashio::services mqtt "password")

POLL=$(bashio::config 'poll_seconds')
DEFAULT_MODE=$(bashio::config 'default_mode')
STARTUP_LEVEL=$(bashio::config 'startup_level')
DISCO_PREFIX=$(bashio::config 'mqtt_discovery_prefix')
BASE_TOPIC=$(bashio::config 'mqtt_base_topic')
LOG_LEVEL=$(bashio::config 'log_level')

# ----- Push trigger temps into the kernel driver ----------------------------
# Writing to trigger_temp_* is ignored unless mode=auto (0), but we still set
# them unconditionally so they're correct the moment the user flips to auto.
if [ "$SYSFS_MODE" = "khadas" ]; then
    for level in low mid high; do
        cfg_key="trigger_temp_${level}"
        cfg_val=$(bashio::config "$cfg_key")
        target="${FAN_PATH}/trigger_temp_${level}"
        if out=$(echo "$cfg_val" > "$target" 2>&1); then
            bashio::log.info "Set ${target} = ${cfg_val}"
        else
            bashio::log.warning "Failed to write ${target}: ${out}"
        fi
    done
fi

# ----- Launch the MQTT bridge -----------------------------------------------
exec python3 -u /app/fan_mqtt.py \
    --mqtt-host "$MQTT_HOST" \
    --mqtt-port "$MQTT_PORT" \
    --mqtt-user "$MQTT_USER" \
    --mqtt-pass "$MQTT_PASS" \
    --poll "$POLL" \
    --default-mode "$DEFAULT_MODE" \
    --startup-level "$STARTUP_LEVEL" \
    --discovery-prefix "$DISCO_PREFIX" \
    --base-topic "$BASE_TOPIC" \
    --sysfs-mode "$SYSFS_MODE" \
    --fan-path "$FAN_PATH" \
    --thermal-path "$THERMAL_PATH" \
    --log-level "$LOG_LEVEL"
