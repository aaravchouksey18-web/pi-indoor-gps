"""pi-indoor-gps collector.

Subscribes to indoor/sighting, keeps a rolling per-node RSSI view of every
device, and — when a target device is configured and spots.json exists —
publishes indoor/estimate for it.

Run:
    python3 collector.py                  # just watch what nodes report
    python3 collector.py --target aa:bb:..  # + estimate the target's spot
    python3 collector.py --boards a,b --target aa:bb:..  # fleet allowlist

The rolling window is intentionally simple for v1: each node's sighting
updates (board, mac) with the latest RSSI and a timestamp; a stale window
(older than WINDOW_S) drops the node from the live vector.

Hardening in this version:
  * resubscribes on every MQTT connect (on_connect) — a broker restart can
    no longer leave the collector permanently deaf with zero diagnostics;
  * payloads are validated by messages.parse_sighting() before touching any
    state, so a malformed/hostile message can't crash or poison the data;
  * --boards filters sightings down to the nodes you configured — a
    misconfiguration guard, NOT an auth boundary (any LAN host can author
    sightings on an open broker; secure the broker if that matters);
  * estimates only publish when the matcher is unambiguous (full fingerprint
    coverage + distance ceiling + margin + a live vector at least as wide as
    the map's widest spot), and carry a units tag so "distance"
    can't be mistaken for metres (it is the Euclidean RSSI distance in dB,
    not a signal level).
"""
import argparse
import json
import os
import time

import paho.mqtt.client as mqtt

from fingerprint import best_match, format_vector, load_spots
from messages import normalize_mac, parse_sighting

# How long a node's RSSI stays fresh. The firmware sniffs ~15 s then reports
# ~30 s (a ~45 s duty cycle); a node that hears the target late can blow any
# single window, and under exact-set-equality ONE stale node turns every
# estimate into "coverage". 2x the duty cycle keeps one slow node from
# killing the whole vector.
WINDOW_S = 90.0

TOPIC_SIGHTING = "indoor/sighting"
TOPIC_ESTIMATE = "indoor/estimate"


class State:
    def __init__(self, target=None, boards=None):
        self.target = target          # lowercase mac, or None
        self.boards = boards or None  # allowed board ids, None = any
        self.nodes = {}               # board -> {mac: {"rssi": int, "ts": float}}
        self.spots = load_spots()

    def allowed(self, board):
        return self.boards is None or board in self.boards

    def prune(self):
        now = time.monotonic()
        for board in list(self.nodes):
            for mac in list(self.nodes[board]):
                if now - self.nodes[board][mac]["ts"] > WINDOW_S:
                    del self.nodes[board][mac]
            if not self.nodes[board]:
                del self.nodes[board]

    def live_vector(self, mac):
        """{board: rssi} for one target across currently-fresh nodes."""
        now = time.monotonic()
        vec = {}
        for board, devs in self.nodes.items():
            if mac in devs and now - devs[mac]["ts"] <= WINDOW_S:
                vec[board] = devs[mac]["rssi"]
        return vec


def make_client(state, min_boards=1):
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # paho's auto-reconnect does NOT restore subscriptions: without this,
        # a broker restart would silently kill every estimate.
        if reason_code != 0:
            print(f"collector: connect refused (reason_code={reason_code}); "
                  f"not subscribed", flush=True)
            return
        client.subscribe(TOPIC_SIGHTING)
        print("collector: connected; subscribed to indoor/sighting", flush=True)

    def on_message(client, userdata, msg):
        s = parse_sighting(msg.payload)
        if s is None:
            return
        if not state.allowed(s["board"]):
            return
        state.nodes.setdefault(s["board"], {})[s["mac"]] = {"rssi": s["rssi"],
                                                            "ts": time.monotonic()}
        state.prune()
        if state.target and s["mac"] == state.target:
            vec = state.live_vector(s["mac"])
            if len(vec) < min_boards:
                print(f"[match] {s['mac']} vector {format_vector(vec)}: "
                      f"only {len(vec)} board(s) fresh (< {min_boards})",
                      flush=True)
                return
            m = best_match(vec, state.spots)
            if m.spot is None:
                reason = {
                    "no_map": "no spots map (run calibrate.py first)",
                    "coverage": ("live board set matches no spot (a node "
                                 "the map expects is silent, or an "
                                 "unexpected node is live)"),
                    "partial_fleet": ("live board set is narrower than the "
                                      "map's widest spot (fleet nodes are "
                                      "silent — re-check the sniffer fleet)"),
                    "distance": "closest spot beyond the distance ceiling",
                    "ambiguous": "two spots too close to call (margin)",
                    "no_live": "empty live vector",
                }.get(m.reason, "no match")
                print(f"[match] {s['mac']} vector {format_vector(vec)}: "
                      f"no match ({reason})", flush=True)
                return
            est = {"board": s["board"], "mac": s["mac"], "vector": vec,
                   "spot": m.spot, "distance": m.distance, "units": "dB distance",
                   "confidence": m.confidence, "margin": m.margin,
                   "matched_boards": m.boards, "ts": round(time.time(), 3)}
            client.publish(TOPIC_ESTIMATE, json.dumps(est))
            print("[estimate]", json.dumps(est), flush=True)
        else:
            print(f"[sighting] {s['board']} {s['mac']} rssi={s['rssi']}",
                  flush=True)

    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    return mqttc


def main(argv=None):
    ap = argparse.ArgumentParser(description="pi-indoor-gps collector")
    ap.add_argument("--target", default=None,
                    help="target device MAC to position (any case)")
    ap.add_argument("--boards", default=None,
                    help="comma-separated board allowlist; empty = all nodes")
    ap.add_argument("--min-boards", type=int, default=1,
                    help="minimum fresh boards before an estimate publishes")
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--mqtt-username",
                    default=os.environ.get("MQTT_USER"),
                    help="MQTT username if the broker requires auth")
    ap.add_argument("--mqtt-password",
                    default=os.environ.get("MQTT_PASS"),
                    help="MQTT password (used with --mqtt-username)")
    args = ap.parse_args(argv)

    target = normalize_mac(args.target) if args.target else None
    if args.target and target is None:
        print(f"warning: --target {args.target!r} is not a valid MAC; "
              "running as a passive observer", flush=True)
    if args.min_boards < 1:
        ap.error("--min-boards must be >= 1")
    if not (1 <= args.port <= 65535):
        ap.error("--port must be 1..65535")

    boards = None
    if args.boards:
        boards = {b.strip() for b in args.boards.split(",") if b.strip()}
        if boards:
            print(f"allowlist: only boards {sorted(boards)} are trusted",
                  flush=True)
        else:
            # "--boards ," parses to an empty set, which State() then treats
            # as allow-all — say so instead of printing "only boards []".
            print("--boards resolved to an empty set (only commas/blank "
                  "entries) — treating it as ALL nodes; re-run with real "
                  "board ids if that wasn't the intent", flush=True)

    state = State(target=target, boards=boards)
    if state.spots:
        print(f"map: {len(state.spots)} spot(s) loaded once at startup from "
              "spots.json — recalibrating while the collector runs requires "
              "a restart", flush=True)
    mqttc = make_client(state, min_boards=args.min_boards)
    if args.mqtt_username:
        mqttc.username_pw_set(args.mqtt_username, args.mqtt_password)
        print("mqtt auth: username configured", flush=True)

    while True:                     # retry broker-down at startup
        try:
            mqttc.connect(args.host, args.port, 30)
            break
        except (OSError, ValueError) as e:
            # ValueError covers paho's "Invalid host." (e.g. MQTT_HOST=""),
            # which is NOT an OSError and used to crash with a raw traceback
            print(f"warning: broker {args.host}:{args.port} unavailable "
                  f"({e}); retrying in 5 s (Ctrl-C to abort)", flush=True)
            time.sleep(5)
    print(f"collector on {args.host}:{args.port}, target={target or 'any'}")
    mqttc.loop_forever()


if __name__ == "__main__":
    main()