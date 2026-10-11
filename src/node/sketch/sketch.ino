/*
  Smart Crossing - sketch MCU (STM32U585 della Arduino UNO Q, core Zephyr)

  Accende i 4 LED di segnalazione dell'incrocio. Chi decide e' il Python sulla parte Linux
  (node/python/leds.py), che via Bridge chiama:   set_leds(packed, ttl_ms)
    packed: 2 bit per LED -> LED1 bit 0-1, LED2 bit 2-3, LED3 bit 4-5, LED4 bit 6-7
            modo 0 spento, 1 acceso, 2 lampeggio lento (1 Hz), 3 lampeggio veloce (4 Hz)
    ttl_ms: validita' del comando. Il Python lo rimanda ogni ~1 s anche se non cambia.
  Ruoli di default (config.json -> leds.roles):
    LED1 (D2) pericolo visto qui        LED2 (D3) pericolo da un incrocio vicino (server)
    LED3 (D4) mezzo di emergenza        LED4 (D5) server: acceso = online, lampeggia = offline

  Fail-safe "cervello offline": se per ttl_ms non arriva nessun comando (app ferma, Python
  bloccato, Bridge giu') tutti i LED si spengono tranne il LED 4, che fa un breve lampo ogni 2 s.
  Cosi' un LED di pericolo non resta mai acceso a vuoto.
  All'avvio: test dei LED uno dopo l'altro (200 ms ciascuno), poi LED 4 fisso finche' il
  Bridge non e' pronto (se resta fisso, il router Linux non risponde).

  COLLEGAMENTO (ogni LED ha gia' la sua resistenza in serie)
    pin D2 / D3 / D4 / D5 -> resistenza -> anodo del LED (gamba lunga)
    catodo del LED (gamba corta, lato piatto) -> GND   (un GND comune va bene per tutti)
  In pratica: il filo che oggi va al 5V va sul pin, quello al GND resta al GND.
  La resistenza puo' stare indifferentemente sulla gamba lunga o su quella corta.
  ATTENZIONE: i pin dell'MCU sono a 3,3 V e reggono poca corrente: stare sotto ~8 mA per pin.
    LED rosso/giallo (~2 V) con 220-330 ohm -> circa 4-6 mA: ok.
    Se a 3,3 V i LED sembrano deboli usare una resistenza piu' piccola, ma MAI sotto ~150 ohm.
    LED blu/bianchi (~3 V) a 3,3 V restano comunque deboli: meglio rossi, gialli o verdi.
*/
#include <Arduino_RouterBridge.h>

const int LED_PINS[4] = {2, 3, 4, 5};
const uint32_t DEFAULT_TTL_MS = 3000;
const uint32_t OFFLINE_PERIOD_MS = 2000;  // fail-safe: un lampo del LED 4 ogni 2 s...
const uint32_t OFFLINE_FLASH_MS = 150;    // ...lungo 150 ms

// Scritti dal thread del Bridge, letti da loop(): volatile e solo valori a 32 bit
volatile uint32_t cmd_packed = 0;
volatile uint32_t cmd_ttl_ms = DEFAULT_TTL_MS;
volatile uint32_t cmd_at_ms = 0;
volatile bool cmd_received = false;

bool level[4] = {false, false, false, false};  // ultimo stato scritto sui pin

// Chiamata dal Python. Con Bridge.provide gira nel thread del Bridge (stack piccolo):
// solo salvare i valori, niente stampe, niente delay, niente Bridge.call.
void set_leds(int packed, int ttl_ms) {
  cmd_packed = (uint32_t)packed & 0xFF;
  cmd_ttl_ms = ttl_ms > 0 ? (uint32_t)ttl_ms : DEFAULT_TTL_MS;
  cmd_at_ms = millis();
  cmd_received = true;
}

// Livello del LED per un modo, calcolato da millis(): stessi modi lampeggiano in fase
bool mode_level(uint32_t mode, uint32_t now) {
  switch (mode) {
    case 1: return true;
    case 2: return (now % 1000) < 500;  // lento, 1 Hz
    case 3: return (now % 250) < 125;   // veloce, 4 Hz
    default: return false;
  }
}

void write_led(int i, bool on) {
  if (level[i] != on) {
    level[i] = on;
    digitalWrite(LED_PINS[i], on ? HIGH : LOW);
  }
}

void setup() {
  for (int i = 0; i < 4; i++) {
    pinMode(LED_PINS[i], OUTPUT);
    digitalWrite(LED_PINS[i], LOW);
  }
  // Test del cablaggio: ogni LED 200 ms, uno dopo l'altro
  for (int i = 0; i < 4; i++) {
    digitalWrite(LED_PINS[i], HIGH);
    delay(200);
    digitalWrite(LED_PINS[i], LOW);
  }
  // LED 4 fisso mentre aspetto il router Linux (Bridge.begin aspetta la sua risposta)
  digitalWrite(LED_PINS[3], HIGH);
  level[3] = true;
  Bridge.begin();
  Bridge.provide("set_leds", set_leds);
}

void loop() {
  // Prima copio il comando, poi leggo millis(): cosi' cmd_at_ms non e' mai "nel futuro"
  uint32_t packed = cmd_packed;
  uint32_t ttl = cmd_ttl_ms;
  uint32_t at = cmd_at_ms;
  bool received = cmd_received;
  uint32_t now = millis();

  bool alive = received && (now - at) < ttl;  // differenza senza segno: ok anche quando millis() riparte da 0
  for (int i = 0; i < 4; i++) {
    bool on;
    if (alive) {
      on = mode_level((packed >> (2 * i)) & 0x3, now);
    } else {
      on = (i == 3) && (now % OFFLINE_PERIOD_MS) < OFFLINE_FLASH_MS;  // cervello offline
    }
    write_led(i, on);
  }
  // niente delay: dopo ogni loop() il core chiama il Bridge (__loopHook) che cede la CPU
}
