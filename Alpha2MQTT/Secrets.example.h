// Copy this file to Secrets.h and fill in your details. Secrets.h is ignored by git.
#ifndef _Secrets_h
#define _Secrets_h

// WiFi. A Pico W joins 2.4 GHz WPA2 networks only.
#define WIFI_SSID		"your-ssid"
#define WIFI_PASSWORD	"your-wifi-password"

// MQTT broker
#define MQTT_SERVER	"192.0.2.10"
#define MQTT_PORT	1883
#define MQTT_USERNAME	""				// Empty string to connect without credentials.
#define MQTT_PASSWORD	""

// Over the air updates (Pico W). Anyone who knows this can replace the firmware, so make it
// long and random, for example: python3 -c "import secrets; print(secrets.token_urlsafe(24))"
// scripts/ota-upload reads it from here at run time.
#define OTA_PASSWORD	"change-me"

#endif
