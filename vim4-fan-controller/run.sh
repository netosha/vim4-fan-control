#!/usr/bin/with-contenv bashio
# Entry point for the VIM4 Fan Controller add-on.
#
# Strategy:
#   - Reads are fine through the container's ro bind-mount of /sys.
#   - Writes are routed over SSH to the HAOS host on port 22222, which
#     bypasses the mount-propagation lock that prevents any remount of
#     /sys rw inside an add-on container.
#
# On first run the add-on auto-generates an ed25519 key and logs the
# public key. The user appends it to /root/.ssh/authorized_keys on the
# HAOS host (via port 22222), then restarts the add-on.

set -e

bashio::log.info "Starting VIM4 Fan Controller..."

# ----- Preflight checks -----------------------------------------------------
if [ ! -d /sys/class/fan ]; then
    bashio::log.fatal "/sys/class/fan is not present. Is the Khadas fan driver loaded?"
    exit 1
fi

if ! bashio::services.available "mqtt"; then
    bashio::log.fatal "No MQTT service configured in Home Assistant."
    bashio::log.fatal "Install the Mosquitto add-on (or connect an external broker)."
    exit 1
fi

# ----- Load config ----------------------------------------------------------
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

SSH_HOST=$(bashio::config 'ssh_host')
SSH_PORT=$(bashio::config 'ssh_port')
SSH_USER=$(bashio::config 'ssh_user')

# ----- SSH key setup --------------------------------------------------------
SSH_DIR=/data/ssh
KEY_PATH="$SSH_DIR/id_ed25519"
PUB_PATH="$SSH_DIR/id_ed25519.pub"
KNOWN_HOSTS="$SSH_DIR/known_hosts"

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

if [ ! -f "$KEY_PATH" ]; then
    bashio::log.info "Generating SSH keypair in $SSH_DIR ..."
    ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "vim4-fan-addon" -q
    chmod 600 "$KEY_PATH"
    chmod 644 "$PUB_PATH"
fi

# Make the public key readable from the HA UI's file editor by copying
# it into /share. Nothing sensitive here — a public key is meant to be
# shared.
if [ -d /share ]; then
    cp "$PUB_PATH" /share/vim4-fan-ssh-pubkey.txt 2>/dev/null || true
fi

PUB_KEY=$(cat "$PUB_PATH")

bashio::log.info ""
bashio::log.info "================================================================="
bashio::log.info "  VIM4 fan add-on SSH public key (paste on HAOS host):"
bashio::log.info ""
bashio::log.info "  $PUB_KEY"
bashio::log.info ""
bashio::log.info "  Also saved to: /share/vim4-fan-ssh-pubkey.txt"
bashio::log.info "================================================================="
bashio::log.info ""

# ----- Probe SSH ------------------------------------------------------------
SSH_ENABLED=false
bashio::log.info "Probing SSH to ${SSH_USER}@${SSH_HOST}:${SSH_PORT} ..."
if ssh -i "$KEY_PATH" \
       -p "$SSH_PORT" \
       -o BatchMode=yes \
       -o StrictHostKeyChecking=accept-new \
       -o UserKnownHostsFile="$KNOWN_HOSTS" \
       -o ConnectTimeout=5 \
       "$SSH_USER@$SSH_HOST" \
       'test -w /sys/class/fan/enable' 2>/tmp/ssh_err; then
    bashio::log.info "SSH control: ENABLED (host /sys/class/fan/enable is writable)"
    SSH_ENABLED=true
else
    bashio::log.warning "SSH control: DISABLED"
    bashio::log.warning "   reason: $(cat /tmp/ssh_err 2>/dev/null || echo 'unknown')"
    bashio::log.warning ""
    bashio::log.warning "To enable fan control:"
    bashio::log.warning "   1. SSH to your HAOS host:  ssh -p 22222 root@<vim4-ip>"
    bashio::log.warning "   2. Append the public key above to /root/.ssh/authorized_keys"
    bashio::log.warning "   3. Restart this add-on"
    bashio::log.warning ""
    bashio::log.warning "Running in monitor-only mode in the meantime."
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
    --ssh-enabled "$SSH_ENABLED" \
    --ssh-host "$SSH_HOST" \
    --ssh-port "$SSH_PORT" \
    --ssh-user "$SSH_USER" \
    --ssh-key "$KEY_PATH" \
    --ssh-known-hosts "$KNOWN_HOSTS" \
    --log-level "$LOG_LEVEL"
