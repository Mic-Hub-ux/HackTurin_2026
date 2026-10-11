/*
  Smart Crossing - braccialetto vibrante (protocollo SC-P2P v1)

  - Riceve i pericoli ("t":"hz") dagli incroci via ESP-NOW broadcast (dal gateway ESP32 del nodo)
    e, se e' definito WIFI_SSID, anche via UDP multicast 239.255.42.99:47999 sulla LAN.
  - Vibra se il messaggio interessa il profilo (ciclista/pedone/veicolo), non e' scaduto
    e ha severita' >= 2 oppure categoria "emergency". I "cancel" non fanno vibrare.
  - Ogni 2 s invia un beacon di presenza "vru" in broadcast ESP-NOW (id casuale a ogni avvio).
  - Profilo salvato in flash (Preferences); pressione lunga del tasto BOOT = profilo successivo,
    confermato da N vibrazioni brevi (1 = ciclista, 2 = pedone, 3 = veicolo).
    Pressione breve = ripete le N vibrazioni del profilo attuale.
  - Le righe JSON scritte nel monitor seriale (115200) vengono trattate come messaggi ricevuti:
    utile per provare il braccialetto senza gateway. "?" stampa lo stato.

  Firma "sig": il braccialetto NON la verifica (accetta anche messaggi non firmati). Il rischio
  e' al massimo una vibrazione falsa; la verifica HMAC la fanno i nodi.
  Il braccialetto non fa mai relay (ignora "h").

  Libreria richiesta: ArduinoJson v7 (Benoit Blanchon).
  Compatibile con core Arduino-ESP32 2.x e 3.x.
*/
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <sys/time.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif
#if __has_include(<esp_random.h>)
#include <esp_random.h>
#endif

// ======================= configurazione =======================
// Scheda: per la ESP32 "normale" (DevKit, LED su GPIO 2, BOOT su GPIO 0) non serve niente.
// Per una ESP32-C3 SuperMini (LED su GPIO 8 acceso con LOW, BOOT su GPIO 9) togliete il commento:
// #define SCHEDA_C3_SUPERMINI
#ifdef SCHEDA_C3_SUPERMINI
#define LED_PIN 8
#define PRESET_LED_ACTIVE_LOW 1
#define PRESET_BUTTON_PIN 9
#endif

#define WIFI_CHANNEL 1  // canale ESP-NOW fisso: deve essere uguale a quello del gateway

// Opzionale: collegamento a una rete Wi-Fi per ricevere anche via UDP multicast dalla LAN.
// ATTENZIONE: con il Wi-Fi attivo ESP-NOW usa il canale dell'access point (WIFI_CHANNEL viene
// ignorato), quindi il gateway deve avere WIFI_CHANNEL = canale dell'AP (AP su canale fisso).
// #define WIFI_SSID "nome-rete"
// #define WIFI_PASS "password"

#define VIBRO_PIN 4             // motorino (via transistor NPN) oppure, per la demo, un LED + resistenza
#ifdef PRESET_BUTTON_PIN
#define BUTTON_PIN PRESET_BUTTON_PIN
#else
#define BUTTON_PIN 0            // tasto BOOT (GPIO 0 su ESP32/ESP32-S3; GPIO 9 su ESP32-C3)
#endif
#ifdef PRESET_LED_ACTIVE_LOW
#define LED_ACTIVE_LOW PRESET_LED_ACTIVE_LOW
#else
#define LED_ACTIVE_LOW 0        // 1 se il LED della scheda si accende con LOW (es. ESP32-C3 SuperMini)
#endif
#define LONG_PRESS_MS 1500      // durata della pressione lunga per cambiare profilo
#define BEACON_MS 2000          // periodo del beacon "vru"
#define DEDUP_SIZE 32           // messaggi (id, a) ricordati
#define DEDUP_MIN_MS 60000UL    // ogni (id, a) e' ricordato almeno 60 s
#define DEFAULT_VALID_MS 60000UL  // validita' se il messaggio non ha "e"
#define MAX_VALID_MS 3600000UL  // tetto alla validita' (1 h)
#define MAX_FRAME 250           // massimo frame ESP-NOW v1
#define RX_QUEUE_LEN 8
#define LINE_BUF 256

#ifndef LED_PIN
#ifdef LED_BUILTIN
#define LED_PIN LED_BUILTIN
#else
#define LED_PIN 2
#endif
#endif

#if defined(WIFI_SSID) && !defined(WIFI_PASS)
#define WIFI_PASS ""
#endif

#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 1
#endif

#ifdef WIFI_SSID
#include <WiFiUdp.h>
#define MCAST_PORT 47999
#endif

// ======================= tipi e stato =======================
// Sequenza di vibrazione: d = acceso, spento, acceso, spento... (ms). prio: chi interrompe chi.
struct Pattern {
  uint8_t n;
  uint8_t prio;
  uint16_t d[16];
};

struct Frame {
  uint16_t len;
  uint8_t data[MAX_FRAME];
};

struct Seen {
  uint32_t hash;   // FNV-1a di "id|a" (0 = libero)
  uint32_t until;  // millis() fino a cui ignorare le ripetizioni
};

// emergenza: 3 impulsi lunghi
static const Pattern PAT_EMERGENCY = {6, 4, {700, 300, 700, 300, 700, 300}};
// severita' 3: 3 impulsi brevi e veloci, ripetuti due volte
static const Pattern PAT_SEV3 = {12, 3, {100, 80, 100, 80, 100, 400, 100, 80, 100, 80, 100, 300}};
// severita' 2: 2 impulsi brevi
static const Pattern PAT_SEV2 = {4, 2, {150, 150, 150, 300}};

static const char *PROFILES[] = {"cyclist", "person", "vehicle"};
static const char *PROFILES_IT[] = {"ciclista", "pedone", "veicolo"};

static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static Pattern curPat, pendingPat;
static bool playing = false, hasPending = false;
static uint8_t patStep = 0;
static uint32_t stepStart = 0;

static Seen seen[DEDUP_SIZE];
static uint8_t seenNext = 0;

static uint8_t profileIdx = 0;
static Preferences prefs;
static char devId[12];

static QueueHandle_t rxQueue = nullptr;
static volatile uint32_t rxDropped = 0;
static uint32_t rxHz = 0, rxOther = 0, rxBadJson = 0, vibrations = 0, beaconErr = 0;
static uint32_t lastBeacon = 0;

static char serLine[LINE_BUF];
static size_t serLen = 0;
static bool serOverflow = false;

#ifdef WIFI_SSID
static WiFiUDP udp;
static const IPAddress MCAST_IP(239, 255, 42, 99);
static bool udpReady = false, ntpStarted = false;
#endif

// ======================= motore + LED (non bloccante) =======================
static void output(bool on) {
  digitalWrite(VIBRO_PIN, on ? HIGH : LOW);
  digitalWrite(LED_PIN, (on != (LED_ACTIVE_LOW != 0)) ? HIGH : LOW);  // il LED copia il motore
}

static void startPattern(const Pattern &p) {
  curPat = p;
  playing = true;
  patStep = 0;
  stepStart = millis();
  output(true);
}

// Una sequenza con priorita' >= di quella in corso la interrompe; altrimenti resta in attesa.
static void play(const Pattern &p) {
  if (p.n == 0) return;
  if (!playing || p.prio >= curPat.prio) {
    startPattern(p);
  } else if (!hasPending || p.prio >= pendingPat.prio) {
    pendingPat = p;
    hasPending = true;
  }
}

static void patternTick() {
  if (!playing) return;
  uint32_t now = millis();
  if (now - stepStart < curPat.d[patStep]) return;
  patStep++;
  stepStart = now;
  if (patStep >= curPat.n) {
    playing = false;
    output(false);
    if (hasPending) {
      hasPending = false;
      startPattern(pendingPat);
    }
    return;
  }
  output(patStep % 2 == 0);  // passi pari = motore acceso
}

// N vibrazioni brevi (conferma profilo)
static void playCount(uint8_t n) {
  Pattern p;
  p.prio = 1;
  p.n = 0;
  for (uint8_t i = 0; i < n && p.n + 2 <= 16; i++) {
    p.d[p.n++] = 120;
    p.d[p.n++] = 220;
  }
  play(p);
}

// ======================= profilo =======================
static void loadProfile() {
  prefs.begin("sc-wear", false);
  profileIdx = prefs.getUChar("profile", 0);
  prefs.end();
  if (profileIdx > 2) profileIdx = 0;
}

static void saveProfile() {
  prefs.begin("sc-wear", false);
  prefs.putUChar("profile", profileIdx);
  prefs.end();
}

static void showProfile() {
  Serial.printf("[profilo] %s (%s): %u vibrazioni\n", PROFILES_IT[profileIdx], PROFILES[profileIdx],
                (unsigned)(profileIdx + 1));
  playCount(profileIdx + 1);
}

static void nextProfile() {
  profileIdx = (profileIdx + 1) % 3;
  saveProfile();
  Serial.print("[profilo] cambiato -> ");
  showProfile();
}

// Tasto BOOT: antirimbalzo 30 ms, pressione lunga = profilo successivo, breve = mostra profilo
static void buttonTick() {
  static bool raw = false, stable = false, longDone = false;
  static uint32_t changedAt = 0, pressedAt = 0;
  uint32_t now = millis();
  bool down = digitalRead(BUTTON_PIN) == LOW;
  if (down != raw) {
    raw = down;
    changedAt = now;
  }
  if (raw != stable && now - changedAt >= 30) {
    stable = raw;
    if (stable) {
      pressedAt = now;
      longDone = false;
    } else if (!longDone) {
      showProfile();
    }
  }
  if (stable && !longDone && now - pressedAt >= LONG_PRESS_MS) {
    longDone = true;
    nextProfile();
  }
}

// ======================= dedup (id, a) =======================
static uint32_t fnv1a(const char *s, char a) {
  uint32_t h = 2166136261UL;
  while (*s) {
    h ^= (uint8_t)*s++;
    h *= 16777619UL;
  }
  h ^= (uint8_t)'|';
  h *= 16777619UL;
  h ^= (uint8_t)a;
  h *= 16777619UL;
  return h ? h : 1;
}

// true se (id, a) e' gia' stato visto e non e' ancora scaduto; altrimenti lo registra
static bool alreadySeen(const char *id, char a, uint32_t keepMs) {
  uint32_t h = fnv1a(id, a), now = millis();
  for (int i = 0; i < DEDUP_SIZE; i++)
    if (seen[i].hash == h && (int32_t)(seen[i].until - now) > 0) return true;
  seen[seenNext].hash = h;
  seen[seenNext].until = now + keepMs;
  seenNext = (seenNext + 1) % DEDUP_SIZE;
  return false;
}

// ======================= tempo =======================
// Ora "vera" in ms epoch, solo se sincronizzata via NTP (con WIFI_SSID); altrimenti false.
static bool wallClockMs(double &out) {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  if (tv.tv_sec < 1700000000L) return false;
  out = (double)tv.tv_sec * 1000.0 + (double)(tv.tv_usec / 1000);
  return true;
}

// Validita' residua in ms (<= 0 = scaduto).
// Senza orologio: il messaggio vale (e - ts) ms dal momento della ricezione.
static int32_t remainingMs(JsonObjectConst m) {
  double e = m["e"].as<double>();
  double ts = m["ts"].as<double>();
  double now = 0, rem;
  if (e <= 0) rem = DEFAULT_VALID_MS;                             // nessuna scadenza indicata
  else if (e > 1.5e12 && wallClockMs(now)) rem = e - now;         // orologio NTP + "e" in ms epoch
  else if (ts > 0) rem = e - ts;
  else rem = DEFAULT_VALID_MS;
  if (rem > (double)MAX_VALID_MS) rem = MAX_VALID_MS;
  if (rem < 0) rem = 0;
  return (int32_t)rem;
}

static void u64toa(uint64_t v, char *out) {
  char tmp[21];
  int i = 0;
  do {
    tmp[i++] = (char)('0' + (v % 10));
    v /= 10;
  } while (v > 0);
  int j = 0;
  while (i > 0) out[j++] = tmp[--i];
  out[j] = '\0';
}

// ======================= messaggi =======================
static void handlePayload(const char *data, size_t len, const char *via) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, data, len);
  if (err) {
    rxBadJson++;
    Serial.printf("[%s] JSON non valido: %s\n", via, err.c_str());
    return;
  }
  JsonObjectConst m = doc.as<JsonObjectConst>();
  if (m.isNull() || m["v"].as<int>() != 1) {
    rxOther++;
    return;
  }
  const char *t = m["t"] | "";
  if (strcmp(t, "hz") != 0) {  // "hb" degli incroci, "vru" di altri braccialetti: ignorati
    rxOther++;
    return;
  }
  rxHz++;
  const char *id = m["id"] | "";
  const char *a = m["a"] | "n";
  if (id[0] == '\0') return;
  const char *kind = m["k"] | "?";
  const char *cat = m["c"] | "";
  int sev = m["s"].as<int>();
  char act = (a[0] == 'c') ? 'c' : 'n';  // "c" = cancel, tutto il resto = new
  bool cancel = (act == 'c');

  int32_t rem = remainingMs(m);
  uint32_t keepMs = (uint32_t)rem > DEDUP_MIN_MS ? (uint32_t)rem : DEDUP_MIN_MS;
  if (alreadySeen(id, act, keepMs)) return;  // copia ripetuta o rilanciata da un altro incrocio

  // profilo nella lista "tg" (lista assente = interessa a tutti)
  bool targeted = true;
  JsonVariantConst tg = m["tg"];
  if (!tg.isNull()) {
    targeted = false;
    if (tg.is<JsonArrayConst>()) {
      for (JsonVariantConst x : tg.as<JsonArrayConst>()) {
        const char *p = x | "";
        if (strcmp(p, PROFILES[profileIdx]) == 0) targeted = true;
      }
    }
  }

  const Pattern *pat = nullptr;
  const char *what;
  if (cancel) what = "annullato, nessuna vibrazione";
  else if (rem <= 0) what = "scaduto";
  else if (!targeted) what = "non per questo profilo";
  else if (strcmp(cat, "emergency") == 0) { pat = &PAT_EMERGENCY; what = "VIBRA: emergenza"; }
  else if (sev >= 3) { pat = &PAT_SEV3; what = "VIBRA: severita' 3"; }
  else if (sev == 2) { pat = &PAT_SEV2; what = "VIBRA: severita' 2"; }
  else what = "severita' bassa, ignorato";

  Serial.printf("[%s] hz id=%s a=%c k=%s c=%s s=%d da=%s valido=%lds -> %s\n", via, id, act, kind, cat, sev,
                (const char *)(m["n"] | "?"), (long)(rem / 1000), what);
  if (pat) {
    vibrations++;
    play(*pat);
  }
}

// ======================= ESP-NOW =======================
// Callback nel task Wi-Fi: solo copia in coda, l'elaborazione avviene nel loop().
static void queueFrame(const uint8_t *data, int len) {
  static Frame f;
  if (data == nullptr || len <= 0 || len > MAX_FRAME) return;
  f.len = (uint16_t)len;
  memcpy(f.data, data, len);
  if (xQueueSend(rxQueue, &f, 0) != pdTRUE) rxDropped = rxDropped + 1;
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  (void)info;
  queueFrame(data, len);
}
#else
static void onRecv(const uint8_t *mac, const uint8_t *data, int len) {
  (void)mac;
  queueFrame(data, len);
}
#endif

static bool startRadio() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // con il risparmio energetico Wi-Fi si perdono i broadcast ESP-NOW
#ifdef WIFI_SSID
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);  // ESP-NOW seguira' il canale dell'access point
#else
  WiFi.disconnect();
  esp_err_t err = ESP_FAIL;
  for (int i = 0; i < 20 && err != ESP_OK; i++) {
    err = esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) delay(50);
  }
  if (err != ESP_OK) Serial.printf("[errore] canale %d non impostato: %s\n", WIFI_CHANNEL, esp_err_to_name(err));
#endif
  if (esp_now_init() != ESP_OK) return false;
  esp_now_register_recv_cb(onRecv);

  esp_now_peer_info_t peer;
  memset(&peer, 0, sizeof(peer));
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = 0;  // 0 = canale corrente
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_err_t perr = esp_now_add_peer(&peer);
  return perr == ESP_OK || perr == ESP_ERR_ESPNOW_EXIST;
}

// Beacon di presenza: {"v":1,"t":"vru","d":"wb-xxxxxx","p":"cyclist","ts":...,"h":0}
static void sendBeacon() {
  double w;
  char ts[24];
  u64toa(wallClockMs(w) ? (uint64_t)w : (uint64_t)millis(), ts);
  char buf[128];
  int n = snprintf(buf, sizeof(buf), "{\"v\":1,\"t\":\"vru\",\"d\":\"%s\",\"p\":\"%s\",\"ts\":%s,\"h\":0}",
                   devId, PROFILES[profileIdx], ts);
  if (n <= 0 || n >= (int)sizeof(buf)) return;
  if (esp_now_send(BCAST, (const uint8_t *)buf, n) != ESP_OK) beaconErr++;
#ifdef WIFI_SSID
  if (udpReady) {
    udp.beginPacket(MCAST_IP, MCAST_PORT);
    udp.write((const uint8_t *)buf, n);
    udp.endPacket();
  }
#endif
}

// ======================= Wi-Fi + UDP multicast (opzionale) =======================
#ifdef WIFI_SSID
static void wifiTick() {
  bool up = WiFi.status() == WL_CONNECTED;
  if (up && !udpReady) {
    udpReady = udp.beginMulticast(MCAST_IP, MCAST_PORT);
    if (!ntpStarted) {
      configTime(0, 0, "pool.ntp.org", "time.google.com");
      ntpStarted = true;
    }
    Serial.printf("[wifi] connesso, ip=%s canale=%d (il gateway deve usare WIFI_CHANNEL %d)\n",
                  WiFi.localIP().toString().c_str(), (int)WiFi.channel(), (int)WiFi.channel());
  } else if (!up && udpReady) {
    udp.stop();
    udpReady = false;
    Serial.println("[wifi] disconnesso");
  }
  if (!udpReady) return;
  static char buf[MAX_FRAME + 1];
  for (int i = 0; i < 4; i++) {
    int n = udp.parsePacket();
    if (n <= 0) break;
    int r = udp.read((uint8_t *)buf, MAX_FRAME);
    if (n > MAX_FRAME) {  // datagramma troppo grande: lo svuota tutto (altrimenti parsePacket resta a 0)
      while (udp.available() > 0) udp.read((uint8_t *)buf, MAX_FRAME);
      continue;
    }
    if (r > 0) handlePayload(buf, (size_t)r, "udp");
  }
}
#endif

// ======================= seriale (test e debug) =======================
static void printStatus() {
  Serial.printf("[stato] id=%s profilo=%s canale=%d hz=%lu altri=%lu json_err=%lu vibrazioni=%lu persi=%lu beacon_err=%lu\n",
                devId, PROFILES[profileIdx], (int)WiFi.channel(), (unsigned long)rxHz, (unsigned long)rxOther,
                (unsigned long)rxBadJson, (unsigned long)vibrations, (unsigned long)rxDropped,
                (unsigned long)beaconErr);
}

static void serialTick() {
  int budget = 256;
  while (budget-- > 0 && Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n') {
      if (!serOverflow && serLen > 0) {
        serLine[serLen] = '\0';
        if (serLine[0] == '{') handlePayload(serLine, serLen, "seriale");
        else if (serLine[0] == '?') printStatus();
      }
      serLen = 0;
      serOverflow = false;
    } else if (c == '\r' || serOverflow) {
      // ignorato
    } else if (serLen >= LINE_BUF - 1) {
      serOverflow = true;
    } else {
      serLine[serLen++] = (char)c;
    }
  }
}

// ======================= setup / loop =======================
void setup() {
  pinMode(VIBRO_PIN, OUTPUT);  // subito: motore spento
  pinMode(LED_PIN, OUTPUT);
  output(false);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  Serial.begin(115200);
  delay(200);

  loadProfile();
  rxQueue = xQueueCreate(RX_QUEUE_LEN, sizeof(Frame));
  if (rxQueue == nullptr || !startRadio()) {
    Serial.println("[errore] ESP-NOW non avviato: riavvio tra 5 s");
    delay(5000);
    ESP.restart();
  }
  // id casuale a ogni avvio (radio accesa = esp_random() davvero casuale): non tracciabile
  snprintf(devId, sizeof(devId), "wb-%06lx", (unsigned long)(esp_random() & 0xFFFFFFUL));

  Serial.printf("\n[avvio] braccialetto SC-P2P v1 id=%s mac=%s profilo=%s canale=%d\n", devId,
                WiFi.macAddress().c_str(), PROFILES[profileIdx], (int)WiFi.channel());
  Serial.println("[avvio] BOOT lungo = cambia profilo, BOOT breve = mostra profilo, '?' = stato");
  showProfile();  // N vibrazioni = profilo attuale
  lastBeacon = millis();
}

void loop() {
  static Frame f;
  for (int i = 0; i < 4 && xQueueReceive(rxQueue, &f, 0) == pdTRUE; i++)
    handlePayload((const char *)f.data, f.len, "espnow");
#ifdef WIFI_SSID
  wifiTick();
#endif
  serialTick();
  buttonTick();
  patternTick();
  if (millis() - lastBeacon >= BEACON_MS) {
    lastBeacon = millis();
    sendBeacon();
  }
  delay(2);  // piccola pausa: lascia respirare gli altri task
}
