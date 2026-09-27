"""Shared MQTT payload handling for the pi scripts.

Indoor positioning runs on a LAN-trusted broker (mosquitto, no auth), so the
collectors must be defensive: every payload arriving on indoor/sighting is
untrusted input. parse_sighting() validates shape + range and returns a
normalized dict or None, so a malformed or hostile message can never crash a
collector or poison a fingerprint. The other untrusted file is the
fingerprint map (spots.json), which fingerprint.load_spots() validates
separately before anything scores against it.
"""
import json
import re

# firmware publishes lowercase "aa:bb:cc:dd:ee:ff" MACs (see publish_seen)
_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

# Sane ceiling for one sighting payload. The firmware's worst case is ~121
# bytes; anything bigger than 512 B is either not a sighting or is hostile.
# Checking before json.loads also caps the two expensive failure modes there:
# a deeply nested payload can hit Python's recursion limit (RecursionError,
# a RuntimeError that the old except clause did NOT catch — uncaught, paho
# re-raises it from the on_message callback and kills the collector) and a
# huge payload can exhaust memory mid-parse.
MAX_SIGHTING_BYTES = 512


def normalize_mac(raw):
    """Lowercase + validate a MAC address; None if it can't be one."""
    if not isinstance(raw, str):
        return None
    mac = raw.strip().lower()
    return mac if _MAC_RE.match(mac) else None


def parse_sighting(payload_bytes):
    """Validate a raw indoor/sighting payload.

    Returns {"board", "mac", "rssi"} (mac lowercased) or None. Rules:

      - must parse as a JSON object
      - board: non-empty string, at most 16 chars (never "_meta", which is
        reserved for fingerprint-map metadata and would poison matching)
      - mac:   a valid 6-byte MAC (lowercased)
      - rssi:  an integer in the plausible dBm band [-150, 0]

    Anything else is dropped silently — a collector must never die (or
    publish a bogus estimate) because one node sent garbage.
    """
    if not isinstance(payload_bytes, (bytes, bytearray)):
        return None
    if len(payload_bytes) > MAX_SIGHTING_BYTES:
        return None
    try:
        p = json.loads(payload_bytes.decode())
    except (ValueError, UnicodeDecodeError, AttributeError,
            RecursionError, MemoryError):
        # RecursionError (deeply nested arrays/objects) is a RuntimeError,
        # not a ValueError: paho re-raises callback exceptions, so without
        # this an unauthenticated LAN host could kill the collector with a
        # 4 KB publish. MemoryError is the same class of remote-DoS: it must
        # not escape into the MQTT thread.
        return None
    if not isinstance(p, dict):
        return None
    board = p.get("board")
    if not isinstance(board, str) or not board.strip() or len(board) > 16:
        return None
    board = board.strip()
    if board == "_meta":
        # "_meta" is reserved for fingerprint-map metadata (calibrate stores
        # it inside a spot); a live node claiming that name would pollute the
        # collector's vector and turn every match into a "coverage" refusal.
        return None
    mac = normalize_mac(p.get("mac"))
    if mac is None:
        return None
    rssi = p.get("rssi")
    try:
        rssi = int(rssi)
    except (TypeError, ValueError, OverflowError):
        # 1e999 / Infinity JSON values parse to float inf: int() raises
        # OverflowError there, not ValueError, and must not kill the caller
        return None
    if not -150 <= rssi <= 0:
        return None
    return {"board": board, "mac": mac, "rssi": rssi}