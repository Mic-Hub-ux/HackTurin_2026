/*
  Smart Crossing - gateway ESP-NOW <-> seriale (protocollo SC-P2P v1)

  Collegato via USB alla UNO Q (115200 baud, un oggetto JSON per riga terminata da "\n"):
    - riga ricevuta dalla seriale  -> inviata in broadcast ESP-NOW (FF:FF:FF:FF:FF:FF)
    - frame ESP-NOW ricevuto       -> stampato sulla seriale come una riga
  Il gateway e' "trasparente": non interpreta il JSON, non fa relay e non controlla le firme
  (dedup, relay con "h", firma HMAC e rilevanza li gestisce il nodo Python).

  Convenzione seriale: le righe che iniziano con "#" sono commenti/stato del gateway e vanno
  ignorate dal lato Python (che comunque scarta ogni riga non JSON). Anche le righe "#" inviate
  dal Python al gateway vengono ignorate.

  Compatibile con core Arduino-ESP32 2.x (ESP-IDF 4.4) e 3.x (ESP-IDF 5.x).
*/
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif
#if __has_include(<esp_idf_version.h>)
#include <esp_idf_version.h>
#endif

// ======================= configurazione =======================
#define WIFI_CHANNEL 1          // canale fisso, UGUALE su tutti i gateway e i braccialetti (1-13)
#define SERIAL_BAUD 115200
#define LINE_BUF 256            // buffer riga seriale: righe piu' lunghe vengono scartate
#define MAX_PAYLOAD 240         // limite del protocollo SC-P2P v1
#define MAX_FRAME 250           // massimo di un frame ESP-NOW v1: frame piu' lunghi ignorati
#define RX_QUEUE_LEN 16         // frame ricevuti in attesa di essere stampati
#define STATUS_EVERY_MS 60000UL // riga di stato "#" periodica (0 = mai)
#define LED_ACTIVE_LOW 0        // 1 se il LED della scheda si accende con LOW

#ifndef LED_PIN
#ifdef LED_BUILTIN
#define LED_PIN LED_BUILTIN
#else
#define LED_PIN 2               // LED blu delle DevKit ESP32 classiche
#endif
#endif

// core 1.x non definisce la versione: assumiamo la vecchia API
#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 1
#define ESP_ARDUINO_VERSION_MINOR 0
#define ESP_ARDUINO_VERSION_PATCH 0
#endif

// La callback di invio ha cambiato firma con ESP-IDF 5.5 (core Arduino 3.3):
// prima (const uint8_t *mac, status), poi (const esp_now_send_info_t *info, status).
#if defined(ESP_IDF_VERSION) && defined(ESP_IDF_VERSION_VAL)
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
#define SC_SEND_INFO_CB 1
#endif
#endif
#ifndef SC_SEND_INFO_CB
#define SC_SEND_INFO_CB 0
#endif

// ======================= stato =======================
struct Frame {
  uint16_t len;
  uint8_t data[MAX_FRAME];
};

static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static QueueHandle_t rxQueue = nullptr;

// contatori (quelli "volatile" sono scritti dal task Wi-Fi nelle callback)
static volatile uint32_t rxDropped = 0, rxTooLong = 0, txFail = 0;
static uint32_t rxCount = 0, rxBad = 0, txCount = 0, txErr = 0, lineErr = 0;

static char line[LINE_BUF];
static size_t lineLen = 0;
static bool lineOverflow = false;

static uint32_t ledOffAt = 0, lastStatus = 0;
static bool ledOn = false;

// ======================= LED attivita' =======================
static void ledWrite(bool on) {
  ledOn = on;
  digitalWrite(LED_PIN, (on != (LED_ACTIVE_LOW != 0)) ? HIGH : LOW);
}

static void ledPulse() {
  ledWrite(true);
  ledOffAt = millis() + 40;
}

static void ledTick() {
  if (ledOn && (int32_t)(millis() - ledOffAt) >= 0) ledWrite(false);
}

// ======================= callback ESP-NOW =======================
// Girano nel task Wi-Fi: solo copia in coda, niente Serial qui dentro.
static void queueFrame(const uint8_t *data, int len) {
  static Frame f;  // la callback e' sempre chiamata dallo stesso task: basta un buffer statico
  if (data == nullptr || len <= 0 || len > MAX_FRAME) {
    rxTooLong = rxTooLong + 1;  // (niente ++ su volatile: deprecato in C++20)
    return;
  }
  f.len = (uint16_t)len;
  memcpy(f.data, data, len);
  if (xQueueSend(rxQueue, &f, 0) != pdTRUE) rxDropped = rxDropped + 1;
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  (void)info;  // info->src_addr = MAC del mittente, info->rx_ctrl->rssi = segnale
  queueFrame(data, len);
}
#else
static void onRecv(const uint8_t *mac, const uint8_t *data, int len) {
  (void)mac;
  queueFrame(data, len);
}
#endif

#if SC_SEND_INFO_CB
static void onSent(const esp_now_send_info_t *info, esp_now_send_status_t status) {
  (void)info;
  if (status != ESP_NOW_SEND_SUCCESS) txFail = txFail + 1;
}
#else
static void onSent(const uint8_t *mac, esp_now_send_status_t status) {
  (void)mac;
  if (status != ESP_NOW_SEND_SUCCESS) txFail = txFail + 1;
}
#endif

// ======================= avvio radio =======================
static bool startEspNow() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();     // nessun AP: il canale resta quello fissato qui sotto
  WiFi.setSleep(false);  // ricezione continua

  esp_err_t err = ESP_FAIL;
  for (int i = 0; i < 20 && err != ESP_OK; i++) {
    err = esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) delay(50);
  }
  if (err != ESP_OK) Serial.printf("# errore: canale %d non impostato (%s)\n", WIFI_CHANNEL, esp_err_to_name(err));

  if (esp_now_init() != ESP_OK) return false;
  esp_now_register_recv_cb(onRecv);
  esp_now_register_send_cb(onSent);

  esp_now_peer_info_t peer;
  memset(&peer, 0, sizeof(peer));
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = 0;  // 0 = canale corrente (quello impostato sopra)
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;  // il broadcast ESP-NOW non puo' essere cifrato
  esp_err_t perr = esp_now_add_peer(&peer);
  return perr == ESP_OK || perr == ESP_ERR_ESPNOW_EXIST;
}

static void printStatus(const char *what) {
  uint8_t ch = 0;
  wifi_second_chan_t sc;
  esp_wifi_get_channel(&ch, &sc);
  Serial.printf("# SC-P2P gateway %s: mac=%s canale=%u core=%d.%d.%d tx=%lu txerr=%lu txfail=%lu "
                "rx=%lu rxbad=%lu persi=%lu lunghi=%lu righe_err=%lu\n",
                what, WiFi.macAddress().c_str(), (unsigned)ch, ESP_ARDUINO_VERSION_MAJOR,
                ESP_ARDUINO_VERSION_MINOR, ESP_ARDUINO_VERSION_PATCH, (unsigned long)txCount,
                (unsigned long)txErr, (unsigned long)txFail, (unsigned long)rxCount,
                (unsigned long)rxBad, (unsigned long)rxDropped, (unsigned long)rxTooLong,
                (unsigned long)lineErr);
}

// ======================= controllo testo =======================
// true se i byte sono testo UTF-8 senza caratteri di controllo e sembrano un oggetto JSON
static bool isJsonText(const uint8_t *s, int n) {
  if (n < 2 || s[0] != '{' || s[n - 1] != '}') return false;
  for (int i = 0; i < n;) {
    uint8_t c = s[i];
    if (c < 0x80) {
      if (c < 0x20 || c == 0x7F) return false;  // controllo, \n, \r, \0 ...
      i++;
      continue;
    }
    int extra = (c & 0xE0) == 0xC0 ? 1 : (c & 0xF0) == 0xE0 ? 2 : (c & 0xF8) == 0xF0 ? 3 : -1;
    if (extra < 0 || c == 0xC0 || c == 0xC1 || c > 0xF4 || i + extra >= n) return false;
    for (int k = 1; k <= extra; k++)
      if ((s[i + k] & 0xC0) != 0x80) return false;
    i += extra + 1;
  }
  return true;
}

// ======================= ESP-NOW -> seriale =======================
static void printFrame(const Frame &f) {
  int n = f.len;
  // toglie eventuali terminatori/spazi finali (es. mittente che invia anche lo '\0')
  while (n > 0 && (f.data[n - 1] == 0 || f.data[n - 1] == ' ' || f.data[n - 1] == '\r' || f.data[n - 1] == '\n')) n--;
  if (!isJsonText(f.data, n)) {
    rxBad++;
    return;
  }
  Serial.write(f.data, n);
  Serial.write('\n');
  rxCount++;
  ledPulse();
}

// ======================= seriale -> ESP-NOW =======================
static void handleLine(char *s, size_t len) {
  while (len > 0 && (s[len - 1] == ' ' || s[len - 1] == '\t')) s[--len] = '\0';
  while (len > 0 && (*s == ' ' || *s == '\t')) {
    s++;
    len--;
  }
  if (len == 0 || s[0] == '#') return;  // riga vuota o commento
  if (s[0] != '{') {
    lineErr++;
    Serial.println("# ignorata: la riga non e' un oggetto JSON");
    return;
  }
  if (len > MAX_PAYLOAD) {
    lineErr++;
    Serial.printf("# errore: messaggio di %u byte > %d, non inviato\n", (unsigned)len, MAX_PAYLOAD);
    return;
  }
  esp_err_t err = esp_now_send(BCAST, (const uint8_t *)s, len);
  for (int i = 0; err == ESP_ERR_ESPNOW_NO_MEM && i < 3; i++) {  // coda TX piena: piccolo ritardo e riprova
    delay(5);
    err = esp_now_send(BCAST, (const uint8_t *)s, len);
  }
  if (err == ESP_OK) {
    txCount++;
    ledPulse();
  } else {
    txErr++;
    Serial.printf("# errore esp_now_send: %s\n", esp_err_to_name(err));
  }
}

static void readSerial() {
  int budget = 512;  // non monopolizza il loop
  while (budget-- > 0 && Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n') {
      if (lineOverflow) {
        lineErr++;
        Serial.printf("# errore: riga oltre %d byte, scartata\n", LINE_BUF - 1);
      } else {
        line[lineLen] = '\0';
        handleLine(line, lineLen);
      }
      lineLen = 0;
      lineOverflow = false;
    } else if (c == '\r' || lineOverflow) {
      // CR ignorato (monitor seriali che inviano CRLF); resto della riga troppo lunga scartato
    } else if (lineLen >= LINE_BUF - 1) {
      lineOverflow = true;
    } else {
      line[lineLen++] = (char)c;
    }
  }
}

// ======================= setup / loop =======================
void setup() {
#if !ARDUINO_USB_CDC_ON_BOOT
  Serial.setRxBufferSize(1024);  // va chiamato prima di begin()
#endif
  Serial.begin(SERIAL_BAUD);
  pinMode(LED_PIN, OUTPUT);
  ledWrite(false);
  delay(200);

  rxQueue = xQueueCreate(RX_QUEUE_LEN, sizeof(Frame));
  if (rxQueue == nullptr || !startEspNow()) {
    Serial.println("# errore: ESP-NOW non avviato, riavvio tra 5 s");
    delay(5000);
    ESP.restart();
  }
  printStatus("pronto");
  lastStatus = millis();
}

void loop() {
  static Frame f;
  readSerial();
  for (int i = 0; i < 8 && xQueueReceive(rxQueue, &f, 0) == pdTRUE; i++) printFrame(f);
  ledTick();
  if (STATUS_EVERY_MS > 0 && millis() - lastStatus >= STATUS_EVERY_MS) {
    lastStatus = millis();
    printStatus("stato");
  }
  delay(1);
}
