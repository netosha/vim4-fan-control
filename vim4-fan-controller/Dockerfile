ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM $BUILD_FROM

# - python3 / py3-paho-mqtt: MQTT bridge runtime.
# - util-linux: provides nsenter so we can enter the host's mount namespace
#   and write to /sys/class/fan/* on the real host sysfs (the container's
#   /sys is bind-mounted read-only and the propagation lock makes remount
#   impossible).
RUN apk add --no-cache python3 py3-paho-mqtt bash util-linux

WORKDIR /app
COPY run.sh /run.sh
COPY fan_mqtt.py /app/fan_mqtt.py
RUN chmod a+x /run.sh

# Labels populated by Supervisor at build time (safe to leave empty when building locally).
ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_REF
ARG BUILD_VERSION

LABEL \
    io.hass.name="VIM4 Fan Controller" \
    io.hass.description="Native fan control for Khadas VIM4 via /sys/class/fan" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="<your name> <dmitriypru@gmail.com>" \
    org.opencontainers.image.title="VIM4 Fan Controller" \
    org.opencontainers.image.description="Native fan control for Khadas VIM4 via /sys/class/fan" \
    org.opencontainers.image.source="https://github.com/netosha/vim4-fan-control" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.created=${BUILD_DATE} \
    org.opencontainers.image.revision=${BUILD_REF} \
    org.opencontainers.image.version=${BUILD_VERSION}

CMD [ "/run.sh" ]
