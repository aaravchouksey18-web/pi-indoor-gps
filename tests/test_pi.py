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

    def test_best_match_never_crashes_on_hostile_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_map(tmp, {"hall": 5, "kitchen": {"a": "near"}})
            m = fingerprint.best_match({"a": -58}, spots=fingerprint.load_spots(path))
            self.assertIsNone(m.spot)


class MatchingTest(unittest.TestCase):
    def test_full_coverage_required(self):
        # a 3-board fingerprint must NOT score when the live vector only
        # covers two of its boards (partial matches are dropped, not scored)
        spots = {"full": {"a": -60, "b": -70},
                 "incomplete": {"a": -58, "b": -75, "d": -80}}
        scored = fingerprint.compute_distances({"a": -58, "b": -70, "c": -40}, spots)
        names = [s[0] for s in scored]
        self.assertIn("full", names)
        self.assertNotIn("incomplete", names)

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

    def test_bad_mac_rejected(self):
        for mac in ("nope", "aa:bb:cc:dd:ee", "AA:BB:CC:DD:EE:FF:00"):
            payload = json.dumps({"board": "a", "mac": mac, "rssi": -45})
            self.assertIsNone(messages.parse_sighting(payload.encode()))


if __name__ == "__main__":
    unittest.main(verbosity=2)