// OBD-READ logger
//
// Reads live engine data from a car's OBD-II port through an ELM327 adapter,
// timestamps each sample, and pushes batches of samples to the Firebase
// Realtime Database under /trips/<tripId>/samples.
//
// Hardware: ESP32 dev board + ELM327 UART adapter (e.g. an STN1110/ELM327 OBD-II
// UART board) on Serial2. The ESP32 is powered from the OBD port's 12 V pin
// through a buck converter.
//
// Libraries: ELMduino (Arduino Library Manager).

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ELMduino.h>
#include <time.h>

#include "secrets.h"  // copy secrets.h.example to secrets.h and fill it in

// ---- Wiring ----------------------------------------------------------------
#define ELM_RX_PIN 16
#define ELM_TX_PIN 17
#define ELM_BAUD   38400

// ---- Sampling ---------------------------------------------------------------
const unsigned long SAMPLE_INTERVAL_MS = 1000;  // one full sample per second
const int BATCH_SIZE = 10;                      // samples per Firebase upload

struct Sample {
  uint64_t t;        // epoch milliseconds
  float speed;       // km/h      (PID 0x0D)
  float rpm;         // rev/min   (PID 0x0C)
  float throttle;    // percent   (PID 0x11)
  float load;        // percent   (PID 0x04)
  float coolant;     // deg C     (PID 0x05)
};

ELM327 elm;
Sample batch[BATCH_SIZE];
int batchCount = 0;
unsigned long sampleIndex = 0;
String tripId;

// ELMduino is non-blocking: each PID query is polled until it finishes, so
// the readings are taken one at a time in this order.
enum Pid { P_SPEED, P_RPM, P_THROTTLE, P_LOAD, P_COOLANT, P_DONE };
Pid currentPid = P_DONE;
Sample pending;
unsigned long lastSampleAt = 0;

uint64_t epochMillis() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000ULL;
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(" connected");
}

void syncClock() {
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.print("NTP");
  while (time(nullptr) < 1600000000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println(" synced");
}

// PATCH the batch into /trips/<tripId>/samples so each sample lands at its own
// index key. If the upload fails the batch is kept and retried next time.
bool uploadBatch() {
  if (batchCount == 0) return true;
  if (WiFi.status() != WL_CONNECTED) return false;

  String body = "{";
  for (int i = 0; i < batchCount; i++) {
    const Sample &s = batch[i];
    unsigned long idx = sampleIndex - batchCount + i;
    if (i > 0) body += ",";
    body += "\"" + String(idx) + "\":{";
    body += "\"t\":" + String((unsigned long long)s.t);
    body += ",\"speed\":" + String(s.speed, 1);
    body += ",\"rpm\":" + String(s.rpm, 0);
    body += ",\"throttle\":" + String(s.throttle, 1);
    body += ",\"load\":" + String(s.load, 1);
    body += ",\"coolant\":" + String(s.coolant, 0);
    body += "}";
  }
  body += "}";

  WiFiClientSecure client;
  client.setInsecure();  // fine for a hobby logger; pin the cert for anything more
  HTTPClient http;
  String url = String(FIREBASE_URL) + "/trips/" + tripId +
               "/samples.json?auth=" + FIREBASE_AUTH;
  http.begin(client, url);
  http.addHeader("Content-Type", "application/json");
  int code = http.PATCH(body);
  http.end();

  if (code == 200) {
    batchCount = 0;
    return true;
  }
  Serial.printf("Upload failed: HTTP %d\n", code);
  return false;
}

void startTrip() {
  tripId = String((unsigned long long)epochMillis());
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  String url = String(FIREBASE_URL) + "/trips/" + tripId +
               "/meta.json?auth=" + FIREBASE_AUTH;
  http.begin(client, url);
  http.addHeader("Content-Type", "application/json");
  http.PUT("{\"started\":" + tripId + ",\"vehicle\":\"" + VEHICLE_NAME + "\"}");
  http.end();
  Serial.println("Trip " + tripId);
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(ELM_BAUD, SERIAL_8N1, ELM_RX_PIN, ELM_TX_PIN);

  connectWifi();
  syncClock();

  while (!elm.begin(Serial2, false, 2000)) {
    Serial.println("ELM327 not responding, retrying...");
    delay(2000);
  }
  Serial.println("ELM327 connected");

  startTrip();
}

// Polls the current PID. Returns true once that reading is finished (whether
// it succeeded or not), so the caller can move on to the next PID.
bool pollPid(Pid pid, float &out) {
  float value = 0;
  switch (pid) {
    case P_SPEED:    value = elm.kph(); break;
    case P_RPM:      value = elm.rpm(); break;
    case P_THROTTLE: value = elm.throttle(); break;
    case P_LOAD:     value = elm.engineLoad(); break;
    case P_COOLANT:  value = elm.engineCoolantTemp(); break;
    default: return true;
  }
  if (elm.nb_rx_state == ELM_GETTING_MSG) return false;
  out = (elm.nb_rx_state == ELM_SUCCESS) ? value : NAN;
  return true;
}

void loop() {
  if (currentPid == P_DONE) {
    if (millis() - lastSampleAt < SAMPLE_INTERVAL_MS) return;
    lastSampleAt = millis();
    pending = Sample{epochMillis(), NAN, NAN, NAN, NAN, NAN};
    currentPid = P_SPEED;
  }

  float *fields[] = {&pending.speed, &pending.rpm, &pending.throttle,
                     &pending.load, &pending.coolant};
  if (!pollPid(currentPid, *fields[currentPid])) return;
  currentPid = (Pid)(currentPid + 1);
  if (currentPid != P_DONE) return;

  // Full sample collected.
  if (batchCount < BATCH_SIZE) {
    batch[batchCount++] = pending;
    sampleIndex++;
  }
  Serial.printf("%.0f km/h  %.0f rpm  thr %.0f%%  load %.0f%%  %.0fC\n",
                pending.speed, pending.rpm, pending.throttle, pending.load,
                pending.coolant);

  if (batchCount >= BATCH_SIZE) uploadBatch();
}
