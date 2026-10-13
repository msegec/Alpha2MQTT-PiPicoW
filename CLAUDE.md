# Alpha2MQTT agent guide

Raspberry Pi Pico W firmware that bridges an AlphaESS inverter (Modbus RTU over
RS485) to MQTT, plus a Home Assistant package in `Home Assistant/`.

## Public boundary

This repository is a public fork. It carries code and generic documentation
only: no IP addresses, hostnames, real entity names, house data, logs or
paths. Private operational notes live in a separate private repo. Agents with
masterrulez installed apply `~/masterrulez/docs/internals/public-source-safety.md`
before every push. Credentials live in the gitignored `Alpha2MQTT/Secrets.h`;
build from it, never print or copy it.

## Live hardware

A deployed unit controls a real battery. Talk to it through read topics only.
Publishing to `<device>/request/...`, flashing and OTA are Mark's call, every
time.

## Build and test

- Host tests: `tests/firmware/run`. Pure logic the firmware shares with the
  tests goes in headers like `Alpha2MQTT/Dispatch.h`, with no Arduino types.
- Firmware: the `arduino-cli compile` line in `README.md`. It needs
  `-include pgmspace.h`. Send build output to a private temp dir: the binary
  embeds the WiFi and MQTT credentials.

## Anchors

Find code by symbol; line numbers drift. `mqttCallback` in `Alpha2MQTT.ino`
parses every request. Its `setCharge` and `setDischarge` branches write the
dispatch block in this order: START 0x0880, ACTIVE_POWER 0x0881, TIME 0x0887,
SOC 0x0886, MODE 0x0885. Register defines are in `Definitions.h`.
