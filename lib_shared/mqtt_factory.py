"""Factory for the platform MQTT client.

Selects the implementation from the MQTT_CLIENT config value: "adafruit" uses
the Adafruit_IO library (the Flask server on Heroku), anything else — including
an unset value — uses paho-mqtt (local dev and the Raspberry Pi, which talks to
io.adafruit.com directly over TLS). The chosen module is imported lazily so
each platform only needs its own MQTT dependency installed.
"""

from lib_shared.config_reader import get_config


def make_mqtt_client(dispatch_callback):
    """Build the MQTT client for this platform.

    Args:
        dispatch_callback: Callable that accepts a raw MQTT payload string.
    """
    which = (get_config().if_exists("MQTT_CLIENT") or "paho").lower()
    if which == "adafruit":
        from lib_shared.adafruit_mqtt_client import AdafruitMqttClient

        return AdafruitMqttClient(dispatch_callback=dispatch_callback)
    from lib_shared.paho_mqtt_client import PahoMqttClient

    return PahoMqttClient(dispatch_callback=dispatch_callback)
