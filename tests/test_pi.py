#!/usr/bin/env python3
"""Tests for pi/fingerprint.py + pi/messages.py (stdlib-only; run with
python3 -m pytest tests/ or python3 tests/test_pi.py)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))

import fingerprint  # noqa: E402
import messages    # noqa: E402


def write_map(tmp, spots):
    with open(os.path.join(tmp, "spots.json"), "w") as fh:
        json.dump({"spots": spots}, fh)
    return os.path.join(tmp, "spots.json")


class LoadSpotsTest(unittest.TestCase):
    def test_missing_file_is_empty_map(self):
        self.assertEqual(fingerprint.load_spots("/nonexistent.json"), {})

    def test_corrupt_json_is_empty_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spots.json")
            with open(path, "w") as fh:
                fh.write("{ not json")
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

    def test_inject_rejects_bad_interval(self):
        r = self.run_cli("inject.py", "--target", "aa:bb:cc:dd:ee:ff",
                         "--rssi", "-45", "--interval", "0")
        self.assertEqual(r.returncode, 2)

    def test_inject_rejects_meta_board(self):
        r = self.run_cli("inject.py", "--target", "aa:bb:cc:dd:ee:ff",
                         "--rssi", "-45", "--board", "_meta")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)