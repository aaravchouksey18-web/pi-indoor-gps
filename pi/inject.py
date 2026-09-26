"""Inject synthetic indoor/sighting payloads — dev/test harness.

Publishes to the mosquitto broker exactly like a node would, so calibrate.py
and collector.py exercise the real code paths even when no probe-emitting
device is handy (e.g. the calibration phone isn't around). Useful for
regression-testing the fingerprint pipeline.

Usage:
    python3 inject.py --target de:ad:be:ef:00:01 --rssi -45 --count 20 --interval 5
"""
import argparse
import json
import os
import time

import paho.mqtt.client as mqtt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="device MAC to impersonate")
    ap.add_argument("--rssi", type=int, required=True, help="signal strength, dBm")
    ap.add_argument("--board", default="b")
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args()

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.connect(args.host, args.port, 30)
    mqttc.loop_start()

    payload = {"board": args.board, "mac": args.target,
               "rssi": args.rssi, "rssi_n": 1}
    print(f"[inject] {args.target} rssi={args.rssi} x{args.count} "
          f"every {args.interval}s", flush=True)
    for i in range(args.count):
        mqttc.publish("indoor/sighting", json.dumps(payload))
        time.sleep(args.interval)
    mqttc.loop_stop()


if __name__ == "__main__":
    main()