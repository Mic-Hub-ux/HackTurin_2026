# Firmware ESP32: gateway P2P e braccialetto vibrante

Gli incroci si parlano direttamente, senza server e senza Wi-Fi, con il protocollo **SC-P2P v1**.
Lo stesso protocollo arriva anche ai dispositivi indossabili.

```
 UNO Q incrocio-1                                                UNO Q incrocio-2
 (nodo Python)                                                   (nodo Python)
      │ USB seriale 115200                                             │ USB
 ┌────┴──────────┐        ESP-NOW broadcast, canale fisso        ┌─────┴─────────┐
 │ ESP32 gateway │ ))) ─────────────────────────────────────(((  │ ESP32 gateway │
 └───────────────┘                    │                          └───────────────┘
                                 ┌────┴──────────────┐
                                 │ ESP32 braccialetto │  vibra + beacon "vru" ogni 2 s
                                 └───────────────────┘
 (in alternativa, sulla stessa LAN: UDP multicast 239.255.42.99:47999, anche per i braccialetti con Wi-Fi)
```

| Cartella | Cosa fa |
|---|---|
| `esp32_gateway/` | Ponte trasparente tra **seriale USB** e **ESP-NOW broadcast**. Ogni riga JSON che arriva dalla UNO Q viene trasmessa a tutti gli ESP32 nel raggio. Ogni frame ESP-NOW ricevuto viene stampato sulla seriale come una riga. Non interpreta il JSON: dedup, relay (`h`), firma e rilevanza li gestisce il nodo Python. |
| `esp32_wearable/` | **Braccialetto**. Riceve i pericoli (`"t":"hz"`), decide se riguardano il profilo di chi lo indossa (ciclista, pedone o veicolo) e vibra con una sequenza diversa per gravità. Ogni 2 s trasmette un beacon di presenza `"vru"` che il nodo riceve tramite il gateway. |

## Protocollo SC-P2P v1 in breve

Ogni messaggio è un solo oggetto JSON compatto, UTF-8, di **massimo 240 byte**, così entra in un
frame ESP-NOW da 250 byte. I campi completi sono nella specifica del nodo; il firmware usa questi:

```json
{"v":1,"t":"hz","id":"incrocio-1-cam0-000123","n":"incrocio-1","a":"n","k":"emergency_vehicle",
 "c":"emergency","s":3,"cc":95,"ts":1760180000000,"e":1760180060000,"tg":["person","cyclist","vehicle"],"h":2}
{"v":1,"t":"hb","n":"incrocio-1","ts":1760180000000,"h":0}
{"v":1,"t":"vru","d":"wb-a1b2c3","p":"cyclist","ts":123456,"h":0}
```

- `a`: `n` significa nuovo, `c` significa annullato. `c` (categoria) può valere `emergency`, `violation`, `risk` o `info`. `s` è la severità da 1 a 3. `e` è la scadenza in ms epoch. `tg` elenca i profili interessati.
- Il **braccialetto vibra** solo se valgono tutte e tre le condizioni:
  - il suo profilo è in `tg`, oppure `tg` manca;
  - il messaggio non è scaduto;
  - `s >= 2` oppure `c == "emergency"`.

  Un messaggio con `a:"c"` non fa vibrare. Ogni coppia `(id, a)` viene ricordata per almeno 60 s e le copie ripetute o rilanciate da altri incroci vengono ignorate.
- **Tempo:** senza Wi-Fi il braccialetto non ha l'ora vera. Considera quindi il messaggio valido per `e - ts` ms dal momento in cui lo riceve. Con `WIFI_SSID` l'ora arriva via NTP e, quando `e` è un vero ms epoch, il braccialetto la confronta con `e`.
- **Firma `sig`:** il braccialetto **non** la verifica e accetta tutto. Nel caso peggiore produce una vibrazione falsa. La verifica HMAC la fanno i nodi. Il gateway inoltra la firma così com'è.
- I braccialetti **non fanno mai relay**. Il rilancio con `h-1` lo fanno solo i nodi.

### Sequenze di vibrazione

| Caso | Sequenza |
|---|---|
| `c == "emergency"` | 3 impulsi lunghi (0,7 s) |
| severità 3 | 3 impulsi brevi e veloci, ripetuti 2 volte |
| severità 2 | 2 impulsi brevi |
| `a == "c"`, severità 1, scaduto, altro profilo | nessuna vibrazione |
| conferma profilo | N impulsi brevi: 1 ciclista, 2 pedone, 3 veicolo |

Il LED della scheda si accende insieme al motore. Una sequenza più grave interrompe quella in corso.
Una meno grave aspetta che la sequenza in corso finisca.

## Cosa serve

- **Arduino IDE 2.x.**
- **Pacchetto schede:** in *Strumenti → Scheda → Gestore schede* cercare **"esp32" di Espressif Systems**. È consigliata la versione 3.x; il codice compila anche con la 2.0.x. Se il pacchetto non compare, aggiungere questo URL in *File → Preferenze → URL aggiuntivi*: `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
- **Libreria**, solo per il braccialetto: **ArduinoJson di Benoit Blanchon, versione 7.x**. Si installa da *Strumenti → Gestisci librerie*.
- **Hardware:**
  - 2 schede ESP32 qualsiasi (DevKit ESP32, ESP32-S3 o ESP32-C3);
  - per il braccialetto: motorino vibrante a moneta da 3 V, transistor NPN, resistenze, diodo e una batteria LiPo con caricatore, oppure un power bank.

## Come si carica il firmware (Arduino IDE)

1. Aprire `esp32_gateway/esp32_gateway.ino` oppure `esp32_wearable/esp32_wearable.ino`. Ogni sketch sta nella sua cartella, che ha lo stesso nome del file.
2. In *Strumenti → Scheda* scegliere **ESP32 Dev Module**, oppure il modulo reale (es. "ESP32S3 Dev Module", "ESP32C3 Dev Module").
3. In *Strumenti → Porta* scegliere la porta COM della scheda.
4. Premere **Carica**. Se l'upload non parte (`Connecting....`), tenere premuto BOOT finché inizia la scrittura.
5. Aprire il *Monitor seriale* a **115200** baud.

Le impostazioni si cambiano con i `#define` in cima agli sketch:

| Define | Default | Note |
|---|---|---|
| `WIFI_CHANNEL` | 1 | **Deve essere uguale su tutti i gateway e su tutti i braccialetti** |
| `VIBRO_PIN` | 4 | Pin del braccialetto collegato alla base del transistor |
| `BUTTON_PIN` | 0 | Tasto BOOT. Su ESP32-C3 è il **GPIO 9** |
| `LED_PIN` / `LED_ACTIVE_LOW` | `LED_BUILTIN` o 2 / 0 | Su ESP32-C3 SuperMini: `LED_ACTIVE_LOW 1` |
| `WIFI_SSID` / `WIFI_PASS` | commentati | Solo braccialetto: abilita la ricezione UDP multicast dalla LAN e la sincronizzazione NTP |

## Regola del canale

ESP-NOW funziona solo se mittente e ricevente sono sullo **stesso canale Wi-Fi**.

- Senza Wi-Fi (caso normale, offline): il canale è `WIFI_CHANNEL`, per default 1. Deve essere uguale in tutti gli sketch.
- Se sul braccialetto è definito `WIFI_SSID`, la radio segue il canale dell'access point e `WIFI_CHANNEL` viene ignorato.
  - Il canale scelto viene stampato all'avvio: `[wifi] connesso ... canale=X`.
  - Allora **tutti i gateway** devono avere `WIFI_CHANNEL X`.
  - L'access point deve stare su un **canale fisso**, non "auto".
- Il Wi-Fi della UNO Q non conta: il gateway ha una radio sua.

## Collegamento del motorino (braccialetto)

Un GPIO non regge la corrente del motorino (60-100 mA). Serve un transistor:

```
 3V3 (o VBAT della LiPo) ──┬─────────────┬──────────┐
                           │             │          │
                       [motorino]   diodo 1N4148  [100 nF]  (condensatore opzionale,
                           │        (catodo, la      │        contro i disturbi)
                           │         riga, verso +)  │
                           ├─────────────┴──────────┘
                           │
                           C
 GPIO 4 ──[ 1 kΩ ]────────B    NPN: 2N2222 / BC547 / BC337
              │            E
           [10 kΩ]         │
              │            │
 GND ─────────┴────────────┴──── GND ESP32
```

- **Resistenza di base da circa 1 kΩ:** circa 2,6 mA di base, sufficienti per circa 100 mA sul collettore.
- **Diodo di ricircolo** (1N4148 o 1N4007) **in parallelo al motorino**, con il catodo verso il +. Protegge il transistor dai picchi di tensione quando il motore si spegne.
- **Condensatore da 100 nF** in parallelo al motorino: facoltativo, riduce i disturbi delle spazzole.
- **Resistenza da 10 kΩ tra base e GND:** facoltativa. Tiene il motore spento durante il boot, quando il pin è ancora flottante.
- Il BC547 regge al massimo 100 mA. Per motori più grossi usare 2N2222 o BC337.
- Va bene anche un **modulo vibrazione già pronto**, con transistor e diodo a bordo: IN → GPIO 4, VCC → 3V3, GND → GND.

## Cambio profilo (tasto BOOT)

- **Pressione lunga (≥ 1,5 s):** passa al profilo successivo, nell'ordine ciclista → pedone → veicolo → ciclista. Il profilo viene salvato in flash (Preferences) e rimane dopo lo spegnimento.
- **Pressione breve:** ripete le N vibrazioni del profilo attuale. All'accensione il braccialetto vibra N volte.
- **Non tenere premuto BOOT mentre si accende o si resetta la scheda:** la scheda entrerebbe in modalità download.

## Collegamento del gateway alla UNO Q

- Il gateway si collega con un cavo USB alla UNO Q, che lo alimenta e ci parla in seriale.
- **La UNO Q ha una sola porta USB-C.** Serve quindi un **hub USB-C alimentato**, con Power Delivery. L'hub alimenta la board e ci si collegano sia il gateway ESP32 sia le webcam.
- Sulla UNO Q il gateway compare come uno di questi device:
  - **`/dev/ttyUSB0`**, per le schede con convertitore USB-seriale CP2102/CH340 (DevKit classiche);
  - **`/dev/ttyACM0`**, per le ESP32-S3/C3 con USB nativa.
- Per verificarlo, dalla shell della UNO Q: `ls /dev/ttyUSB* /dev/ttyACM*` oppure `dmesg | tail`. Se non compare nulla, il kernel potrebbe non avere il driver CP210x/CH34x: in quel caso usare una ESP32-S3/C3 con USB nativa (driver `cdc_acm`).
- Il device va indicato nella config del nodo:

  ```json
  "p2p": { "serial": { "port": "/dev/ttyUSB0" } }
  ```

- L'utente che esegue il nodo deve poter aprire la porta (gruppo `dialout`).
- Se il nodo gira dentro App Lab, va **verificato** che il container dell'app veda il device. Se non lo vede, ci sono due alternative: avviare il nodo da shell, oppure usare solo il trasporto UDP multicast.

### Convenzione seriale (per chi scrive `peer.py`)

- Formato 115200 8N1. Un oggetto JSON compatto per riga, terminato da `\n`. Il gateway tollera anche `\r\n`.
- **Le righe che iniziano con `#` sono commenti o stato del gateway e vanno ignorate.** Il lato Python deve comunque ignorare qualunque riga che non sia JSON valido. Esempi di righe `#`:
  - `# SC-P2P gateway pronto: mac=... canale=1 core=3.x.x ...` all'avvio;
  - `# SC-P2P gateway stato: ...` ogni 60 s;
  - `# errore: ...` per righe troppo lunghe (oltre 240 byte) o errori di invio.
- Anche le righe che iniziano con `#` inviate **al** gateway vengono ignorate. Le righe che non iniziano con `{` non vengono trasmesse.
- All'apertura della porta, le DevKit possono resettarsi. Aspettare la riga `# SC-P2P gateway pronto` oppure circa 1-2 s prima di scrivere.
- Il gateway stampa **tutto** ciò che riceve via ESP-NOW, quindi anche:
  - i beacon `vru` dei braccialetti, uno ogni 2 s per ciascun braccialetto;
  - gli `hb` degli altri incroci.

  Non stampa i frame che non sono testo JSON. Non riceve i propri invii.

## Prove

### Braccialetto da solo, senza gateway

Il braccialetto accetta messaggi anche dal suo monitor seriale.

1. Aprire il monitor a 115200 con fine riga **"Nuova riga"**.
2. Incollare:

   ```
   {"v":1,"t":"hz","id":"test-1","n":"incrocio-2","a":"n","k":"emergency_vehicle","c":"emergency","s":3,"cc":95,"ts":1000,"e":61000,"tg":["person","cyclist","vehicle"],"h":0}
   ```

   Il braccialetto fa 3 vibrazioni lunghe e stampa `... -> VIBRA: emergenza`.
3. Per ripetere la prova, **cambiare `id`** (`test-2`, `test-3`, ...): lo stesso `(id, a)` viene ignorato per 60 s.
4. Digitare `?` per stampare lo stato.

### Gateway e braccialetto

1. Caricare il gateway su un ESP32 e il braccialetto su un altro, con lo stesso `WIFI_CHANNEL`.
2. Aprire il monitor seriale **del gateway** (115200, "Nuova riga"). Ogni 2 s compaiono righe come `{"v":1,"t":"vru","d":"wb-...","p":"cyclist",...}`. Questo conferma che il braccialetto viene ricevuto.
3. Incollare nel monitor del gateway le righe di prova qui sotto. Il gateway le trasmette e il braccialetto reagisce.

| Riga | Risultato atteso (profilo ciclista) |
|---|---|
| `{"v":1,"t":"hz","id":"t-10","n":"incrocio-2","a":"n","k":"red_light_violation","c":"violation","s":3,"cc":98,"ts":1000,"e":31000,"tg":["person","cyclist"],"h":0}` | 3 impulsi brevi ripetuti 2 volte |
| `{"v":1,"t":"hz","id":"t-11","n":"incrocio-2","a":"n","k":"close_pass","c":"violation","s":2,"cc":97,"ts":1000,"e":21000,"tg":["cyclist"],"h":0}` | 2 impulsi brevi |
| `{"v":1,"t":"hz","id":"t-12","n":"incrocio-2","a":"n","k":"pedestrian_waiting","c":"info","s":2,"cc":12,"ts":1000,"e":61000,"tg":["vehicle"],"h":0}` | niente: non riguarda il profilo |
| `{"v":1,"t":"hz","id":"t-10","n":"incrocio-2","a":"c","k":"red_light_violation","c":"violation","s":3,"cc":98,"ts":1000,"e":31000,"h":0}` | niente: annullamento |
| `{"v":1,"t":"hz","id":"t-13","n":"incrocio-2","a":"n","k":"speeding","c":"violation","s":3,"cc":99,"ts":1000,"e":1000,"h":0}` | niente: scaduto (`e - ts = 0`) |

### Due incroci

Collegare un gateway a ciascuna UNO Q, oppure al PC con il nodo in simulazione. I pericoli con `nx` o `tw`
arrivano all'altro nodo via ESP-NOW anche senza rete. Il relay con `h` lo fa il Python.

## Limiti noti

- **ESP-NOW broadcast non è cifrato.** L'autenticità si ottiene con la firma HMAC `sig`, verificata dai nodi ma non dal braccialetto.
- **Portata:** con l'antenna sul PCB, circa 50-150 m in vista libera, meno tra gli edifici. Tra incroci lontani serve il relay (`h`) da parte degli incroci intermedi, oppure ESP32 con antenna esterna.
- **Batteria:** il braccialetto tiene la radio sempre accesa (circa 100 mA) per non perdere i broadcast. Una LiPo da 500 mAh dura qualche ora: va bene per la demo, non per l'uso reale.
- **Il gateway accetta solo payload fino a 240 byte.** I frame ESP-NOW v2, più lunghi di 250 byte, vengono ignorati.
- Le schede ESP8266 non sono supportate: hanno un'API ESP-NOW diversa.
