# Smart Crossing — piano di lavoro passo per passo

Questo documento spiega **cosa fare, in che ordine, e come verificare** che ogni pezzo funzioni.
Ogni fase finisce con un ✅ **controllo**: non passate alla fase dopo finché il controllo non è verde.

---

## 0. Cosa c'è nella cartella

```
Hackaton/
├── PIANO.md                    questo file
├── dataset/                    tutto ciò che serve per ADDESTRARE il modello (sul PC)
│   ├── prepara_dataset.py      costruisce il dataset e lo esporta per Edge Impulse
│   ├── video/                  ← i vostri video di strada (un frame al secondo diventa foto)
│   ├── foto_nostre/strada/     ← foto di auto, ciclisti, incroci (62 foto già dentro)
│   ├── foto_nostre/emergency/  ← foto di ambulanze, polizia, pompieri (109 già dentro)
│   ├── roboflow/emergency/     dataset Roboflow di mezzi di emergenza (già scaricato)
│   └── dataset_oi/data/        RISULTATO: la cartella da caricare su Edge Impulse
└── node/                       il software del NODO INCROCIO
    ├── python/                 ← va copiato sulla UNO Q, nella cartella python/ dell'app
    │   ├── main.py             collega camera → modello → situation awareness → eventi
    │   ├── situation.py        il "cervello": capisce i comportamenti pericolosi
    │   ├── tracker.py          segue ogni oggetto nel tempo
    │   ├── geometry.py         zone, linee, conversione pixel → metri
    │   ├── traffic_light.py    stato del semaforo (spento di default: il nodo non lo vede)
    │   ├── publisher.py        salva gli eventi e li manda al server (anche offline)
    │   └── config.json         zone, linee, corsie, soglie: LA configurazione dell'incrocio
    └── tools/                  strumenti da usare sul PC
        ├── simulate.py         test automatico di tutte le regole (senza camera)
        ├── replay.py           fa girare il nodo su un VIDEO vero e mostra cosa capisce
        └── zone_editor.py      disegnate zone e linee cliccando su una foto della camera
```

Il flusso completo è questo:

```
camera ─► modello AI (riconosce: person, cyclist, vehicle, emergency)
       ─► tracker (segue ogni oggetto: dove va, a che velocità, da che via entra/esce)
       ─► regole (capisce i comportamenti pericolosi)
       ─► eventi JSON ─► log sulla board + pagina web + server (che avvisa l'incrocio dopo e i ciclisti)
```

---

## 1. Il dataset (sul PC)

### 1.1 Cosa riconosce il modello

| Classe | Cosa contiene | Perché serve |
|---|---|---|
| `person` | adulti e bambini insieme | pedoni sulle strisce o fuori |
| `cyclist` | UNA box: persona + bicicletta | i ciclisti da proteggere (la bici parcheggiata NON conta) |
| `vehicle` | auto, furgoni, bus, moto con chi la guida | i comportamenti pericolosi li fanno loro |
| `emergency` | ambulanze, polizia, pompieri | priorità e avvisi |

### 1.2 Da dove arrivano le immagini

| Sorgente | Cosa dà | Note |
|---|---|---|
| Open Images (già scaricato) | persone, ciclisti, ambulanze | le auto che compaiono vengono etichettate, ma non scarichiamo auto apposta (sono vecchie/straniere) |
| Roboflow `emergency` (già scaricato) | ambulanze, pompieri, polizia, auto | coreano: serve per la forma dei mezzi |
| `foto_nostre/emergency/` (109) | mezzi di emergenza italiani | il veicolo più grande della foto diventa `emergency` |
| `foto_nostre/strada/` (62) | auto italiane recenti, ciclisti, incroci | etichettate in automatico |
| `video/` | **la fonte migliore**: strade vere di Torino | un frame al secondo, etichettato in automatico |

Le foto vostre vengono **etichettate in automatico** da un modello YOLO già addestrato. Le regole sono:
- una persona sopra una bici diventa `cyclist`, con una box che le comprende entrambe;
- una persona sopra una moto diventa `vehicle`;
- le altre persone diventano `person`;
- auto, bus e camion diventano `vehicle`.

Le 12 foto di ciclisti le ho già provate: in 11 su 12 il ciclista viene riconosciuto correttamente.

### 1.3 Il video di Torino (fortemente consigliato)

Le foto dal web sono utili, ma il modello impara meglio da **immagini simili a quelle che vedrà la camera**:

1. Andate a un incrocio vicino, con strisce, auto e possibilmente una ciclabile.
2. Tenete il telefono **in alto e inclinato verso il basso**, a circa 30–45°, come sarà la camera: da un muretto, un ponte, una finestra, o con il braccio alzato.
3. Girate in **orizzontale**, 5–10 minuti in tutto, anche in più spezzoni, e cambiate punto ogni tanto.
4. Copiate i file `.mp4` in `dataset/video/`.

⚠️ Il video serve solo per addestrare: **non pubblicatelo** nel repo, perché ci sono persone e targhe riconoscibili.

### 1.4 Costruire il dataset

Aprite PowerShell nella cartella del dataset:
```bash
cd C:\Users\Gabriele\Desktop\Hackaton\dataset
```
```bash
.venv\Scripts\python.exe prepara_dataset.py
```
Ci vogliono alcuni minuti: ogni foto vostra passa dal modello che etichetta. Per controllare le box a occhio:
```bash
.venv\Scripts\python.exe prepara_dataset.py --guarda
```
Si apre il browser su `localhost:5151`. In alto potete filtrare per etichetta: guardate una ventina di `cyclist` e una ventina di `emergency`. Per chiudere premete Ctrl+C in PowerShell.

✅ **Controllo**
- La riga `Box per classe` mostra solo `person`, `cyclist`, `vehicle`, `emergency`.
- Nessuna classe ha l'avviso `ATTENZIONE` (sotto 300 box). Se `cyclist` è basso, aggiungete un video con bici o altre foto in `foto_nostre/strada/` e rilanciate.
- Nelle foto controllate a occhio, le box `cyclist` contengono persona **e** bici.

---

## 2. Addestramento su Edge Impulse

È lo stesso procedimento che avete già fatto, con queste differenze.

1. **Data acquisition** → selezionate tutto → **Delete**. Le classi sono cambiate, quindi il dataset vecchio va tolto.
2. **Upload data** → cartella `dataset\dataset_oi\data` → *Automatically split* → **Upload**.
3. ✅ Aprite qualche immagine: le etichette devono essere solo le 4 classi.
4. **Impulse design** → **Object detection** → **Save & train** con **50 cicli** (il resto invariato: 320×320, YOLO‑Pro, augmentation).
5. **Model testing** → **Classify all**.
6. **Deployment** → **Arduino UNO Q** → **Build**.
7. In App Lab: brick → **AI models** → scaricate il modello aggiornato → selezionatelo.

✅ **Controllo:** nella pagina web dell'app i riquadri dicono `person`, `cyclist`, `vehicle` ed `emergency`.

> Il motore di situation awareness funziona **anche con il modello di default** (YoloX/COCO): il ciclista lo ricava da persona + bici. Quindi le fasi 3–6 si possono fare **in parallelo** all'addestramento.

---

## 3. Il cervello: situation awareness

Il prototipo sta **al centro dell'incrocio** e non vede i semafori. Per questo le regole principali **non dipendono dal semaforo**: le regole sul rosso esistono, ma sono spente finché il nodo non conosce lo stato del semaforo.

### 3.1 I comportamenti pericolosi riconosciuti

| Evento | Quando scatta (soglie in `config.json` → `thresholds`) | Chi avvisare | Inoltro all'incrocio dopo | Norma (indicativa) |
|---|---|---|---|---|
| `speeding` | > 50 km/h (camera calibrata) oppure > 2,5× la velocità tipica (non calibrata, indicativo) | tutti | sì | CdS art. 141–142 |
| `sudden_acceleration` | +15 km/h in 3 s | tutti | sì | art. 141 |
| `harsh_braking` | −20 km/h in 2,5 s (indizio di un quasi-incidente) | tutti | no | art. 141 |
| `solid_line_crossing` | il veicolo attraversa la linea continua | tutti | sì | art. 146 |
| `wrong_way` | contromano nella corsia per più di 1 s | tutti | sì | art. 143 |
| `bike_lane_invasion` | veicolo nella ciclabile per più di 0,6 s; dalla 2ª volta "ripetuta" | ciclisti | sì | art. 146 |
| `bike_lane_hotspot` | 3 invasioni in 10 minuti: incrocio a rischio | ciclisti | sì | art. 146 |
| `illegal_stop` | veicolo fermo per più di 5 s sulle strisce o sulla ciclabile | pedoni, ciclisti | no | art. 158 |
| `pedestrian_not_yielded` | il veicolo passa sulle strisce mentre c'è un pedone sopra | pedoni | no | art. 191 |
| `near_miss` | veicolo in movimento a meno di 2 m da un pedone o ciclista | pedoni, ciclisti | sì | art. 141, 191 |
| `pedestrian_outside_crosswalk` | pedone in carreggiata fuori dalle strisce per più di 1,5 s | veicoli | no | art. 190 |
| `emergency_vehicle` | arriva un mezzo di emergenza (che è esente dalle altre regole) | tutti | sì | art. 177 |
| `red_light_violation` / `late_yellow` | **solo** se il semaforo è noto (`traffic_light.mode`) | pedoni, ciclisti | sì / no | art. 146 |
| `transit` (flusso, non pericolo) | ogni oggetto che passa: classe, via d'ingresso, via d'uscita, velocità massima | statistiche, mappe di calore | — | — |

I riferimenti al Codice della Strada servono a spiegare la regola, non a multare. Il sistema non legge le targhe e non salva immagini.

### 3.2 Come fa a capire cosa succede
1. **Punto a terra.** Per ogni oggetto prende il centro del lato inferiore della box, cioè dove tocca la strada. Non usa il centro della box, che in prospettiva "galleggia".
2. **Calibrazione.** Con 4 punti a terra di misure note (ad esempio il rettangolo delle strisce), i pixel diventano **metri**. Da qui ricava velocità in km/h e distanze vere. Senza calibrazione funziona lo stesso, ma la velocità è solo relativa ("2 volte più veloce del normale").
3. **Tracker.** Collega le detection di un frame a quelle del frame dopo, prevedendo dove sarà l'oggetto. Così ogni oggetto ha un ID, una traiettoria e una velocità, anche con 2–3 FPS.
4. **Regole.** Usano la traiettoria e le zone che disegnate voi: ciclabile, strisce, carreggiata, linea continua, corsie con il loro verso.
5. **Eventi.** Sono JSON nel formato C‑ITS (causeCode ETSI). Contengono a chi interessano (`targets`) e se vanno inoltrati agli incroci vicini (`propagate`).

### 3.3 Esempio di evento
```json
{
  "type": "hazard", "kind": "bike_lane_invasion", "id": "incrocio-1-000007",
  "node": "incrocio-1", "camera": "cam0", "ts": 1791648000123, "expires": 1791648030123,
  "cause": 99, "cause_txt": "dangerousSituation", "severity": 3, "norma": "CdS art. 146",
  "targets": ["cyclist"], "propagate": true,
  "actor": {"track": "cam0-17", "class": "vehicle", "from": "via", "speed_kmh": 34.2, "heading_deg": 180},
  "where": {"zone": "bike_lane"}, "details": {"count": 2, "repeated": true}
}
```

---

## 4. Verificare che funzioni (sul PC, prima della board)

### 4.1 Test automatico di tutte le regole
```bash
cd C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\tools
```
```bash
C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe simulate.py
```
Genera 15 scenari finti: un'auto normale, un'auto a 80 km/h, contromano, linea continua, doppia invasione della ciclabile, pedone sulle strisce, mancato investimento, auto ferma sulle strisce, ambulanza e altri. Per ognuno controlla che escano **esattamente** gli eventi attesi: nessuno in meno e **nessun falso allarme**.

✅ **Controllo:** l'ultima riga dice `TUTTO OK`. Oggi passa: rilanciatelo ogni volta che cambiate una soglia o una regola.

### 4.2 Prova su un video vero
1. Prendete un video di strada (quello di Torino della fase 1.3, oppure uno girato apposta).
2. Salvate il primo frame come foto:
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe replay.py C:\percorso\video.mp4 --snapshot foto.png
   ```
3. Disegnate le zone su quella foto (vedi 4.3) **usando una copia della configurazione**, così non toccate quella della board:
   ```bash
   copy ..\python\config.json config_video.json
   ```
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe zone_editor.py foto.png config_video.json
   ```
4. Fate girare il nodo sul video:
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe replay.py C:\percorso\video.mp4 --config config_video.json
   ```
   Si apre una finestra con:
   - le zone disegnate;
   - ogni oggetto con ID, colore per classe, traiettoria e km/h;
   - **in rosso gli eventi** quando scattano.

   Tasti: `spazio` pausa, `q` esci. Con `--fps 3` simulate la velocità della board, che è il default.
5. Per avere il video annotato da mostrare nella demo, aggiungete `--save risultato.mp4`.

✅ **Controllo:** guardate il video e verificate a occhio. Quando un'auto entra nella ciclabile deve comparire `bike_lane_invasion`; quando non succede niente di pericoloso, non deve comparire niente. Se una regola scatta troppo o troppo poco, cambiate la soglia in `thresholds` nel config e riprovate.

### 4.3 Come si usa `zone_editor.py`
La finestra vi chiede un elemento alla volta. **Click sinistro** aggiunge un punto, **INVIO** conferma, **u** annulla l'ultimo punto, **s** salta l'elemento, **ESC** salva ed esce.

| Elemento | Cosa cliccare |
|---|---|
| calibrazione | 4 punti a terra di un rettangolo che conoscete, nell'ordine **lontano‑sx, lontano‑dx, vicino‑dx, vicino‑sx**; poi in PowerShell scrivete larghezza e lunghezza in metri |
| carreggiata (`roadway`) | il contorno della strada dove passano le auto, esclusa la ciclabile |
| ciclabile (`bike_lane`) | il contorno della pista ciclabile |
| strisce (`crosswalk`) | il contorno delle strisce pedonali |
| linea continua | 2 o più punti lungo la linea |
| corsia verso l'incrocio | il contorno della corsia, poi 2 punti per la **direzione di marcia** (da dove arrivano → dove vanno) |
| corsia che si allontana | stessa cosa per l'altra corsia |
| `via` / `incrocio` | una fascia in fondo alla strada e una vicino alla camera: servono a capire da dove entra e dove esce ogni oggetto |

Per la calibrazione misurate con un metro un rettangolo a terra: ad esempio il rettangolo delle strisce, oppure 2 strisce della carreggiata. Più è grande, più le velocità sono precise.

---

## 5. Il prototipo fisico

### 5.1 Il dispositivo
Seguite la vostra ispirazione: un palo con sfere modulari, stampate in 3D.

| Modulo | Cosa contiene |
|---|---|
| sfera 1 (display) | i LED (striscia WS2813 o matrice) per avvisare: blu = pedoni, magenta = ciclisti, arancione lampeggiante = emergenza, rosso = pericolo |
| sfera 2 (cupola camera) | la camera IMX219, con il cavo flat che scende nel palo |
| sfera 3 (sensore) | il Modulino Distance, per il futuro |
| base | UNO Q + Media Carrier + alimentatore USB‑C |

Per la camera:
- **In alto e inclinata verso il basso**, 30–45°: la rete riconosce meglio gli oggetti visti in diagonale che dall'alto in verticale.
- **Persone e auto devono apparire grandi** nell'inquadratura. Il modello è debole sugli oggetti piccoli (mAP "small" ≈ 0), quindi meglio una strada inquadrata da vicino che un incrocio intero da lontano.

### 5.2 La scena per la demo (3 modi, si possono combinare)

| Modo | Come | Pro | Contro |
|---|---|---|---|
| **A. Schermo** (consigliato per le auto) | Un monitor o tablet davanti alla camera fa vedere un video vero di Torino; disegnate le zone sull'inquadratura dello schermo | auto vere, comportamenti ripetibili, il modello le riconosce | riflessi: abbassate la luminosità ed evitate le luci sopra lo schermo |
| **B. Pavimento in scala reale** (consigliato per le persone) | Con il nastro disegnate a terra una strada di 2–3 m: strisce, ciclabile, linea continua. La camera sta su un treppiede o palo a ~2 m. Voi fate pedone, pedone fuori dalle strisce, ciclista con una bici | dal vivo, molto d'impatto per la giuria | le auto vere non ci sono |
| **C. Plastico** | Cartone con strada, auto giocattolo spinte a mano | si vede il "sistema completo" | il modello va addestrato **anche** sulle auto giocattolo: 100–200 foto dalla camera, etichettate su Edge Impulse |

**Consiglio:** usate A per le regole sulle auto (velocità, contromano, ciclabile, linea continua) e B per quelle sulle persone (strisce, pedone fuori dalle strisce, mancato investimento). Per questi test l'ID del veicolo in scena non conta: conta il comportamento.

### 5.3 Quali immagini servono

| A cosa serve | Che immagini | Quante |
|---|---|---|
| Addestrare il modello | video e foto di strade reali, da un punto alto e in diagonale | 5–10 min di video, più le foto che avete |
| Disegnare le zone | **una** foto con **esattamente** l'inquadratura della camera | 1 per ogni posizione della camera |
| Calibrare | la stessa foto, più le misure in metri di un rettangolo a terra | 1 |
| Testare le regole | video in cui succede (e non succede) ogni comportamento | 1–2 per regola |
| Demo C (plastico) | foto delle auto giocattolo prese dalla camera | 100–200 |

---

## 6. Il nodo sulla UNO Q

### 6.1 Copiare il software
In PowerShell sul PC, dopo aver controllato che `ssh efesto` funzioni. Se la board ha cambiato IP, sulla board lanciate `hostname -I` e aggiornate `HostName` in `C:\Users\Gabriele\.ssh\config`.

Controllate il nome della cartella dell'app:
```bash
ssh efesto "ls ~/ArduinoApps"
```
Fate una copia di sicurezza del `main.py` originale:
```bash
ssh efesto "cp ~/ArduinoApps/efesto/python/main.py ~/ArduinoApps/efesto/python/main_originale.py"
```
Copiate i file del nodo:
```bash
scp C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\*.py C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\config.json efesto:~/ArduinoApps/efesto/python/
```

### 6.2 Primo avvio
1. In App Lab premete **Run** e poi **Open console**.
2. Ogni 5 s compare `[stato] tracce=… coordinate max viste=(x, y) spazio=…`. Passate davanti alla camera e guardate quei numeri:
   - circa 640 × 480: va bene;
   - più grandi: mettete la risoluzione vera in `"frame_size"` nel config;
   - sotto 1: va bene anche così.

✅ **Controllo:** nessun `errore situation awareness` in console. Quando qualcuno passa, compaiono righe di transito.

### 6.3 Disegnare le zone della camera vera
1. Aprite la pagina dell'app (`http://<IP-della-board>:7000`).
2. Fate uno screenshot con **Win+Shift+S** ritagliando **solo il riquadro del video**, bordi esatti. Salvatelo come `node\tools\foto.png`.
3. Disegnate le zone (fase 4.3), questa volta sul config della board:
   ```bash
   C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe zone_editor.py foto.png
   ```
4. Ricopiate il config sulla board:
   ```bash
   scp C:\Users\Gabriele\Desktop\Hackaton\HackTurin_2026\src\node\python\config.json efesto:~/ArduinoApps/efesto/python/
   ```
5. In App Lab: **Stop**, poi **Run**.

⚠️ Se spostate la camera, anche di poco, le zone non corrispondono più: rifate lo screenshot e le zone.

### 6.4 Prova dal vivo
Per ogni regola fate succedere la scena (con lo schermo o dal vivo) e guardate la console.

✅ **Controllo:** compaiono righe `[PERICOLO] <evento> …` solo quando deve. Tutti gli eventi finiscono anche nel file `events.jsonl`:
```bash
ssh efesto "tail -n 20 ~/ArduinoApps/efesto/python/events.jsonl"
```

---

## 7. Collegamento al server (parte vostra)

1. Nel config mettete l'URL del vostro server, alla voce `"publish": {"url": "http://…/eventi", "token": "…"}`.
2. Ogni evento arriva in **POST** come JSON, con lo schema della fase 3.3.
3. Senza rete gli eventi restano in coda in `outbox.jsonl` e partono quando la rete torna.

Cosa deve fare il server per l'avviso al ciclista:
1. Riceve un `hazard` con `propagate: true`, ad esempio `bike_lane_invasion`.
2. Guarda da dove arriva il veicolo (`actor.from`) e dove sta andando (`heading_deg`, l'ultimo `transit`), e decide **quale incrocio viene dopo**.
3. Inoltra l'evento a quell'incrocio e ai dispositivi (wearable o app) degli utenti la cui categoria è in `targets`, ad esempio `cyclist`.
4. Il dispositivo vibra o mostra l'avviso finché non scade (`expires`).

---

## 8. Dopo la demo: 4 camere, visione a 360°

Il tracker lavora già **in metri**, nel sistema di riferimento della strada. Con 4 camere al centro dell'incrocio:
1. ogni camera ha la **sua** calibrazione, che porta i pixel nello **stesso** piano in metri, con l'origine al centro dell'incrocio;
2. le detection di tutte le camere entrano in **un solo tracker**;
3. un'auto che entra dalla via nord, inquadrata dalla camera 1, ed esce dalla via est, inquadrata dalla camera 2, resta **la stessa traccia**. Il `transit` dice `nord → est`, cioè la svolta a destra.

Nel config questo diventerà una lista `cameras: [{id, calibration}, …]`, con le zone in metri invece che in pixel.

---

## 9. Checklist finale

| Fase | Controllo |
|---|---|
| Dataset | `Box per classe` con 4 classi e nessuna `ATTENZIONE` |
| Modello | sulla pagina web compaiono le 4 classi |
| `simulate.py` | `TUTTO OK` |
| `replay.py` su un video vero | gli eventi giusti al momento giusto |
| Zone sulla camera vera | disegnate e copiate sulla board |
| Prova dal vivo | eventi in console e in `events.jsonl` |
| Video demo di 2 minuti | registrato con `replay.py --save` e con la demo dal vivo |
