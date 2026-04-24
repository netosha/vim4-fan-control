#!/usr/bin/with-contenv bashio
# Entry point for the VIM4 Fan Controller add-on.
# Resolves MQTT broker details from Supervisor, pushes configured trigger temps
# into /sys, then hands control to fan_mqtt.py.

set -e

bashio::log.info "Starting VIM4 Fan Controller..."

# ----- Remount /sys rw ------------------------------------------------------
# Supervisor mounts /sys read-only inside add-on containers even when
# full_access: true is set. CAP_SYS_ADMIN (implied by full_access) lets us
# remount it read-write so /sys/class/fan/* is writable.
remount_rw() {
    local target="$1"
    if mount | grep -E "on ${target} .*\(ro[, ]" >/dev/null 2>&1; then
        if mount -o remount,rw "$target" 2>/dev/null; then
            bashio::log.info "Remounted ${target} read-write"
        else
            bashio::log.warning "Failed to remount ${target} rw — writes may fail"
        fi
    fi
}

remount_rw /sys
# Some kernels mount the fan class on its own sysfs node; try it too.
remount_rw /sys/class/fan

# ----- Detect sysfs layout --------------------------------------------------
if [ -d /sys/class/fan ]; then
    bashio::log.info "Detected legacy Khadas fan driver at /sys/class/fan"
    SYSFS_MODE="khadas"
    # Verify a canonical node is actually writable now.
    if [ ! -w /sys/class/fan/enable ]; then
        bashio::log.error "/sys/class/fan/enable is still not writable."
        bashio::log.error "Confirm that full_access: true is set and the add-on"
        bashio::log.error "was fully stopped/started (not just restarted) after enabling it."
    fi
elif [ -f /sys/class/thermal/thermal_zone0/trip_point_3_temp ]; then
    bashio::log.warning "Khadas fan driver not found; falling back to /sys/class/thermal"
    SYSFS_MODE="thermal"
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
        target="/sys/class/fan/trigger_temp_${level}"
        if [ -w "$target" ]; then
            bashio::log.info "Setting ${target} = ${cfg_val}"
            echo "$cfg_val" > "$target" || bashio::log.warning "Failed to write $target"
        else
            bashio::log.warning "$target is not writable; skipping"
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
    --log-level "$LOG_LEVEL"
