import os
import wifi
import socketpool
import adafruit_minimqtt.adafruit_minimqtt as MQTT

# Credentials are loaded from settings.toml
SSID          = os.getenv("WIFI_SSID")
PASSWORD      = os.getenv("WIFI_PASSWORD")
MQTT_HOST     = os.getenv("MQTT_HOST")
MQTT_PORT     = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC    = os.getenv("MQTT_TOPIC", "sms/incoming")
MQTT_USERNAME = os.getenv("MQTT_USERNAME", None)
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", None)


def on_message(client, topic, message):
    print(f"SMS received [{topic}]: {message}")

    # --- Add your logic here ---
    # Example: toggle a pin based on message content
    # import board, digitalio
    # led = digitalio.DigitalInOut(board.LED)
    # led.direction = digitalio.Direction.OUTPUT
    # if message.strip().lower() == "on":
    #     led.value = True
    # elif message.strip().lower() == "off":
    #     led.value = False


def connect_wifi():
    print(f"Connecting to WiFi: {SSID}")
    wifi.radio.connect(SSID, PASSWORD)
    print(f"Connected, IP: {wifi.radio.ipv4_address}")


def create_mqtt_client(pool):
    client = MQTT.MQTT(
        broker=MQTT_HOST,
        port=MQTT_PORT,
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD,
        socket_pool=pool,
    )
    client.on_message = on_message
    return client


connect_wifi()
pool = socketpool.SocketPool(wifi.radio)
mqtt = create_mqtt_client(pool)

print("Connecting to MQTT broker...")
mqtt.connect()
mqtt.subscribe(MQTT_TOPIC)
print(f"Subscribed to: {MQTT_TOPIC}")

while True:
    try:
        mqtt.loop(timeout=1)
    except Exception as e:
        print(f"MQTT error: {e} — reconnecting...")
        try:
            mqtt.reconnect()
        except Exception as e2:
            print(f"Reconnect failed: {e2}")
