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
    ap.add_argument("--mqtt-username",
                    default=os.environ.get("MQTT_USER"),
                    help="MQTT username if the broker requires auth")
    ap.add_argument("--mqtt-password",
                    default=os.environ.get("MQTT_PASS"),
                    help="MQTT password (used with --mqtt-username)")
    args = ap.parse_args(argv)

    mac = normalize_mac(args.target)
    if mac is None:
        ap.error(f"--target {args.target!r} is not a valid MAC address")
    if args.jitter < 0:
        ap.error("--jitter must be >= 0")
    if not (-150 <= args.rssi <= 0):
        ap.error("--rssi must be within [-150, 0] dBm (collectors reject "
                 "values outside this band)")
    if args.count < 1:
        ap.error("--count must be >= 1")
    if args.interval <= 0:
        ap.error("--interval must be > 0")
    if not (1 <= args.port <= 65535):
        ap.error("--port must be 1..65535")
    board = (args.board or "").strip()
    if not board or board == "_meta":
        ap.error("--board must be a non-empty id (and not \"_meta\")")
    if len(board) > 16:
        # mirror messages.py's 16-char cap and the firmware static_assert:
        # a longer board id publishes sightings NOBODY accepts
        ap.error("--board must be at most 16 chars (the collector and "
                 "firmware both enforce this cap)")
    args.board = board

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if args.mqtt_username:
        mqttc.username_pw_set(args.mqtt_username, args.mqtt_password)
        print("mqtt auth: username configured", flush=True)
    try:
        mqttc.connect(args.host, args.port, 30)
    except (OSError, ValueError) as e:
        # ValueError covers paho's "Invalid host." for e.g. MQTT_HOST=""
        sys.exit(f"cannot reach broker {args.host}:{args.port}: {e}")
    mqttc.loop_start()

    print(f"[inject] {mac} rssi={args.rssi} "
          f"({'+-' + str(args.jitter) + ' dB ' if args.jitter else ''}"
          f"x{args.count} every {args.interval}s via {args.board}",
          flush=True)

    sent = 0
    for i in range(args.count):
        rssi = args.rssi + random.randint(-args.jitter, args.jitter) if args.jitter else args.rssi
        # clamp into the [-150, 0] dBm band the collector accepts, so jitter
        # near the edges still produces observable (non-dropped) samples
        rssi = max(-150, min(0, rssi))
        payload = {"board": args.board, "mac": mac, "rssi": rssi, "rssi_n": 1}
        info = mqttc.publish(TOPIC_SIGHTING, json.dumps(payload))
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"warning: publish #{i} failed (rc={info.rc})", flush=True)
        else:
            sent += 1
        time.sleep(args.interval)
    # paho's loop thread does the actual socket write in the background: rc
    # == SUCCESS only means "queued in the client", so give it a moment to
    # flush before tearing the loop down (a fast --interval used to lose the
    # last packets while the summary still claimed "delivered N/M").
    time.sleep(max(0.3, args.interval))
    mqttc.loop_stop()
    try:
        mqttc.disconnect()
    except OSError:
        pass  # broker already gone; nothing left to flush
    print(f"[inject] queued {sent}/{args.count} to the broker", flush=True)


if __name__ == "__main__":
    main()