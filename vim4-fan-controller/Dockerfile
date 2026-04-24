ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM $BUILD_FROM

# - python3 / py3-paho-mqtt: MQTT bridge runtime.
# - openssh-client: for shelling writes out to the HAOS host on port
#   22222 (the sanctioned way out of the add-on sysfs jail).
RUN apk add --no-cache python3 py3-paho-mqtt bash openssh-client

WORKDIR /app
COPY run.sh /run.sh
COPY fan_mqtt.py /app/fan_mqtt.py
RUN chmod a+x /run.sh

ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_REF
ARG BUILD_VERSION

LABEL \
    io.hass.name="VIM4 Fan Controller" \
    io.hass.description="Control the Khadas VIM4 fan from Home Assistant via SSH to the HAOS host" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="netosha <dmitriypru@gmail.com>" \
    org.opencontainers.image.title="VIM4 Fan Controller" \
    org.opencontainers.image.description="Control the Khadas VIM4 fan from Home Assistant" \
    org.opencontainers.image.source="https://github.com/netosha/vim4-fan-control" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.created=${BUILD_DATE} \
    org.opencontainers.image.revision=${BUILD_REF} \
    org.opencontainers.image.version=${BUILD_VERSION}

CMD [ "/run.sh" ]
