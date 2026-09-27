#!/usr/bin/env python3
"""Tests for the pi/ scripts (stdlib-only: python3 tests/test_pi.py, or
python3 -m unittest discover -s tests)."""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))

import fingerprint  # noqa: E402
import messages    # noqa: E402
import collector   # noqa: E402


def write_map(tmp, spots):
    with open(os.path.join(tmp, "spots.json"), "w") as fh:
        json.dump({"spots": spots}, fh)
    return os.path.join(tmp, "spots.json")


def sighting(board="a", mac="aa:bb:cc:dd:ee:ff", rssi=-45):
    """A payload shaped exactly like the firmware's serializeJson() output."""
    return json.dumps({"board": board, "mac": mac, "rssi": rssi}).encode()


class LoadSpotsTest(unittest.TestCase):
    def test_missing_file_is_empty_map(self):
        self.assertEqual(fingerprint.load_spots("/nonexistent.json"), {})

    def test_corrupt_json_is_empty_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spots.json")
            with open(path, "w") as fh:
                fh.write("{ not json")
            self.assertEqual(fingerprint.load_spots(path), {})

    def test_deeply_nested_corrupt_map_never_raises(self):
        # json.load() of a deeply nested missing-the-map file can hit the
        # interpreter recursion limit (RecursionError) — for the local map
        # that must still mean "no map", not "collector died".
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spots.json")
            with open(path, "w") as fh:
                fh.write('{"spots": ' + "[" * 1200 + "]" * 1200 + "}")
            self.assertEqual(fingerprint.load_spots(path), {})

    def test_bad_top_level_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spots.json")
            with open(path, "w") as fh:
                json.dump({"not_spots": {"x": {}}}, fh)
            self.assertEqual(fingerprint.load_spots(path), {})

    def test_non_dict_spot_records_are_dropped(self):
        # this exact map used to crash the collector inside the MQTT callback
        with tempfile.TemporaryDirectory() as tmp:
            path = write_map(tmp, {"hall": 5, "kitchen": {"a": "near"}, "ok": {"a": -58}})
            spots = fingerprint.load_spots(path)
            self.assertEqual(spots, {"ok": {"a": -58.0}})

    def test_non_finite_and_string_rssi_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_map(tmp, {
                "bad": {"a": "NaN", "b": "1e999", "c": "oops"},
                "good": {"a": -58}})
            spots = fingerprint.load_spots(path)
            self.assertEqual(spots, {"good": {"a": -58.0}})

    def test_huge_integer_literal_never_raises(self):
        # an unquoted 400-digit integer in spots.json reaches float() and
        # raises OverflowError unless caught; must be dropped, not thrown
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spots.json")
            with open(path, "w") as fh:
                fh.write('{"spots": {"kitchen": {"a": ' + "9" * 400 + "}}}")
            self.assertEqual(fingerprint.load_spots(path), {})

    def test_best_match_sanitizes_hostile_raw_map(self):
        # best_match() with a hand-built map must not trust it blindly
        m = fingerprint.best_match({"a": -58},
                                   spots={"s": {"a": "near"}, "list": ["x"]})
        self.assertIsNone(m.spot)
        m = fingerprint.best_match(
            {"a": -58}, spots={"s": {"a": "123456789" * 60}})
        self.assertIsNone(m.spot)

    def test_best_match_never_crashes_on_hostile_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_map(tmp, {"hall": 5, "kitchen": {"a": "near"}})
            m = fingerprint.best_match({"a": -58}, spots=fingerprint.load_spots(path))
            self.assertIsNone(m.spot)

    def test_huge_but_finite_spot_rssi_refuses_not_crashes(self):
        # 1e200 is FINITE, so the sanitizer keeps it; squaring the diff would
        # raise OverflowError unless computed with x*x (overflow -> inf). The
        # matcher must come back "distance", never crash the process.
        m = fingerprint.best_match(
            {"a": -58, "b": -70},
            spots={"kitchen": {"a": 1e200, "b": -70}})
        self.assertIsNone(m.spot)
        self.assertEqual(m.reason, "distance")

    def test_runner_up_overflow_does_not_crash(self):
        # only the runner-up overflows to inf (a hostile 1e200 rssi in the
        # map): the winner stays finite and truly matches. There must be no
        # OverflowError and no Infinity leaking into the payload (margin
        # stays None when the gap is not finite).
        m = fingerprint.best_match(
            {"a": -58, "b": -70},
            spots={"far": {"a": -60, "b": -70}, "huge": {"a": 1e200, "b": -70}})
        self.assertEqual(m.spot, "far")
        self.assertIsNone(m.reason)
        self.assertIsNone(m.margin)


class MatchingTest(unittest.TestCase):
    def test_full_coverage_required(self):
        # coverage runs both ways: a 2-board fingerprint is not scored against
        # a 3-board live vector, and a candidate that ignores a live board is
        # dropped too (only the spot that explains every live board scores)
        spots = {"full": {"a": -60, "b": -70},
                 "incomplete": {"a": -58, "b": -75, "d": -80},
                 "all3": {"a": -58, "b": -70, "c": -40}}
        scored = fingerprint.compute_distances({"a": -58, "b": -70, "c": -40}, spots)
        self.assertEqual([s[0] for s in scored], ["all3"])

    def test_narrow_fingerprint_cannot_ignore_contradicting_node(self):
        # pass-4 HIGH: live a=-59 b=-90 contradicts the wide spot by 20 dB on
        # board b; the 1-board "narrow" fingerprint must not win by ignoring
        # that board (it fails to explain every live board, so it is dropped)
        spots = {"narrow": {"a": -58}, "wide": {"a": -60, "b": -70}}
        scored = fingerprint.compute_distances({"a": -59, "b": -90}, spots)
        names = [s[0] for s in scored]
        self.assertNotIn("narrow", names)
        self.assertEqual(names, ["wide"])

    def test_refusal_reasons(self):
        # reasons distinguish the failure mode so the collector can tell the
        # user which knob to turn (no map vs no coverage vs too far vs close)
        self.assertEqual(fingerprint.best_match({}).reason, "no_live")
        self.assertEqual(fingerprint.best_match({"a": -50}, spots={}).reason,
                         "no_map")
        far = fingerprint.best_match({"a": -50},
                                     spots={"a": {"a": -110}, "b": {"a": -105}})
        self.assertEqual(far.reason, "distance")
        close = fingerprint.best_match({"a": -49},
                                       spots={"x": {"a": -58}, "y": {"a": -40}})
        self.assertEqual(close.reason, "ambiguous")
        unknown = fingerprint.best_match({"zz": -40}, spots={"s": {"a": -58}})
        self.assertEqual(unknown.reason, "coverage")
        ok = fingerprint.best_match({"a": -30},
                                    spots={"x": {"a": -58}, "y": {"a": -40}})
        self.assertIsNone(ok.reason)
        self.assertEqual(ok.spot, "y")

    def test_partial_fleet_refused(self):
        # loop-6 B1: under exact-set equality a 1-board live vector can only
        # be scored against the 1-board spot, so that narrow spot becomes the
        # SOLE candidate and would "win" with confidence 1.0 and no margin
        # check while every wider spot silently sits out. That must be
        # refused ("partial_fleet") instead of published.
        spots = {"living": {"a": -58, "b": -72}, "desk": {"a": -46, "b": -88},
                 "narrow": {"a": -50}}
        m = fingerprint.best_match({"a": -60}, spots=spots)
        self.assertIsNone(m.spot)
        self.assertEqual(m.reason, "partial_fleet")
        m = fingerprint.best_match({"a": -60, "b": -72}, spots=spots)
        self.assertIsNone(m.reason)          # full fleet still matches
        self.assertEqual(m.spot, "living")
        # a map with NO narrow spot has no candidate for a 1-board vector at
        # all — that stays plain "coverage", not a fleet-width problem
        self.assertEqual(fingerprint.best_match(
            {"a": -60}, spots={"living": {"a": -58, "b": -72}}).reason,
            "coverage")

    def test_partial_fleet_allows_equal_width_map(self):
        # a map where EVERY spot is 1-board has width 1, so a 1-board vector
        # is the full fleet for that map and must keep matching
        spots = {"desk": {"a": -45}, "couch": {"a": -75}}
        m = fingerprint.best_match({"a": -50}, spots=spots)
        self.assertIsNone(m.reason)
        self.assertEqual(m.spot, "desk")

    def test_distance_is_euclidean_not_squared(self):
        spots = {"s": {"a": -58}}
        scored = fingerprint.compute_distances({"a": -62}, spots)
        self.assertEqual(scored[0][1], 4.0)     # sqrt(16), not 16

    def test_ceiling_and_margin(self):
        spots = {"a": {"x": -110}, "b": {"x": -105}}
        # live -50: 60/55 dB from a/b -> both beyond MAX_DIST_DB (48) ceiling
        far = fingerprint.best_match({"x": -50}, spots=spots)
        self.assertIsNone(far.spot)
        # -- margin/ambiguity cases use a tight pair of spots -- #
        tight = {"a": {"x": -58}, "b": {"x": -40}}
        # live -49: equidistant (9 dB) from both -> margin 0 < MIN_MARGIN_DB
        close = fingerprint.best_match({"x": -49}, spots=tight)
        self.assertIsNone(close.spot)
        # live -30: clearly nearest to b (margin 18) -> unambiguous match
        hit = fingerprint.best_match({"x": -30}, spots=tight)
        self.assertEqual(hit.spot, "b")

    def test_margin_decision_uses_unrounded_gap(self):
        # true gap is 2.4999 (< MIN_MARGIN_DB) but rounds to 2.5: the
        # DECISION must refuse ("ambiguous"), not publish on the round.
        m = fingerprint.best_match(
            {"a": -60},
            spots={"win": {"a": -55.0}, "run": {"a": -52.5001}})
        self.assertIsNone(m.spot)
        self.assertEqual(m.reason, "ambiguous")


class MessagesTest(unittest.TestCase):
    def test_out_of_range_rssi_rejected(self):
        for rssi in (-151, 1, "abc", None):
            payload = json.dumps({"board": "a", "mac": "aa:bb:cc:dd:ee:ff", "rssi": rssi})
            self.assertIsNone(messages.parse_sighting(payload.encode()))

    def test_valid_sighting_normalized(self):
        payload = json.dumps({"board": "a", "mac": "AA:BB:CC:DD:EE:FF", "rssi": -45})
        s = messages.parse_sighting(payload.encode())
        self.assertEqual(s["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(s["rssi"], -45)

    def test_infinite_rssi_rejected_not_crash(self):
        # JSON 1e999 / -1e999 parse to float inf; int() then raises
        # OverflowError (not ValueError) and must be rejected, not thrown
        for rssi in (1e999, -1e999):
            payload = json.dumps({"board": "a", "mac": "aa:bb:cc:dd:ee:ff",
                                  "rssi": rssi})
            self.assertIsNone(messages.parse_sighting(payload.encode()))

    def test_bad_mac_rejected(self):
        for mac in ("nope", "aa:bb:cc:dd:ee", "AA:BB:CC:DD:EE:FF:00"):
            payload = json.dumps({"board": "a", "mac": mac, "rssi": -45})
            self.assertIsNone(messages.parse_sighting(payload.encode()))

    def test_meta_board_rejected(self):
        # "_meta" is reserved for fingerprint-map metadata; a live node
        # claiming it would pollute the collector's vector and break every
        # match (spot fingerprints never contain "_meta").
        for board in ("_meta", " _meta "):
            payload = json.dumps({"board": board,
                                  "mac": "aa:bb:cc:dd:ee:ff", "rssi": -45})
            self.assertIsNone(messages.parse_sighting(payload.encode()))

    def test_padded_board_is_stripped_not_dropped(self):
        payload = json.dumps({"board": "  kitchen  ",
                              "mac": "aa:bb:cc:dd:ee:ff", "rssi": -45})
        self.assertEqual(messages.parse_sighting(payload.encode())["board"],
                         "kitchen")

    def test_oversized_payload_rejected_before_parse(self):
        # loop-6 B2: a payload over MAX_SIGHTING_BYTES is rejected before any
        # json parsing — this is what makes the cheap "any LAN host can kill
        # the collector" DoS impossible, and it also caps parse memory.
        big = b" " * (messages.MAX_SIGHTING_BYTES + 1) + sighting()
        self.assertIsNone(messages.parse_sighting(big))

    def test_deeply_nested_under_cap_is_rejected_not_throw(self):
        # nested-but-small payloads parse fine as JSON but are not dicts;
        # under the size cap that is the (safe) path — must return None,
        # never raise (RecursionError would be a RuntimeError and was NOT
        # caught in earlier loops, letting paho re-raise and kill daemons).
        nested = b"[" * 200 + b"0" + b"]" * 200
        self.assertEqual(len(nested), 401)  # 200 brackets + "0" + 200 brackets
        self.assertLess(len(nested), messages.MAX_SIGHTING_BYTES)
        self.assertIsNone(messages.parse_sighting(nested))

    def test_non_dict_and_none_payloads(self):
        for raw in (None, b"null", b"[1,2]", b'"str"', b"", b"nope"):
            self.assertIsNone(messages.parse_sighting(raw))

    def test_firmware_payload_contract(self):
        # the payload the .ino actually builds (board/mac/rssi/rssi_n and an
        # optional ssid, lowercase colon MAC) must round-trip the collector —
        # a firmware-side rename would otherwise break the pipeline with zero
        # test failures.
        payload = json.dumps({"board": "a", "mac": "aa:bb:cc:dd:ee:ff",
                              "ssid": "homewifi", "rssi": -58, "rssi_n": 12})
        s = messages.parse_sighting(payload.encode())
        self.assertEqual(s, {"board": "a", "mac": "aa:bb:cc:dd:ee:ff",
                             "rssi": -58})


class CollectorStateTest(unittest.TestCase):
    """Collector.State windowing + the on_message estimate path."""

    def _state(self):
        st = collector.State(target="aa:bb:cc:dd:ee:ff", boards=None)
        st.spots = {}  # decouple from any on-disk spots.json
        return st

    def test_prune_drops_stale_nodes_only(self):
        st = self._state()
        base = 1000.0
        with mock.patch("collector.time.monotonic", return_value=base):
            st.nodes["a"] = {"11:22:33:44:55:66": {"rssi": -45,
                                                   "ts": base - 10}}
            st.nodes["b"] = {"22:33:44:55:66:77": {"rssi": -50,
                                                   "ts": base - collector.WINDOW_S - 1}}
        with mock.patch("collector.time.monotonic", return_value=base):
            st.prune()
        self.assertIn("a", st.nodes)
        self.assertNotIn("b", st.nodes)

    def test_window_boundary_inclusive(self):
        # exactly WINDOW_S old is still fresh; one step older is stale
        st = self._state()
        base = 1000.0
        with mock.patch("collector.time.monotonic", return_value=base):
            st.nodes["a"] = {"aa:bb:cc:dd:ee:ff": {"rssi": -45,
                                                   "ts": base - collector.WINDOW_S}}
            vec = st.live_vector("aa:bb:cc:dd:ee:ff")
        self.assertEqual(vec, {"a": -45})
        with mock.patch("collector.time.monotonic", return_value=base):
            st.nodes["a"]["aa:bb:cc:dd:ee:ff"]["ts"] = \
                base - collector.WINDOW_S - 0.001
            vec = st.live_vector("aa:bb:cc:dd:ee:ff")
        self.assertEqual(vec, {})

    def test_on_message_publishes_estimate_once_fleet_is_full(self):
        st = self._state()
        st.spots = {"living": {"a": -58.0, "b": -72.0}}
        mqttc = collector.make_client(st, min_boards=1)
        calls = []
        mqttc.publish = lambda topic, payload: (
            calls.append((topic, json.loads(payload))), SimpleNamespace(rc=0))[1]
        # board a only -> vec is 1-board, map's widest spot is 2-board:
        # partial_fleet, must NOT publish (that was the loop-6 B1 hole)
        mqttc.on_message(mqttc, None, SimpleNamespace(payload=sighting("a")))
        self.assertEqual(calls, [])
        # board b arrives -> full 2-board vector -> a real estimate
        mqttc.on_message(mqttc, None,
                         SimpleNamespace(payload=sighting("b", rssi=-72)))
        self.assertEqual(len(calls), 1)
        topic, est = calls[0]
        self.assertEqual(topic, collector.TOPIC_ESTIMATE)
        self.assertEqual(est["spot"], "living")
        self.assertEqual(est["matched_boards"], 2)
        self.assertEqual(est["board"], "b")
        self.assertEqual(est["units"], "dB distance")

    def test_on_message_rejects_partial_fleet_without_publish(self):
        # a genuinely narrow measurement (1 fresh board against a 2-board
        # map) covers the reason-string mapping end to end: refusal, not NaN
        st = self._state()
        st.spots = {"living": {"a": -58.0, "b": -72.0}}
        mqttc = collector.make_client(st, min_boards=1)
        calls = []
        mqttc.publish = lambda topic, payload: (
            calls.append(topic), SimpleNamespace(rc=0))[1]
        mqttc.on_message(mqttc, None, SimpleNamespace(payload=sighting("a")))
        self.assertEqual(calls, [])
        # a non-target node's sighting must never trigger estimates either
        mqttc.on_message(mqttc, None,
                         SimpleNamespace(payload=sighting("b",
                                                          mac="11:22:33:44:55:66")))
        self.assertEqual(calls, [])


class CalibrateFileTest(unittest.TestCase):
    """calibrate.load_existing_spots() — the "never corrupt the map" guard."""

    def _path(self, tmp, content=None, raw=None):
        p = os.path.join(tmp, "spots.json")
        if raw is not None:
            with open(p, "w") as fh:
                fh.write(raw)
        elif content is not None:
            with open(p, "w") as fh:
                json.dump(content, fh)
        return p

    def test_missing_file_is_empty(self):
        import calibrate
        with mock.patch.object(calibrate, "SPOTS_PATH", "/nonexistent/st.json"):
            self.assertEqual(calibrate.load_existing_spots(), {})

    def test_corrupt_file_exits_1(self):
        import calibrate
        with tempfile.TemporaryDirectory() as tmp:
            p = self._path(tmp, raw="{ nope")
            with mock.patch.object(calibrate, "SPOTS_PATH", p), \
                    self.assertRaises(SystemExit) as cm:
                calibrate.load_existing_spots()
            self.assertEqual(cm.exception.code, 1)

    def test_non_map_top_level_exits_1(self):
        import calibrate
        with tempfile.TemporaryDirectory() as tmp:
            p = self._path(tmp, content=["a", "list"])
            with mock.patch.object(calibrate, "SPOTS_PATH", p), \
                    self.assertRaises(SystemExit) as cm:
                calibrate.load_existing_spots()
            self.assertEqual(cm.exception.code, 1)


class ArgGateTest(unittest.TestCase):
    """CLI arg gates the pass-5 review added (each must exit with code 2)."""

    def _script(self, name):
        return os.path.join(os.path.dirname(__file__), "..", "pi", name)

    def run_cli(self, script, *argv):
        import subprocess
        return subprocess.run([sys.executable, self._script(script), *argv],
                              capture_output=True, text=True, timeout=30)

    def test_collector_rejects_bad_port(self):
        self.assertEqual(self.run_cli("collector.py", "--port", "70000").returncode, 2)

    def test_calibrate_rejects_empty_spot(self):
        # required=True accepts "--spot ''"; the empty-name gate must catch it
        r = self.run_cli("calibrate.py", "--spot", "",
                         "--target", "aa:bb:cc:dd:ee:ff")
        self.assertEqual(r.returncode, 2)

    def test_calibrate_rejects_min_samples_zero(self):
        r = self.run_cli("calibrate.py", "--spot", "kitchen",
                         "--target", "aa:bb:cc:dd:ee:ff", "--min-samples", "0")
        self.assertEqual(r.returncode, 2)

    def test_calibrate_rejects_min_per_board_zero(self):
        r = self.run_cli("calibrate.py", "--spot", "kitchen",
                         "--target", "aa:bb:cc:dd:ee:ff", "--min-per-board", "0")
        self.assertEqual(r.returncode, 2)

    def test_inject_rejects_bad_interval(self):
        r = self.run_cli("inject.py", "--target", "aa:bb:cc:dd:ee:ff",
                         "--rssi", "-45", "--interval", "0")
        self.assertEqual(r.returncode, 2)

    def test_inject_rejects_meta_board(self):
        r = self.run_cli("inject.py", "--target", "aa:bb:cc:dd:ee:ff",
                         "--rssi", "-45", "--board", "_meta")
        self.assertEqual(r.returncode, 2)

    def test_inject_rejects_overlong_board(self):
        # mirror of the firmware static_assert: nobody accepts a 17-char id
        r = self.run_cli("inject.py", "--target", "aa:bb:cc:dd:ee:ff",
                         "--rssi", "-45", "--board", "x" * 17)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)