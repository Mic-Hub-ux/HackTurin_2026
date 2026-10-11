/*
  Smart Crossing - Arduino UNO R4 WiFi che fa da SECONDO INCROCIO ("incrocio-2") per la demo.

  Parla con la UNO Q in peer-to-peer (protocollo SC-P2P v1, UDP porta 47999) SENZA server:
  - RICEVE i pericoli che la UNO Q manda agli incroci vicini e li mostra sulla matrice LED:
      triangolo con "!" = pericolo, croce = mezzo di emergenza.
      Lampeggio veloce se il pericolo e' indirizzato a questo incrocio, lento se e' generico.
  - MANDA alla UNO Q un pericolo "visto" da questo incrocio (la UNO Q accende il LED 2):
      tasto tra D2 e GND, oppure dal Monitor seriale (115200):
        b = auto in ciclabile, e = ambulanza, v = velocita' alta, c = fine dell'ultimo pericolo
  - Ogni 5 s manda un "heartbeat": la UNO Q vede incrocio-2 online (LED 4 acceso anche senza server).

  Prima di caricare: impostate WIFI_SSID, WIFI_PASS e l'IP della UNO Q (NODE_IP) qui sotto.
  Sulla UNO Q, in config.json: "p2p": {"enabled": true, "peers": ["<IP di questa R4>:47999"]}
  (l'IP della R4 viene stampato sul Monitor seriale all'avvio).

  Librerie: ArduinoJson (Benoit Blanchon, v7). WiFiS3 e Arduino_LED_Matrix sono gia' nel
  pacchetto "Arduino UNO R4 Boards".
*/
#include <WiFiS3.h>
#include <WiFiUdp.h>
#include <ArduinoJson.h>
#include "Arduino_LED_Matrix.h"

// ======================= configurazione =======================
#define WIFI_SSID "nome-rete"           // la stessa rete della UNO Q (2,4 GHz)
#define WIFI_PASS "password"
IPAddress NODE_IP(192, 168, 23, 174);   // IP della UNO Q (sulla board: hostname -I)
#define MY_ID "incrocio-2"
#define TARGET_ID "incrocio-1"          // a chi mandiamo i pericoli (la UNO Q)
#define P2P_PORT 47999
#define BUTTON_PIN 2                    // tasto (o un filo) tra D2 e GND
#define HEARTBEAT_MS 5000
#define VALIDITY_MS 30000               // durata dei pericoli che mandiamo

// ======================= immagini per la matrice 8x12 =======================
uint8_t PERICOLO[8][12] = {
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,1,0,0,1,0,0,0,0},
  {0,0,0,1,0,1,1,0,1,0,0,0},
  {0,0,1,0,0,1,1,0,0,1,0,0},
  {0,1,0,0,0,1,1,0,0,0,1,0},
  {1,0,0,0,0,0,0,0,0,0,0,1},
  {1,0,0,0,0,1,1,0,0,0,0,1},
  {1,1,1,1,1,1,1,1,1,1,1,1}};
uint8_t EMERGENZA[8][12] = {
  {0,0,0,0,1,1,1,1,0,0,0,0},
  {0,0,0,0,1,1,1,1,0,0,0,0},
  {1,1,1,1,1,1,1,1,1,1,1,1},
  {1,1,1,1,1,1,1,1,1,1,1,1},
  {1,1,1,1,1,1,1,1,1,1,1,1},
  {1,1,1,1,1,1,1,1,1,1,1,1},
  {0,0,0,0,1,1,1,1,0,0,0,0},
  {0,0,0,0,1,1,1,1,0,0,0,0}};
uint8_t INVIATO[8][12] = {
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,1,0,0,0,0},
  {0,0,0,0,0,0,0,1,1,0,0,0},
  {1,1,1,1,1,1,1,1,1,1,0,0},
  {1,1,1,1,1,1,1,1,1,1,0,0},
  {0,0,0,0,0,0,0,1,1,0,0,0},
  {0,0,0,0,0,0,0,1,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0}};
uint8_t PUNTO[8][12] = {
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,1,1,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0},
  {0,0,0,0,0,0,0,0,0,0,0,0}};
uint8_t VUOTO[8][12] = {{0}};

// ======================= stato =======================
WiFiUDP Udp;
ArduinoLEDMatrix matrix;
char rxBuf[300];
unsigned long lastHeartbeat = 0, alertUntil = 0, sentUntil = 0, lastBlink = 0, lastButton = 0;
bool alertEmergency = false, alertForMe = false, blinkOn = false, buttonWasDown = false;
char alertId[48] = "";
char lastSentId[48] = "";
char lastSentKind[32] = "";
unsigned long seq = 0;

// ======================= invio =======================
void sendJson(JsonDocument &doc) {
  char out[256];
  size_t n = serializeJson(doc, out, sizeof(out));
  if (n == 0 || n > 240) {
    Serial.println("# messaggio troppo lungo, non inviato");
    return;
  }
  Udp.beginPacket(NODE_IP, P2P_PORT);
  Udp.write((const uint8_t *)out, n);
  Udp.endPacket();
}

void sendHeartbeat() {
  JsonDocument doc;
  doc["v"] = 1;
  doc["t"] = "hb";
  doc["n"] = MY_ID;
  doc["ts"] = millis();
  doc["h"] = 0;
  sendJson(doc);
}

// I tempi sono in millis(): la UNO Q si accorge che l'orologio e' diverso e li riporta sul suo,
// tenendo la durata del pericolo (VALIDITY_MS).
void sendHazard(const char *kind, const char *category, int severity, int cause, const char *target, bool cancel) {
  JsonDocument doc;
  doc["v"] = 1;
  doc["t"] = "hz";
  if (cancel) {
    if (lastSentId[0] == 0) {
      Serial.println("# nessun pericolo da chiudere");
      return;
    }
    doc["id"] = lastSentId;
    kind = lastSentKind;
  } else {
    seq++;
    snprintf(lastSentId, sizeof(lastSentId), "%s-%lu", MY_ID, seq);
    strncpy(lastSentKind, kind, sizeof(lastSentKind) - 1);
    doc["id"] = lastSentId;
  }
  unsigned long now = millis();
  doc["n"] = MY_ID;
  doc["a"] = cancel ? "c" : "n";
  doc["k"] = kind;
  doc["c"] = category;
  doc["s"] = severity;
  doc["cc"] = cause;
  doc["ts"] = now;
  doc["e"] = cancel ? now : now + VALIDITY_MS;
  doc["tw"] = "est";
  doc["nx"][0] = TARGET_ID;
  doc["tg"][0] = target;
  doc["h"] = 1;
  sendJson(doc);
  Serial.print(cancel ? "inviato FINE " : "inviato pericolo ");
  Serial.print(kind);
  Serial.print(" -> ");
  Serial.println(TARGET_ID);
  sentUntil = millis() + 1000;
}

// ======================= ricezione =======================
void handlePacket(int len) {
  int n = Udp.read(rxBuf, sizeof(rxBuf) - 1);
  if (n <= 0) return;
  rxBuf[n] = 0;
  JsonDocument doc;
  if (deserializeJson(doc, rxBuf, n)) return;  // non e' JSON: ignoro
  const char *t = doc["t"] | "";
  const char *from = doc["n"] | "?";
  if (strcmp(from, MY_ID) == 0) return;
  if (strcmp(t, "hb") == 0) {
    Serial.print("heartbeat da ");
    Serial.println(from);
    return;
  }
  if (strcmp(t, "hz") != 0) return;

  const char *id = doc["id"] | "";
  const char *azione = doc["a"] | "n";
  const char *kind = doc["k"] | "?";
  const char *cat = doc["c"] | "";
  bool forMe = true;  // senza "nx" il pericolo e' per tutti i vicini
  if (doc["nx"].is<JsonArray>() && doc["nx"].size() > 0) {
    forMe = false;
    for (JsonVariant v : doc["nx"].as<JsonArray>()) {
      if (strcmp(v | "", MY_ID) == 0) forMe = true;
    }
  }
  Serial.print("pericolo da ");
  Serial.print(from);
  Serial.print(": ");
  Serial.print(kind);
  Serial.print(" azione=");
  Serial.print(azione);
  Serial.println(forMe ? " (per questo incrocio)" : " (per un altro incrocio)");

  if (strcmp(azione, "c") == 0) {
    if (strcmp(id, alertId) == 0) alertUntil = 0;  // la UNO Q dice che e' finito
    return;
  }
  long durata = (long)((doc["e"] | 0.0) - (doc["ts"] | 0.0));
  if (durata <= 0 || durata > 120000) durata = VALIDITY_MS;
  alertUntil = millis() + durata;
  alertEmergency = strcmp(cat, "emergency") == 0;
  alertForMe = forMe;
  strncpy(alertId, id, sizeof(alertId) - 1);
}

// ======================= matrice =======================
void updateMatrix() {
  unsigned long now = millis();
  if ((long)(sentUntil - now) > 0) {
    matrix.renderBitmap(INVIATO, 8, 12);
    return;
  }
  if ((long)(alertUntil - now) > 0) {
    unsigned long periodo = alertForMe ? 150 : 500;
    if (now - lastBlink >= periodo) {
      lastBlink = now;
      blinkOn = !blinkOn;
    }
    if (blinkOn) {
      if (alertEmergency) matrix.renderBitmap(EMERGENZA, 8, 12);
      else matrix.renderBitmap(PERICOLO, 8, 12);
    } else {
      matrix.renderBitmap(VUOTO, 8, 12);
    }
    return;
  }
  // nessun pericolo: un punto che "respira" ogni secondo = incrocio acceso e in ascolto
  if ((now / 1000) % 2 == 0) matrix.renderBitmap(PUNTO, 8, 12);
  else matrix.renderBitmap(VUOTO, 8, 12);
}

// ======================= setup / loop =======================
void connectWifi() {
  Serial.print("collegamento a ");
  Serial.println(WIFI_SSID);
  while (WiFi.begin(WIFI_SSID, WIFI_PASS) != WL_CONNECTED) {
    Serial.println("  riprovo tra 3 s...");
    delay(3000);
  }
  while (WiFi.localIP() == IPAddress(0, 0, 0, 0)) delay(200);  // l'IP arriva poco dopo
  Serial.print("collegato. IP di questa R4: ");
  Serial.println(WiFi.localIP());
  Serial.println("-> sulla UNO Q mettete in config.json p2p.peers: [\"<questo IP>:47999\"]");
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  matrix.begin();
  matrix.renderBitmap(PUNTO, 8, 12);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("modulo WiFi non trovato!");
    while (true) delay(1000);
  }
  connectWifi();
  Udp.begin(P2P_PORT);
  Serial.println("pronto. Comandi: b = auto in ciclabile, e = ambulanza, v = velocita', c = fine pericolo");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();

  int len = Udp.parsePacket();
  if (len > 0) handlePacket(len);

  if (millis() - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = millis();
    sendHeartbeat();
  }

  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'b') sendHazard("bike_lane_invasion", "violation", 3, 99, "cyclist", false);
    else if (c == 'e') sendHazard("emergency_vehicle", "emergency", 3, 95, "person", false);
    else if (c == 'v') sendHazard("speeding", "violation", 2, 99, "person", false);
    else if (c == 'c') sendHazard("", "", 1, 0, "cyclist", true);
  }

  bool down = digitalRead(BUTTON_PIN) == LOW;  // tasto premuto = collegato a GND
  if (down && !buttonWasDown && millis() - lastButton > 300) {
    lastButton = millis();
    sendHazard("bike_lane_invasion", "violation", 3, 99, "cyclist", false);
  }
  buttonWasDown = down;

  updateMatrix();
}
