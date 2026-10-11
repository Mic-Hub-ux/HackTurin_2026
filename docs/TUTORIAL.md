# Tutorial completo: dal dataset al nodo funzionante sulla board

Seguite le fasi **in ordine**. Ogni fase finisce con un ✅ **controllo**: se non è verde, fermatevi e risolvete prima di andare avanti (in fondo c'è la tabella dei problemi frequenti).
Le spiegazioni sul *perché* delle cose sono in [PIANO.md](PIANO.md); qui c'è solo il *cosa fare*.

| Fase | Cosa | Dove | Tempo |
|---|---|---|---|
| 1 | Costruire il dataset | PC | ✔ già fatta, 5–10 min se la rifate |
| 2 | Addestrare il modello | Edge Impulse | 1–2 h, quasi tutto attesa |
| 3 | Mettere il modello sulla board | App Lab | 10 min |
| 4 | Collegarsi alla board | PC | 5 min |
| 5 | Installare il "cervello" del nodo | PC + board | 10 min |
| 6 | Disegnare le zone della camera | PC + board | 15 min |
| 7 | Provare le regole sul PC | PC | 20 min, **si può fare durante la fase 2** |
| 8 | Prova dal vivo | board | 30 min |
| 9 | Collegare il server | config | 5 min |

---

## Fase 1 — Costruire il dataset ✔ (già fatta)

L'ho già lanciata io con i vostri due video e le vostre foto. Il risultato è in `dataset\dataset_oi\data`:

| Classe | Box | Immagini |
|---|---|---|
| `person` | 20.085 | |
| `vehicle` | 3.784 | |
| `emergency` | 1.834 | |
| `cyclist` | 659 | |
| **totale** | | **6.454** (circa 1,8 GB) |

Dai video sono usciti 175 frame, uno al secondo, ridotti da 4K a 1280 px. In tutto le immagini vostre sono 345.

**Se aggiungete video o foto, rifatela così:**
1. Mettete i video `.mp4` in `dataset\video\`, le foto di strada in `dataset\foto_nostre\strada\` e quelle di mezzi di emergenza in `dataset\foto_nostre\emergency\`.
2. Aprite PowerShell nella cartella del dataset:
   ```bash
   cd C:\Users\Gabriele\Desktop\Hackaton\dataset
   ```
3. Lanciate lo script:
   ```bash
   .venv\Scripts\python.exe prepara_dataset.py
   ```
4. (Facoltativo) Controllate le box nel browser (Ctrl+C in PowerShell per chiudere):
   ```bash
   .venv\Scripts\python.exe prepara_dataset.py --guarda
   ```

✅ **Controllo:** la riga `Box per classe` mostra solo le 4 classi e nessun avviso `ATTENZIONE`.

> **Versione leggera.** Se nella fase 2 l'upload è troppo lento o l'addestramento supera il limite di tempo del piano gratuito, rifate il dataset con meno foto di sole persone:
> ```bash
> .venv\Scripts\python.exe prepara_dataset.py --leggero
> ```

---

## Fase 2 — Addestrare il nuovo modello (Edge Impulse)

Aprite studio.edgeimpulse.com → progetto **efesto** (quello collegato ad App Lab).

### 2.1 Togliere i dati vecchi
1. Menu a sinistra → **Data acquisition**.
2. Spuntate la casella in cima alla lista per selezionare tutto. Se vi chiede "seleziona tutti i campioni", non solo la pagina, dite sì.
3. Premete il **cestino** (Delete) e confermate.

✅ La lista è vuota sia in **Training** sia in **Test**.

### 2.2 Caricare il nuovo dataset
1. Prima: **impostate il PC perché non vada in sospensione** e tenete la scheda del browser aperta.
2. **Data acquisition** → **Upload data**, l'icona con la freccia in su.
3. Impostate le opzioni:
   - **Select a folder** → `C:\Users\Gabriele\Desktop\Hackaton\dataset\dataset_oi\data`;
   - **Upload into category** → **Automatically split between training and testing**;
   - **Label**: non scrivete nulla, le etichette sono nel file `_annotations.coco.json`.
4. Premete **Upload data**. Il log a destra scorre file per file: con 1,8 GB ci vuole da mezz'ora a un'ora.

✅ **Controllo:**
- In alto la divisione è circa **80% / 20%**.
- Aprendo 5–6 immagini a caso si vedono i **riquadri colorati**.
- Le etichette sono solo `person`, `cyclist`, `vehicle`, `emergency`.

### 2.3 Target e impulse
1. In alto a destra, **Target** → **Arduino UNO Q**.
2. **Impulse design** → **Create impulse**. Deve esserci quello di prima: Image data **320×320**, *Squash*, blocco **Image** e blocco **Object Detection**. Se c'è, non toccate niente. Se le classi risultano vecchie, premete **Save Impulse**.
3. **Image** → Color depth **RGB** → **Save parameters** → **Generate features**.

✅ Il log finisce con **Job completed**.

### 2.4 Addestramento di prova (10 cicli)
1. **Object detection** → modello **YOLO‑Pro** (il più piccolo), oppure **MobileNetV2 SSD FPN‑Lite 320×320**.
2. **Training cycles: 10**, Data augmentation: attiva.
3. Premete **Save & train**.

✅ **Controllo:**
- Nel log la `loss` scende di ciclo in ciclo.
- Alla fine compare **mAP**.
- **On-device performance** (con target UNO Q) mostra un tempo di inferenza. Sotto ~300 ms va bene; se è molto più alto, vedi i problemi frequenti.

Il giro di prova serve solo a verificare che tutto il percorso funzioni: **fate subito le fasi 2.6 e 3 con questo modello**, poi tornate qui per il 2.5.

### 2.5 Addestramento vero (50 cicli)
1. **Object detection** → **Training cycles: 50** → **Save & train**.
2. Se il job si interrompe per il limite di tempo, provate con 30 cicli, oppure rifate la fase 1 con `--leggero` e ricaricate.

### 2.6 Test e build per la board
1. **Model testing** → **Classify all**. Guardate la tabella per classe: `cyclist` ed `emergency` sono le più difficili.
2. **Deployment** → target **Arduino UNO Q** → **Build**.

✅ In **Latest build** compare la build appena fatta.

---

## Fase 3 — Il modello sulla board (App Lab)

1. Accendete la board con carrier e camera, aprite App Lab e aprite la vostra app (**efesto**).
2. Brick **Video Object Detection** → scheda **AI models**.
3. Accanto al vostro modello (**efesto**) premete **Download** o **Update** per prendere la versione nuova, e aspettate "Installing model…".
4. Selezionatelo con il **cerchio a sinistra**: deve comparire **MODEL IN USE**.
5. Premete **Run** e poi aprite la pagina dell'app (l'indirizzo lo vedete in App Lab, porta 7000).

✅ **Controllo:** nella pagina i riquadri dicono `person` (passate voi davanti alla camera) e `vehicle` (mostrate una foto di un'auto dal telefono). Se vedete ancora le etichette vecchie (`adult`, …), il modello non è stato aggiornato: ripetete i punti 3–4.

---

## Fase 4 — Collegarsi alla board

1. In App Lab aprite il **terminale** della board (icona `>_` in basso) e scrivete:
   ```bash
   hostname -I
   ```
   L'IP giusto è il primo numero che **non** inizia con `172.` (quelli sono interni). Ad esempio `10.73.6.154`.
2. Sul PC aprite il file di configurazione SSH:
   ```bash
   notepad $env:USERPROFILE\.ssh\config
   ```
   Controllate che `HostName` sia l'IP giusto, poi salvate:
   ```
   Host efesto
       HostName 10.73.6.154
       User arduino
   ```
3. Provate:
   ```bash
   ssh efesto "echo ok"
   ```

✅ Risponde `ok` (con o senza richiesta di password).

---

## Fase 5 — Installare il "cervello" del nodo

1. Controllate il nome della cartella dell'app (deve comparire `efesto`):
   ```bash
   ssh efesto "ls ~/ArduinoApps"
   ```
2. Fate una copia di sicurezza del `main.py` originale. Basta la prima volta:
   ```bash
   ssh efesto "cp -n ~/ArduinoApps/efesto/python/main.py ~/ArduinoApps/efesto/python/main_originale.py"
   ```
3. Copiate il software del nodo:
   ```bash
   scp C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\*.py C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\config.json efesto:~/ArduinoApps/efesto/python/
   ```
4. In App Lab premete **Stop** e poi **Run**. Aprite **Open console**.
5. Passate davanti alla camera. Ogni 5 secondi compare una riga di stato, ad esempio `[stato] tracce=1 semaforo=unknown coordinate max viste=(612.3, 470.1) spazio=image`.

✅ **Controllo:**
- Nessuna riga `errore situation awareness`.
- Quando uscite dall'inquadratura, dopo 2 s, compare un transito nel log.
- I numeri di `coordinate max viste` dicono come impostare `frame_size`:

| `coordinate max viste` | Cosa fare |
|---|---|
| sotto 640 e 480 | va bene così |
| più grandi (es. 1280, 720) | mettete quei valori in `"frame_size"` dentro `node\python\config.json`, ricopiatelo (fase 6.4) e riavviate |
| sotto 1 | va bene così |

---

## Fase 6 — Disegnare le zone della camera vera

Le zone dicono al nodo **dov'è** la ciclabile, dove sono le strisce, la linea continua, le corsie e il loro verso. **Vanno rifatte ogni volta che spostate la camera.**

### 6.1 La foto della camera
1. Con l'app in esecuzione aprite la pagina dell'app nel browser.
2. Premete **Win+Shift+S** e ritagliate **solo il riquadro del video**, bordi esatti, senza pezzi di pagina.
3. Aprite lo screenshot (notifica in basso a destra), premete **Salva**, e salvatelo come `C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\tools\foto.png`.

### 6.2 La calibrazione: le misure a terra
Scegliete **un rettangolo a terra** che compaia nell'inquadratura e misuratelo con un metro: ad esempio il rettangolo delle strisce, un tratto di corsia, o un rettangolo fatto con il nastro sul pavimento. Annotate la **larghezza** (sinistra–destra) e la **lunghezza** (vicino–lontano) in metri.

### 6.3 Disegnare
```bash
cd C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\tools
```
```bash
C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe zone_editor.py foto.png
```
Comandi: **click sinistro** aggiunge un punto, **INVIO** conferma, **u** annulla l'ultimo punto, **s** salta l'elemento, **ESC** salva ed esce.

Il programma chiede, uno alla volta:
1. **Calibrazione**: i 4 angoli del rettangolo nell'ordine **lontano‑sinistra, lontano‑destra, vicino‑destra, vicino‑sinistra**. Poi, nella finestra di PowerShell, scrivete larghezza e lunghezza in metri.
2. **Carreggiata**: il contorno della strada dove passano le auto, esclusa la ciclabile.
3. **Ciclabile**: il contorno della pista ciclabile. Se non c'è, premete **s**.
4. **Strisce pedonali**: il loro contorno.
5. **Linea continua**: 2 o più punti lungo la linea. Se non c'è, **s**.
6. **Corsia verso l'incrocio**: il contorno della corsia, poi **2 punti per la direzione** (da dove arrivano le auto → dove vanno).
7. **Corsia che si allontana**: come la 6.
8. **via**: una fascia in fondo alla strada.
9. **incrocio**: una fascia vicina alla camera.

✅ **Controllo:**
- PowerShell scrive `salvato in …config.json`.
- Rilanciando l'editor sulla stessa foto, vedete disegnate le zone che avete fatto (premete ESC per uscire senza cambiare niente).

### 6.4 Copiare sulla board
```bash
scp C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\config.json efesto:~/ArduinoApps/efesto/python/
```
Poi in App Lab: **Stop** → **Run**.

✅ Nella riga `[stato]` ora c'è **`spazio=world`**: vuol dire che la calibrazione è attiva e le velocità sono in km/h.

---

## Fase 7 — Provare le regole sul PC (anche durante la fase 2)

### 7.1 Test automatico
```bash
cd C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\tools
```
```bash
C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe simulate.py
```
✅ L'ultima riga è **`TUTTO OK`**: tutte e 15 le situazioni danno esattamente gli eventi attesi.

### 7.2 Prova sui vostri video
Ho già salvato il primo frame dei due video: `foto_video_auto.png` e `foto_video_autostrada.png` (in `node\tools`). Per ognuno dei due video:

1. Create una configurazione separata, così non toccate quella della board:
   ```bash
   copy ..\python\config.json config_auto.json
   ```
2. Disegnate le zone **su quel video**, come nella fase 6.3. Per la calibrazione, usate una misura nota della strada, ad esempio una corsia larga circa 3,5 m:
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe zone_editor.py foto_video_auto.png config_auto.json
   ```
3. Fate girare il nodo sul video:
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe replay.py "C:\Users\Gabriele\Desktop\Hackaton\dataset\video\Cars Moving On Road Stock Footage - Free Download_1080p.mp4" --config config_auto.json
   ```
   Nella finestra vedete:
   - le zone;
   - ogni oggetto con ID, traiettoria e km/h;
   - **in rosso** gli eventi quando scattano.

   Tasti: **spazio** pausa, **q** esci.
4. Per salvare il video annotato, utile per la demo, aggiungete `--save risultato_auto.mp4` al comando del punto 3.
5. Ripetete i passi 1–4 per l'autostrada. Al passo 1 create `config_autostrada.json` con lo stesso comando `copy`.
   - Al passo 2 usate `foto_video_autostrada.png` e `config_autostrada.json`.
   - Al passo 3:
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe replay.py "C:\Users\Gabriele\Desktop\Hackaton\dataset\video\4K Video of Highway Traffic!_2160p.mp4" --config config_autostrada.json
   ```

✅ **Controllo, guardando il video:**
- Ogni auto ha **un solo ID** mentre attraversa la scena, tranne ai tagli di scena.
- Gli eventi rossi corrispondono a quello che succede davvero.
- Se una regola scatta troppo o troppo poco, cambiate la soglia in `"thresholds"` del config e rilanciate.

⚠️ Questi due video sono riprese stock con **tagli di scena**: a ogni taglio le tracce ripartono e la "velocità tipica" mescola scene diverse. È normale qualche avviso di velocità in più. Con una camera fissa vera (fasi 6 e 8) il problema non c'è, e con la calibrazione la velocità è in km/h veri.

---

## Fase 8 — Prova dal vivo sulla board

Con l'app in esecuzione e la console aperta, provocate ogni situazione e controllate che compaia la riga `[PERICOLO] …`.

| Evento | Come provocarlo nella demo |
|---|---|
| `pedestrian_outside_crosswalk` | una persona sta per 2 s in carreggiata, fuori dalle strisce |
| `pedestrian_not_yielded` | una persona sulle strisce e un'auto (anche in video, sullo schermo) che le attraversa |
| `near_miss` | un'auto passa a meno di 2 m da una persona |
| `bike_lane_invasion` | un'auto entra nella ciclabile; se esce e rientra, scatta come "ripetuta" |
| `illegal_stop` | un'auto ferma per più di 5 s sulle strisce o sulla ciclabile |
| `solid_line_crossing` | un'auto attraversa la linea continua |
| `wrong_way` | un'auto va nel verso opposto alla freccia della corsia per più di 1 s |
| `speeding` / `sudden_acceleration` / `harsh_braking` | un video sullo schermo con un'auto veloce, che accelera o che frena di colpo |
| `emergency_vehicle` | una foto o un video di un'ambulanza davanti alla camera |

Per vedere tutti gli eventi salvati sulla board:
```bash
ssh efesto "tail -n 20 ~/ArduinoApps/efesto/python/events.jsonl"
```

✅ **Controllo:** ogni situazione provocata dà il suo evento, e quando non succede niente di pericoloso **non compare niente**.

> **Scena consigliata per la demo**
> - uno **schermo** davanti alla camera con video di traffico vero, per le regole sulle auto;
> - una **strada di nastro sul pavimento**, con voi che fate i pedoni e i ciclisti.
>
> Dettagli in PIANO.md, sezione 5.

---

## Fase 9 — Collegare il vostro server

1. Aprite `C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\config.json` e alla voce `"publish"` mettete l'indirizzo del server:
   ```json
   "publish": {"url": "http://INDIRIZZO-SERVER:PORTA/eventi", "token": "", "log_file": "events.jsonl", "send_flows": true}
   ```
2. Ricopiate il config sulla board (fase 6.4) e riavviate l'app.
3. Ogni evento arriva al server in **POST** come JSON. Se il server è irraggiungibile, gli eventi restano in coda in `outbox.jsonl` e partono quando torna.

✅ Il server riceve gli eventi della fase 8.

---

## Problemi frequenti

| Problema | Soluzione |
|---|---|
| `ssh` va in timeout | la board ha cambiato IP o il PC è su un'altra rete: rifate la fase 4 |
| Upload su Edge Impulse lentissimo o interrotto | rifate la fase 1 con `--leggero`, cancellate i dati (2.1) e ricaricate |
| Il job di training supera il tempo | meno cicli (30), oppure dataset `--leggero` |
| Inferenza sopra 600 ms sulla board | in Create impulse scegliete la versione più piccola di YOLO‑Pro; in ultima istanza 224×224 |
| In pagina compaiono le etichette vecchie | App Lab usa ancora il modello vecchio: fase 3, punti 3–4 |
| In console compare `errore situation awareness` | mandatemi la riga completa |
| Nessun evento anche quando dovrebbe | le zone non corrispondono all'inquadratura (camera spostata?): rifate la fase 6 |
| Troppi eventi falsi | alzate la soglia corrispondente in `"thresholds"`; i nomi delle soglie sono in `node\python\situation.py` nella tabella `SOGLIE` |
| `spazio=image` invece di `world` | manca la calibrazione: fase 6.3, punto 1 |
