#!/usr/bin/with-contenv bashio
# Entry point for the VIM4 Fan Controller add-on.
# Resolves MQTT broker details from Supervisor, pushes configured trigger temps
# into /sys, then hands control to fan_mqtt.py.

set -e

bashio::log.info "Starting VIM4 Fan Controller..."

# ----- Diagnostics ----------------------------------------------------------
# /proc/1/root only exposes PID 1's root directory — file operations still go
# through the CALLER's mount namespace, which is the container's ro /sys.
# Entering the host's mount namespace with nsenter is the only way to land
# writes on the host's real sysfs.
bashio::log.info "Host PID 1 (expect: host init if host_pid: true took effect):"
head -n 1 /proc/1/status 2>/dev/null | sed 's/^/  /' | while read -r line; do bashio::log.info "$line"; done
bashio::log.info "Mount state of /sys:"
mount | grep -E '^(sysfs|[^ ]+) on /sys ' | sed 's/^/  /' | while read -r line; do bashio::log.info "$line"; done

# ----- Decide the write method ---------------------------------------------
# In order of preference:
#   1. Container's /sys is writable (would be surprising on HAOS but trivial).
#   2. nsenter into host mount namespace (expected path on HAOS).
# Reads always go through the container's /sys (ro bind-mount supports them).
CONTAINER_FAN="/sys/class/fan"
CONTAINER_THERMAL="/sys/class/thermal/thermal_zone0"
# Path visible from INSIDE the host's mount namespace:
HOST_NS_FAN="/sys/class/fan"
HOST_NS_THERMAL="/sys/class/thermal/thermal_zone0"

WRITE_METHOD=""

probe_direct_write() {
    local path="$1"
    [ -w "${path}/enable" ] || return 1
    local cur
    cur=$(cat "${path}/enable" 2>/dev/null) || return 1
    (echo "$cur" > "${path}/enable") 2>/dev/null || return 1
    return 0
}

probe_nsenter_write() {
    local path="$1"
    # Read current value through container (ro sysfs reads are fine).
    local cur
    cur=$(cat "${path}/enable" 2>/dev/null) || return 1
    # Attempt a no-op write through the host mount namespace.
    nsenter -t 1 -m -- sh -c "echo $cur > $path/enable" 2>/dev/null || return 1
    return 0
}

if [ -d "$CONTAINER_FAN" ]; then
    if probe_direct_write "$CONTAINER_FAN"; then
        WRITE_METHOD="direct"
        bashio::log.info "Write method: direct ($CONTAINER_FAN is rw)"
    elif probe_nsenter_write "$CONTAINER_FAN"; then
        WRITE_METHOD="nsenter"
        bashio::log.info "Write method: nsenter (writes routed through host mount namespace)"
    else
        bashio::log.error "Neither direct nor nsenter writes to ${CONTAINER_FAN}/enable worked."
        bashio::log.error "Diagnosing:"
        bashio::log.error "  nsenter availability: $(command -v nsenter || echo 'NOT FOUND')"
        bashio::log.error "  Host PID 1 visible:  $(test -r /proc/1/ns/mnt && echo yes || echo NO)"
        bashio::log.error "  /sys on host:"
        nsenter -t 1 -m -- mount 2>/dev/null | grep -E '^(sysfs|[^ ]+) on /sys ' | sed 's/^/    /' || bashio::log.error "    (nsenter failed or host /sys not visible)"
        # Fall through anyway — Python will publish temp reads and report
        # errors on any write attempt.
        WRITE_METHOD="direct"
    fi
fi

FAN_PATH=""
THERMAL_PATH=""

if [ -d "$CONTAINER_FAN" ]; then
    SYSFS_MODE="khadas"
    FAN_PATH="$CONTAINER_FAN"
    bashio::log.info "Detected legacy Khadas fan driver; FAN_PATH=$FAN_PATH"
elif [ -f "$CONTAINER_THERMAL/trip_point_3_temp" ]; then
    SYSFS_MODE="thermal"
    THERMAL_PATH="$CONTAINER_THERMAL"
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
fan_write() {
    local file="$1" value="$2"
    case "$WRITE_METHOD" in
        nsenter)
            nsenter -t 1 -m -- sh -c "echo $value > /sys/class/fan/$file" 2>&1
            ;;
        *)
            echo "$value" > "${FAN_PATH}/${file}" 2>&1
            ;;
    esac
}

if [ "$SYSFS_MODE" = "khadas" ]; then
    for level in low mid high; do
        cfg_key="trigger_temp_${level}"
        cfg_val=$(bashio::config "$cfg_key")
        if out=$(fan_write "trigger_temp_${level}" "$cfg_val"); then
            bashio::log.info "Set trigger_temp_${level} = ${cfg_val} (via ${WRITE_METHOD:-direct})"
        else
            bashio::log.warning "Failed to write trigger_temp_${level}: ${out}"
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
    --write-method "${WRITE_METHOD:-direct}" \
    --log-level "$LOG_LEVEL"
