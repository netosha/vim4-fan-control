# VIM4 HAOS Tools

Home Assistant add-on repository containing tools for Khadas VIM4 single-board
computers running Home Assistant OS.

## Add-ons

| Add-on | Description |
| --- | --- |
| [**VIM4 Fan Controller**](vim4-fan-controller/) | Native fan control via `/sys/class/fan/*` exposed over MQTT Discovery. |

## Install this repository

In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ (top right) →
Repositories**, then paste this repository's URL and click **Add**. The
add-ons above will appear in the store under a "VIM4 HAOS Tools" section.

## Why this exists

Home Assistant OS for Khadas VIM4 is real (`vim4-haos-15.2.img.xz` from
`dl.khadas.com/products/vim4/firmware/home-assistant/`, built on the legacy
Amlogic 5.4/5.15 kernel), but the Core container is sandboxed off from the
host's `/sys` tree where the Khadas fan driver lives. A pure
`custom_components/` integration can't reach the driver, and `shell_command`
can't either. A privileged add-on with `full_access: true` is the only
officially-sanctioned path — see the [VIM4 Fan Controller
README](vim4-fan-controller/README.md) for details.

The existing `Nicooow/homeassistant-khadas-tools` repo targets VIM3 (a
different SoC, different kernel, and VIM3 HAOS is missing the fan driver
entirely), so it isn't reusable on VIM4 — only the general add-on packaging
shape is.

## License

MIT.
