/*
  Smart Crossing - BRACCIALE DEL CICLISTA (ESP32 DevKit oppure ESP32-C3 SuperMini)

  Canale DIRETTO incrocio -> bracciale: l'incrocio piu' vicino (la UNO Q, "incrocio-1") manda un
  pacchetto UDP all'indirizzo del bracciale, porta 48100. Niente server, niente internet: basta che
  bracciale e incrocio siano sulla stessa rete Wi-Fi (anche l'hotspot di un telefono).

    {"v":1,"t":"wr","n":"incrocio-1","seq":12,"on":1,"k":"bike_lane_invasion","sev":2,
     "txt":"Auto nella ciclabile","ttl":2500}

  - on:1 -> ALLARME: il LED (o il motorino) di ALERT_PIN lampeggia. Pericoli per il ciclista:
    auto che entra nella ciclabile, auto ferma nella ciclabile, sorpasso troppo vicino.
  - on:0 -> fine allarme. L'incrocio lo rimanda ogni mezzo secondo anche quando non c'e' niente:
    e' il segnale "sei vicino all'incrocio e sei collegato".
  - Se per "ttl" ms non arriva niente l'allarme si spegne da solo (pacchetto perso, incrocio spento).

  LED di stato (quello sulla scheda):
    lampeggio veloce   = Wi-Fi non collegato
    lampo ogni secondo = Wi-Fi ok, nessun incrocio vicino
    acceso fisso       = incrocio collegato (riceve i suoi messaggi)

  COLLEGAMENTO: LED + resistenza 220-330 ohm tra ALERT_PIN (GPIO 4, gamba lunga) e GND.
  Al posto del LED si puo' mettere un motorino vibrante, ma tramite un transistor NPN.

  Monitor seriale a 115200: stampa l'indirizzo IP (da mettere nel config dell'incrocio) e ogni allarme.
  Comandi dal Monitor seriale: "t" = allarme di prova per 3 s, "i" = stato e indirizzo IP.

  Nessuna libreria da installare: serve solo il pacchetto schede "esp32" di Espressif.
*/
#include <WiFi.h>
#include <WiFiUdp.h>

// ------------------------------------------------------------------ DA COMPILARE
#define WIFI_SSID "SaleSindacali-WiFi-Ext"      // la stessa rete della UNO Q, a 2,4 GHz
#define WIFI_PASS "Unione1906"
// #define SCHEDA_C3_SUPERMINI     // togliete le // se la scheda e' una ESP32-C3 SuperMini
// ------------------------------------------------------------------

#define PORTA 48100
#define ALERT_PIN 4                // LED di allarme (o motorino con transistor)

#ifdef SCHEDA_C3_SUPERMINI
#define STATUS_PIN 8               // LED blu della SuperMini, si accende con LOW
#define STATUS_ACTIVE_LOW 1
#elif defined(LED_BUILTIN)
#define STATUS_PIN LED_BUILTIN     // LED della scheda (sulla ESP32-S3 e' quello RGB: si accende bianco)
#define STATUS_ACTIVE_LOW 0
#else
#define STATUS_PIN 2               // LED della ESP32 DevKit classica
#define STATUS_ACTIVE_LOW 0
#endif

#define LINK_TIMEOUT_MS 3000       // senza messaggi per 3 s: incrocio non piu' collegato

WiFiUDP udp;
char buf[300];

bool alertOn = false;
unsigned long alertUntil = 0;      // l'allarme si spegne da solo dopo ttl ms senza conferme
int alertSev = 2;
char alertTxt[48] = "";
char alertFrom[24] = "";
unsigned long lastMsg = 0;         // ultimo messaggio dall'incrocio (0 = mai)
unsigned long lastStatusPrint = 0;
unsigned long received = 0;
bool wasConnected = false;

// ------------------------------------------------------------------ lettura del JSON (senza librerie)

long numField(const char *s, const char *key, long def) {
  char pat[24];
  snprintf(pat, sizeof(pat), "\"%s\":", key);
  const char *p = strstr(s, pat);
  return p ? strtol(p + strlen(pat), nullptr, 10) : def;
}

bool strField(const char *s, const char *key, char *out, size_t n) {
  char pat[24];
  snprintf(pat, sizeof(pat), "\"%s\":\"", key);
  const char *p = strstr(s, pat);
  if (!p) return false;
  p += strlen(pat);
  size_t i = 0;
  while (*p && *p != '"' && i + 1 < n) out[i++] = *p++;
  out[i] = 0;
  return true;
}

// ------------------------------------------------------------------ LED

void statusLed(bool on) {
  if (STATUS_PIN < 0) return;
  digitalWrite(STATUS_PIN, (on ^ STATUS_ACTIVE_LOW) ? HIGH : LOW);
}

void startAlert(const char *txt, const char *from, int sev, unsigned long ttl) {
  if (!alertOn) {
    Serial.printf("[ALLARME] %s (da %s)\n", txt, from);
  }
  alertOn = true;
  alertSev = sev;
  alertUntil = millis() + ttl;
  strncpy(alertTxt, txt, sizeof(alertTxt) - 1);
  strncpy(alertFrom, from, sizeof(alertFrom) - 1);
}

void stopAlert(const char *why) {
  if (alertOn) Serial.printf("[fine allarme] %s\n", why);
  alertOn = false;
  digitalWrite(ALERT_PIN, LOW);
}

void updateLeds(unsigned long now) {
  // allarme: lampeggio veloce (4 Hz) se grave, altrimenti 2 Hz
  if (alertOn) {
    unsigned long periodo = alertSev >= 3 ? 250 : 500;
    digitalWrite(ALERT_PIN, (now % periodo) < periodo / 2 ? HIGH : LOW);
  } else {
    digitalWrite(ALERT_PIN, LOW);
  }
  bool wifi = WiFi.status() == WL_CONNECTED;
  bool link = lastMsg && now - lastMsg < LINK_TIMEOUT_MS;
  if (!wifi) statusLed((now % 200) < 100);
  else if (!link) statusLed((now % 1000) < 80);
  else statusLed(true);
}

// ------------------------------------------------------------------ rete

void printStatus() {
  bool link = lastMsg && millis() - lastMsg < LINK_TIMEOUT_MS;
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[stato] ip=%s porta=%d  incrocio=%s  messaggi=%lu  allarme=%s\n",
                  WiFi.localIP().toString().c_str(), PORTA,
                  link ? (alertFrom[0] ? alertFrom : "collegato") : "nessuno",
                  received, alertOn ? alertTxt : "no");
  } else {
    Serial.printf("[stato] Wi-Fi non collegato a \"%s\" (rete a 2,4 GHz? password giusta?)\n", WIFI_SSID);
  }
}

void handlePacket() {
  int n = udp.parsePacket();
  if (n <= 0) return;
  int len = udp.read(buf, sizeof(buf) - 1);
  if (len <= 0) return;
  buf[len] = 0;
  char t[8] = "";
  if (!strField(buf, "t", t, sizeof(t)) || strcmp(t, "wr") != 0) return;  // non e' per il bracciale

  char from[24] = "incrocio";
  strField(buf, "n", from, sizeof(from));
  unsigned long now = millis();
  if (!lastMsg || now - lastMsg >= LINK_TIMEOUT_MS) {
    Serial.printf("[collegato] ricevo dall'incrocio %s (%s)\n", from, udp.remoteIP().toString().c_str());
  }
  lastMsg = now;
  received++;
  strncpy(alertFrom, from, sizeof(alertFrom) - 1);

  long ttl = numField(buf, "ttl", 2500);
  if (numField(buf, "on", 0)) {
    char txt[48] = "pericolo";
    if (!strField(buf, "txt", txt, sizeof(txt))) strField(buf, "k", txt, sizeof(txt));
    startAlert(txt, from, (int)numField(buf, "sev", 2), (unsigned long)ttl);
  } else {
    stopAlert("l'incrocio non vede piu' il pericolo");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(ALERT_PIN, OUTPUT);
  if (STATUS_PIN >= 0) pinMode(STATUS_PIN, OUTPUT);
  // test del LED di allarme: due lampi
  for (int i = 0; i < 2; i++) {
    digitalWrite(ALERT_PIN, HIGH);
    delay(150);
    digitalWrite(ALERT_PIN, LOW);
    delay(150);
  }
  Serial.println();
  Serial.println("Smart Crossing - bracciale del ciclista");
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);            // senza risparmio energetico i pacchetti arrivano subito
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[wifi] mi collego a \"%s\"...\n", WIFI_SSID);
  udp.begin(PORTA);
}

void loop() {
  unsigned long now = millis();
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected && !wasConnected) {
    udp.stop();
    udp.begin(PORTA);
    Serial.printf("[wifi] collegato, ip=%s  -> mettete \"%s\" in wearable.devices del config dell'incrocio\n",
                  WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str());
  }
  wasConnected = connected;

  if (connected) handlePacket();
  if (alertOn && (long)(now - alertUntil) > 0) stopAlert("nessun messaggio dall'incrocio (fuori portata?)");
  updateLeds(now);

  if (Serial.available()) {
    char c = Serial.read();
    if (c == 't') startAlert("allarme di prova", "monitor seriale", 3, 3000);
    if (c == 'i') printStatus();
  }
  if (now - lastStatusPrint > 10000) {   // ogni 10 s: cosi' l'IP si legge anche aprendo il monitor dopo
    lastStatusPrint = now;
    printStatus();
  }
  delay(5);
}
