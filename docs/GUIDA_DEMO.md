# Guida: dal PC alla demo completa

Ordine consigliato: **Parte 1 → 2 → 3** (il nodo funziona e si vede sul PC). Solo dopo **4** (collega
da remoto) e **5** (bracciale del ciclista con ESP32).
Ogni passo ha un ✅ **controllo**: se non è verde, guardate la tabella dei problemi in fondo.

```
                Wi-Fi 2,4 GHz (hotspot del telefono o router), stessa rete per tutti
   +-----------------+        HTTP          +-------------------------------+
   | UNO Q           | -------------------> | PC: server.py + dashboard     | <--- collega (Parte 4)
   | "incrocio-1"    | <-- pericoli vicini- | http://<IP-PC>:8000           | <--- telefono /app
   | camera + 4 LED  |                      +-------------------------------+
   +-----------------+
        |  UDP peer-to-peer (porta 47999), anche SENZA server
        +--> UDP diretto, porta 48100 --> ESP32 "bracciale del ciclista" (LED): senza server
```

---

## Parte 1 — Rete e server sul PC

1. **Una sola rete per tutti**, a 2,4 GHz (l'ESP32 non vede i 5 GHz): l'hotspot di un telefono
   (su iPhone "Massimizza compatibilità") oppure il Wi‑Fi della sede, se non ha la *client isolation*.
   Collegate il PC a questa rete.
2. **IP del PC**: in PowerShell lanciate `ipconfig` e prendete l'"Indirizzo IPv4" della scheda Wi‑Fi
   (es. `192.168.43.20`). Lo chiameremo **IP‑PC**.
3. **Rete "privata" su Windows**, altrimenti il firewall blocca la board: Impostazioni → Rete e Internet →
   Wi‑Fi → la vostra rete → *Tipo di profilo di rete* → **Privato**.
4. **Avviate il server**:
   ```bash
   cd C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\server
   ```
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe server.py --port 8000
   ```
   La prima volta Windows chiede il permesso del firewall per Python: **Consenti** (reti private).
   Il server stampa gli indirizzi da usare. Lasciate questa finestra aperta.
5. Aprite la **dashboard** sul PC: `http://localhost:8000`.

✅ **Controllo**: dal **telefono** collegato alla stessa rete aprite `http://IP-PC:8000`: deve comparire la
dashboard. Se dal telefono non si apre, è il firewall (punto 3) o la client isolation della rete.

---

## Parte 2 — Il nodo sulla UNO Q

### 2.1 Board in rete
1. La board deve stare sulla **stessa rete** del PC. Se è su un'altra, cambiatela dal terminale di App Lab
   (via USB) con:
   ```bash
   sudo nmcli dev wifi connect "NOME-RETE" password "PASSWORD"
   ```
2. Trovate il suo IP (terminale di App Lab o SSH): `hostname -I` → il primo numero che **non** inizia con `172.`
   (lo chiameremo **IP‑BOARD**).
3. Aggiornate `HostName` in `C:\Users\Gabriele\.ssh\config` con IP‑BOARD e provate:
   ```bash
   ssh efesto "echo ok"
   ```

✅ Risponde `ok`.

### 2.2 Il modello nuovo
1. Edge Impulse → **Deployment** → **Arduino UNO Q** → **Build**.
2. App Lab → app **efesto** → brick **Video Object Detection** → **AI models** → sul vostro modello
   **Download/Update** → selezionatelo (*MODEL IN USE*).
3. **Run** con il `main.py` attuale e aprite la pagina dell'app.

✅ I riquadri dicono `person`, `cyclist`, `vehicle`, `emergency`.

### 2.3 Configurazione (sul PC, prima di copiare)
Aprite `C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\config.json` e modificate:
```json
"node_id": "incrocio-1",
"server": {"url": "http://IP-PC:8000", "token": "", "poll_s": 1.0},
"p2p": {"enabled": true, "peers": [], "key": "", "serial": {"port": ""}},
```
(l'indirizzo del bracciale ESP32 in `wearable.devices` lo aggiungete nella Parte 5).

Lasciate `"token": ""`. Il token è nel file `.env` nella radice della repo, che non va su GitHub
(il modello è `.env.example`). Il server lo legge da solo, mentre sulla board lo copiate al passo 2.4.

### 2.4 Copiare il software sulla board
Dal PC (PowerShell):
```bash
scp -r C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python efesto:~/sc_python
```
```bash
scp C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\sketch\sketch.ino efesto:~/sc_sketch.ino
```
Poi sulla board (terminale di VS Code collegato a `efesto`, oppure `ssh efesto`):
```bash
cp -n ~/ArduinoApps/efesto/python/main.py ~/main_originale.py
```
```bash
cp -n ~/ArduinoApps/efesto/sketch/sketch.ino ~/sketch_originale.ino
```
```bash
cp -r ~/sc_python/* ~/ArduinoApps/efesto/python/
```
```bash
mkdir -p ~/ArduinoApps/efesto/sketch
```
```bash
cp ~/sc_sketch.ino ~/ArduinoApps/efesto/sketch/sketch.ino
```
(`mkdir` serve se l'app era solo Python e non ha ancora la cartella `sketch/`.)
I backup vanno nella home e **non** nella cartella `sketch/`: due file `.ino` nella stessa cartella non compilano.

Il token: copiate il `.env` accanto a `config.json`. Il comando parte dal PC, in PowerShell, e il `cp -r ... *`
di prima non copia i file che iniziano con il punto:
```bash
scp C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\.env efesto:~/ArduinoApps/efesto/python/.env
```

> D'ora in poi, quando cambiate solo la configurazione, basta:
> ```bash
> scp C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\config.json efesto:~/ArduinoApps/efesto/python/
> ```
> e poi **Stop → Run** in App Lab.

### 2.5 I 4 LED
Collegamento ad **anodo comune**, a board **spenta**. È quello della nostra breadboard, con `COMMON_ANODE true` nello sketch:
- **gamba lunga** di tutti i LED sul binario comune, collegato al pin **3.3V** della board. Meglio del 5V, perché i pin lavorano a 3,3 V;
- **gamba corta → resistenza → pin** della tabella, al posto del filo che prima andava a GND.

Il LED si accende quando il pin va a GND.

| Pin | LED | Significato |
|---|---|---|
| D2 | LED 1 | pericolo visto da questo incrocio (acceso / lampeggio veloce se grave) |
| D3 | LED 2 | pericolo segnalato da un incrocio vicino (lampeggio) |
| D4 | LED 3 | mezzo di emergenza (lampeggio veloce) |
| D5 | LED 4 | acceso = collegato al server o a un vicino, lampeggio lento = offline |

I pin lavorano a 3,3 V: con le resistenze già montate i LED si accendono, magari un po' meno forte
(non scendete sotto ~150 Ω).

### 2.6 Avvio
1. App Lab → **Run**: compila lo sketch e avvia il Python.
2. All'accensione i LED si accendono **uno dopo l'altro** (test), poi il LED 4 resta acceso finché il collegamento
   Python ↔ MCU è pronto.
3. Se la compilazione dice che manca `Arduino_RouterBridge.h`: **Sketch Libraries → +** → aggiungete
   **Arduino_RouterBridge** e rifate Run.

✅ **Controlli** (in **Open console**):

| Riga in console | Significato |
|---|---|
| nessuna riga `[config]` | configurazione valida (se ci sono, leggetele: dicono cosa sistemare) |
| `[invio] server: http://IP-PC:8000/api/events` | il nodo sa dove mandare i dati |
| `[server] collegato a http://IP-PC:8000` | il nodo riceve dal server; **LED 4 acceso fisso** |
| `[stato cam0] ... coordinate max viste=(x, y)` | se x, y superano 640×480 mettete i valori veri in `frame_size` |

Sulla dashboard **incrocio‑1** diventa verde e ogni 10 s arrivano FPS, temperatura e tracce attive.

### 2.7 Le zone
Come in TUTORIAL.md, fase 6: screenshot del solo video → `zone_editor.py` → copiate `config.json` → Stop/Run.
**Fissate la camera prima**: se si sposta, le zone vanno ridisegnate.

---

## Parte 3 — Provare i casi

Per ogni prova guardate: **console** di App Lab (`[PERICOLO] ...`), **LED**, **dashboard**.
Le auto si provano con un **video di traffico su tablet/telefono davanti alla camera**; le persone dal vivo.

| # | Prova | Come farla | Cosa deve succedere |
|---|---|---|---|
| A | riconoscimento | passate davanti alla camera | console: transito `person`; dashboard: tracce |
| B | pedone fuori dalle strisce | restate 2 s nella carreggiata, fuori dalle strisce | `pedestrian_outside_crosswalk`, LED 1 |
| C | pedone lento | attraversate le strisce molto lentamente | `slow_pedestrian`, LED 1 |
| D | auto in ciclabile | nel video, un'auto dentro la zona `bike_lane` | `bike_lane_invasion`, LED 1; in dashboard l'inoltro verso l'incrocio dove va l'auto |
| E | emergenza | foto/video di un'ambulanza davanti alla camera, poi toglietela | LED 3 lampeggio veloce; dopo 2 s senza ambulanza "terminato" e LED 3 spento |
| F | pericolo da un vicino | dashboard → **Simula pericolo** da incrocio‑2 verso incrocio‑1 | LED 2 lampeggia; console `[PERICOLO VICINO] ... da=incrocio-2` |
| G | offline | fermate il server (Ctrl+C) per 30 s, poi riavviatelo | LED 4 lampeggia lento, il nodo continua a vedere e accendere il LED 1; al riavvio la dashboard riceve tutto quello che era in coda |
| H | fail‑safe | in App Lab premete **Stop** | l'MCU spegne i LED e fa lampeggiare il LED 4 ogni 2 s |

---

## Parte 4 — Far vedere i dati al collega

**Sulla stessa rete** (il caso più semplice):
- dashboard: `http://IP-PC:8000`
- pagina telefono (avvisi + vibrazione su Android): `http://IP-PC:8000/app?node=incrocio-1` → scegliete il profilo e premete **Avvia**.

**Da un'altra rete** (il collega è a casa): due modi.

**A. Tailscale — privato, consigliato.**
1. Installate Tailscale sul PC:
   ```bash
   winget install tailscale.tailscale
   ```
2. Fate il login, e fatelo fare anche al collega sul suo PC o telefono, invitandolo nella vostra rete Tailscale.
3. Il collega apre `http://IP-TAILSCALE-PC:8000`: è l'indirizzo `100.x.y.z` che Tailscale mostra per il vostro PC.

**B. Link pubblico immediato con Cloudflare, senza account.**
1. Installate cloudflared:
   ```bash
   winget install --id Cloudflare.cloudflared
   ```
2. Controllate che il server parta con il token: all'avvio deve stampare `.env: ...` e `token: richiesto ai dispositivi`.
   Con il link pubblico chiunque **vede** la dashboard, ma senza `SC_TOKEN` nessuno può mandare dati o simulare.
3. Aprite il tunnel:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
4. Mandate al collega il link `https://....trycloudflare.com` che compare. La dashboard gli chiede il token solo se usa **Simulate**.
5. Chiudete il tunnel (Ctrl+C) quando avete finito.

In entrambi i casi la board continua a parlare con il PC sulla rete locale: condividete solo la dashboard.

**C. Server sempre online su PythonAnywhere.** Lì basta scaricare 11 file della repo: `src/server/`, `events.py` e `app.html`.
Da una console Bash di PythonAnywhere:
```bash
cd ~/HackTurin_2026
```
```bash
git sparse-checkout set --no-cone /src/server/ /src/node/python/events.py /src/node/python/web/app.html
```
```bash
git pull
```
Poi create il file `~/HackTurin_2026/.env` con la riga `SC_TOKEN=...`, la stessa del `.env` del PC, e premete
**Reload** nella scheda **Web**. Il file WSGI può restare `from server import app as application`.
Sulla board: `"server": {"url": "https://<utente>.pythonanywhere.com"}`.

---

## Parte 5 — Il bracciale del ciclista (ESP32, canale diretto)

L'incrocio più vicino (la UNO Q, `incrocio-1`) manda gli allarmi **direttamente** al bracciale: un
pacchetto UDP all'indirizzo del bracciale, porta **48100**. Non passa dal server e non serve internet,
basta la stessa rete Wi‑Fi. È un canale separato da quello tra incroci.
Il LED del bracciale lampeggia quando un'auto **entra** nella ciclabile, quando un'auto è **ferma** nella ciclabile
o quando c'è un **sorpasso troppo vicino**, e si spegne appena l'incrocio non vede più il pericolo.

1. **Arduino IDE 2** → Boards Manager → **esp32 by Espressif Systems**. Non servono librerie.
2. **LED:** gamba lunga → resistenza 220–330 Ω → **GPIO 4**; gamba corta → **GND**.
3. Aprite `src/firmware/esp32_bracciale/esp32_bracciale.ino` e compilate `WIFI_SSID` e `WIFI_PASS` (la rete della
   UNO Q, a 2,4 GHz). Se la scheda è una **ESP32‑C3 SuperMini**, togliete le `//` a `#define SCHEDA_C3_SUPERMINI`.
4. Scegliete la scheda:
   - ESP32 normale: **ESP32 Dev Module**;
   - C3 SuperMini: **ESP32C3 Dev Module**, con *USB CDC On Boot: Enabled*.

   Poi **Carica**.
5. Aprite il **Monitor seriale a 115200**: compare `[wifi] collegato, ip=192.168.x.y`. Questo è l'**IP del bracciale**.
6. Nel `config.json` dell'incrocio:
   ```json
   "wearable": {"enabled": true, "devices": ["192.168.x.y"], "broadcast": true, "port": 48100},
   ```
   Copiate il config sulla board e fate **Stop → Run**.
7. ✅ **Controlli:**
   - **LED della scheda del bracciale acceso fisso** = l'incrocio è collegato. Nel Monitor seriale compare `[collegato] ricevo dall'incrocio incrocio-1`.
   - **Auto nella zona ciclabile** (video sul telefono davanti alla camera): il LED su GPIO 4 lampeggia e nel monitor compare `[ALLARME] Auto nella ciclabile`. Quando l'auto esce: `[fine allarme]`.
   - **Dashboard → Nodes:** accanto a incrocio‑1 c'è `wristband ✓`, che diventa `wristband ALERT` durante l'allarme.
   - **Prova senza incrocio:** scrivete `t` nel Monitor seriale per un allarme di prova di 3 s.

---

## Problemi frequenti

| Problema | Soluzione |
|---|---|
| Il telefono non apre `http://IP-PC:8000` | rete Windows impostata su "Pubblica" (Parte 1.3), firewall, oppure client isolation della rete |
| In console `[server] ... non raggiungibile` | `server.url` sbagliato (deve essere `http://IP-PC:8000`, senza `/api`), oppure server spento |
| `[server] ... token rifiutato` | il `.env` sulla board (accanto a `config.json`) deve avere lo stesso `SC_TOKEN` del server |
| La compilazione fallisce su `Arduino_RouterBridge.h` | aggiungete la libreria Arduino_RouterBridge allo sketch (Parte 2.6) |
| Il LED 4 lampeggia ogni 2 s e gli altri sono spenti | il Python non comanda l'MCU: app ferma o in errore, guardate la console |
| I LED non si accendono mai | gamba lunga e gamba corta invertite, oppure LED ancora collegato al 5 V |
| Il bracciale non riceve niente (LED della scheda che lampeggia ogni secondo) | stessa rete a 2,4 GHz? IP giusto in `wearable.devices`? `wearable.enabled: true`? config copiato e Stop → Run? |
| Troppi pericoli falsi | zone non allineate all'inquadratura: ridisegnatele; poi alzate le soglie in `thresholds` |
