"""Inject synthetic indoor/sighting payloads — dev/test harness.

Publishes to the mosquitto broker exactly like a node would, so calibrate.py
and collector.py exercise the real code paths even when no probe-emitting
device is handy (e.g. the calibration phone isn't around). Useful for
regression-testing the fingerprint pipeline.

Usage:
    python3 inject.py --target de:ad:be:ef:00:01 --rssi -45 --count 20 --interval 5
    python3 inject.py --target de:ad:be:ef:00:01 --rssi -58 --jitter 6  # an RF wobble
"""
import argparse
import json
import os
import random
import sys
import time

import paho.mqtt.client as mqtt

from messages import normalize_mac

TOPIC_SIGHTING = "indoor/sighting"


def main(argv=None):
    ap = argparse.ArgumentParser(description="synthetic sighting injector")
    ap.add_argument("--target", required=True, help="device MAC to impersonate")
    ap.add_argument("--rssi", type=int, required=True, help="signal strength, dBm")
    ap.add_argument("--jitter", type=int, default=0,
                    help="+/- dBm noise to add per sample (simulates RF wobble)")
    ap.add_argument("--board", default="b")
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args(argv)

    mac = normalize_mac(args.target)
    if mac is None:
        ap.error(f"--target {args.target!r} is not a valid MAC address")
    if args.jitter < 0:
        ap.error("--jitter must be >= 0")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    try:
        mqttc.connect(args.host, args.port, 30)
    except OSError as e:
        sys.exit(f"cannot reach broker {args.host}:{args.port}: {e}")
    mqttc.loop_start()

    print(f"[inject] {mac} rssi={args.rssi} "
          f"({'+-' + str(args.jitter) + ' dB ' if args.jitter else ''}"
          f"x{args.count} every {args.interval}s via {args.board}",
          flush=True)

    sent = 0
    for i in range(args.count):
        rssi = args.rssi + random.randint(-args.jitter, args.jitter) if args.jitter else args.rssi
        payload = {"board": args.board, "mac": mac, "rssi": rssi, "rssi_n": 1}
        info = mqttc.publish(TOPIC_SIGHTING, json.dumps(payload))
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"warning: publish #{i} failed (rc={info.rc})", flush=True)
        else:
            sent += 1
        time.sleep(args.interval)
    mqttc.loop_stop()
    print(f"[inject] delivered {sent}/{args.count}", flush=True)


if __name__ == "__main__":
    main()