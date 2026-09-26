// pi-indoor-sniffer.ino
// Passive 802.11 probe-request sniffer with per-device RSSI, for pi-indoor-gps.
//
// Adapted from presence-vigil's presence-sniffer v3.9 (same SDK tricks:
// rxctl header + PKT_OFF 12, cold-radio sniff session, report phase in the
// same boot straight from RAM — power-cycling wipes RTC, but RAM survives
// within a boot). The one real change: every seen device also records the
// strongest probe RSSI observed this session (byte 0 of the rxctl header),
// published in the sighting payload. Presence stays untouched; RSSI is new.

#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
extern "C" {
#include <user_interface.h>   // promiscuous RX + RTC memory API
}

#include "config.h"

// mirror pi/messages.py's 16-char board cap in the Pi collector: a
// longer BOARD_ID would compile + flash yet publish sightings nobody
// accepts, so trap it at build time instead
static_assert(sizeof(BOARD_ID) <= 17, "BOARD_ID must be <= 16 chars "
              "(the Pi collector drops longer board ids)");

#define FRAME_TYPE_MGMT   0
#define SUBTYPE_PROBE_REQ 4

// The promiscuous callback on this SDK delivers the 12-byte rxctl header
// first: RSSI at [0] (signed dBm), rate at [1], then the 802.11 frame at +12.
#define PKT_OFF 12

// cycle knobs (config.h can override)
#ifndef SNIFF_SESSION_MS
#define SNIFF_SESSION_MS 15000   // radio in capture mode at boot
#endif
#ifndef PH_HOME_MS
#define PH_HOME_MS 30000         // max time spent associating + reporting
#endif
#ifndef WIFI_RETRY_MS
#define WIFI_RETRY_MS 5000       // re-issue WiFi.begin() this often while joining
#endif

// ---------------------------------------------------------------------------
// in-RAM seen table, filled by the sniffer callback during a capture session.
// Published straight to MQTT from the SAME boot's report phase.
// ---------------------------------------------------------------------------
#define SEEN_MAX 24
struct seen_t {
  uint8_t mac[6];
  uint32_t last_seen;         // millis() of the newest probe this session
  char ssid[33];
  int8_t rssi_best;           // strongest probe RSSI seen this session, dBm
  uint16_t rssi_n;            // how many probes contributed
};
static seen_t seen[SEEN_MAX];
static uint32_t burst_frames = 0;   // every 802.11 frame the radio caught
static uint32_t burst_probes = 0;   // probe requests that hit the table

// frame-type histogram, printed at session end (kept from presence-vigil)
static uint32_t hist_mgmt[16];      // per management subtype
static uint32_t hist_data = 0;
static uint32_t hist_ctrl = 0;

WiFiClient net;
PubSubClient mqtt(net);

// --- 802.11 helpers --------------------------------------------------------

static uint8_t frame_type(uint8_t *f)    { return (f[0] >> 2) & 0x03; }
static uint8_t frame_subtype(uint8_t *f) { return (f[0] >> 4) & 0x0f; }

// pull the SSID out of a probe-request body (tagged params, tag id 0)
static void parse_ssid(uint8_t *frame, uint16_t len, char *out, size_t out_sz) {
  size_t off = 24;                       // 802.11 header on management frames
  while (off + 2 <= len) {
    uint8_t id   = frame[off];
    uint8_t tlen = frame[off + 1];
    if (off + 2 + tlen > len) break;
    if (id == 0x00 && tlen > 0) {
      size_t n = tlen < out_sz - 1 ? tlen : out_sz - 1;
      memcpy(out, &frame[off + 2], n);
      out[n] = '\0';
      return;
    }
    off += 2 + tlen;
  }
  out[0] = '\0';
}

// --- sniffer callback: stamp only, never touch TCP --------------------------

static void ICACHE_RAM_ATTR on_packet(uint8_t *buf, uint16_t len) {
  if (len < PKT_OFF + 24) return;   // real frame must fit after the rxctl
  int8_t rssi = (int8_t)buf[0];     // rxctl byte 0 == signal strength
  uint8_t *f = buf + PKT_OFF;
  uint16_t flen = len - PKT_OFF;

  burst_frames++;
  if (frame_type(f) == 0)      hist_mgmt[frame_subtype(f)]++;
  else if (frame_type(f) == 1) hist_ctrl++;
  else if (frame_type(f) == 2) hist_data++;
  if (frame_type(f) != FRAME_TYPE_MGMT)      return;
  if (frame_subtype(f) != SUBTYPE_PROBE_REQ) return;

  const uint8_t *mac = &f[10];         // address 2 == transmitter
  uint32_t now = millis();

  burst_probes++;
  for (int i = 0; i < SEEN_MAX; i++) {
    if (seen[i].last_seen != 0 && memcmp(seen[i].mac, mac, 6) == 0) {
      seen[i].last_seen = now;
      if (rssi > seen[i].rssi_best) seen[i].rssi_best = rssi;
      seen[i].rssi_n++;
      if (!seen[i].ssid[0]) parse_ssid(f, flen, seen[i].ssid, sizeof(seen[i].ssid));
      return;
    }
  }
  for (int i = 0; i < SEEN_MAX; i++) {
    if (seen[i].last_seen == 0) {        // fresh slot
      memcpy(seen[i].mac, mac, 6);
      seen[i].last_seen = now;
      seen[i].rssi_best = rssi;
      seen[i].rssi_n = 1;
      parse_ssid(f, flen, seen[i].ssid, sizeof(seen[i].ssid));
      return;
    }
  }
  // table full: recycle the slot whose sighting was captured longest ago
  int oldest = 0;
  for (int i = 1; i < SEEN_MAX; i++)
    if (seen[i].last_seen < seen[oldest].last_seen) oldest = i;
  memcpy(seen[oldest].mac, mac, 6);
  seen[oldest].last_seen = now;
  seen[oldest].rssi_best = rssi;
  seen[oldest].rssi_n = 1;
  parse_ssid(f, flen, seen[oldest].ssid, sizeof(seen[oldest].ssid));
}

// --- publish the captured sightings from the same boot's report phase ---------

static uint16_t pending_count() {
  uint16_t n = 0;
  for (int i = 0; i < SEEN_MAX; i++)
    if (seen[i].last_seen != 0) n++;
  return n;
}

static uint16_t publish_seen() {
  uint16_t sent = 0;
  for (int i = 0; i < SEEN_MAX; i++) {
    if (seen[i].last_seen == 0) continue;      // nothing captured this session
    StaticJsonDocument<192> doc;
    doc["board"] = BOARD_ID;
    char mac_s[18];
    snprintf(mac_s, sizeof(mac_s), "%02x:%02x:%02x:%02x:%02x:%02x",
             seen[i].mac[0], seen[i].mac[1], seen[i].mac[2],
             seen[i].mac[3], seen[i].mac[4], seen[i].mac[5]);
    doc["mac"] = mac_s;
    if (seen[i].ssid[0]) doc["ssid"] = seen[i].ssid;
    doc["rssi"] = seen[i].rssi_best;           // strongest probe this session
    doc["rssi_n"] = seen[i].rssi_n;            // sample count for that device

    char payload[192];
    serializeJson(doc, payload, sizeof(payload));
    if (mqtt.publish("indoor/sighting", payload)) {
      sent++;
      seen[i].last_seen = 0;                   // delivered: don't resend this boot
    }
  }
  if (sent) Serial.printf("published %u sightings\n", sent);
  return sent;
}

// The MQTT write only reaches the socket; the WiFi TCP TX queue needs a few
// link-layer turns before ESP.restart wipes RAM. Loop a moment and report what
// could not be flushed.
static void drain_and_reboot() {
  for (int attempt = 0; attempt < 10 && pending_count() > 0; attempt++) {
    mqtt.loop();
    delay(200);
  }
  Serial.printf("restarting (%u sightings left unsent)\n", pending_count());
  delay(100);
  ESP.restart();
}

// --- MQTT --------------------------------------------------------------------

static bool mqtt_connect() {
  StaticJsonDocument<96> doc;
  doc["board"] = BOARD_ID;
  doc["online"] = false;              // LWT: published if we die mid-cycle
  char will[96];
  serializeJson(doc, will, sizeof(will));
  return mqtt.connect(BOARD_ID, "indoor/online", 1, true, will);
}

// -----------------------------------------------------------------------------

enum { PH_HOME, PH_SNIFF };
static uint8_t phase = PH_HOME;
static uint32_t phase_until = 0;
static uint32_t wifi_retry_at = 0;

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.println("boot");
  Serial.printf("reset reason: %s\n", ESP.getResetReason());

  // Sniff FIRST, from a cold idle radio that has never associated — the
  // canonical ESP8266 sniffer state (presence-vigil proved this matters).
  WiFi.mode(WIFI_STA);                 // station opmode, NOT associated
  wifi_set_channel(SNIFF_CHANNEL);
  wifi_set_promiscuous_rx_cb(on_packet);
  wifi_promiscuous_enable(true);
  Serial.printf("sniffing on channel %d\n", wifi_get_channel());

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
#ifdef MQTT_USER
  if (strlen(MQTT_USER) > 0) {
    mqtt.setCredentials(MQTT_USER, MQTT_PASS);   // MQTT 3.1 username/password
  }
#endif
  mqtt.setBufferSize(256);
  mqtt.setKeepAlive(60);

  phase = PH_SNIFF;
  phase_until = millis() + SNIFF_SESSION_MS;
}

void loop() {
  if (phase == PH_SNIFF) {
    if (millis() >= phase_until) {
      wifi_promiscuous_enable(false);
      Serial.printf("session done: %u frames, %u probes, ch=%u\n",
                    burst_frames, burst_probes, wifi_get_channel());
      Serial.printf("hist mgmt[0]=%lu [4]probe_req=%lu [5]probe_resp=%lu "
                    "[8]beacon=%lu [11]auth=%lu [12]deauth=%lu | data=%lu ctrl=%lu\n",
                    hist_mgmt[0], hist_mgmt[4], hist_mgmt[5], hist_mgmt[8],
                    hist_mgmt[11], hist_mgmt[12], hist_data, hist_ctrl);
      Serial.println("reporting...");
      phase = PH_HOME;
      phase_until = millis() + PH_HOME_MS;
      wifi_retry_at = millis() + WIFI_RETRY_MS;   // (re)associate shortly
      WiFi.begin(WIFI_SSID, WIFI_PASS);          // fresh association for the report
    }
    return;
  }

  // PH_HOME: associate + report, then reboot whatever happens.
  // Association is async *and* can stall on a flaky AP; re-issue begin every
  // WIFI_RETRY_MS until we're connected so a one-off failed join doesn't cost
  // the whole session's captures.
  if (phase == PH_HOME && WiFi.status() != WL_CONNECTED &&
      millis() >= wifi_retry_at) {
    Serial.printf("wifi status=%d, re-issuing begin\n", WiFi.status());
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    wifi_retry_at = millis() + WIFI_RETRY_MS;
  }

  if (WiFi.status() == WL_CONNECTED && !mqtt.connected()) {
    digitalWrite(LED_BUILTIN, LOW);
    if (mqtt_connect()) {
      Serial.printf("mqtt connected to %s:%d\n", MQTT_HOST, MQTT_PORT);
      StaticJsonDocument<96> doc;
      doc["board"] = BOARD_ID;
      doc["online"] = true;
      char online_msg[96];
      serializeJson(doc, online_msg, sizeof(online_msg));
      mqtt.publish("indoor/online", online_msg, true);
      digitalWrite(LED_BUILTIN, HIGH);
    }
  }
  mqtt.loop();
  publish_seen();                 // drains this boot's captures via MQTT

  if (millis() >= phase_until) {
    drain_and_reboot();
  }
}