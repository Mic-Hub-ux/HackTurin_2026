# HackTurin_2026

# Smart Crossing: AI-Powered Intelligent Pedestrian Crossing

An intelligent pedestrian crossing system developed for **HackTurin**. This project combines computer vision, category-specific dynamic light signaling, peer-to-peer (P2P) road node communication and a centralized  server architecture for urban mobility heatmaps.

--- 

# Challenge Reference:

**Smart Crossing: an intersection that understands what is happening.**

The device doesn't just detect presence; it interprets the scene in real time, distinguishes approaching user categories, triggers targeted signals, coordinates locally with adjacent nodes, and synchronizes data with a central server for traffic analytics without relying on a constant internet connection.

--- 

# Key Features 

* **Local AI & Computer Vision:** Running machine learning models directly on the edge device for real-time classification (pedestrians, cyclists, animals, and emergency vehicles).
* **Dynamic Light Signaling:** An integrated RGB LED system paired with the traffic light that changes color based on the detected user category to maximize visibility and safety:
  * **Pedestrians:** Blue Light
  * **Cyclists:** Magenta Light
  * **Ambulances / Emergency:** Orange Flashing Light
* **Traffic Intensity & Counting:** Counting passing vehicles to estimate traffic intensity in specific areas for statistical and planning purposes.
* **Offline Operation (Edge):** Full operational capability ensured even without internet connectivity.
* **P2P Inter-Node Communication:** Local mesh/P2P network allowing adjacent smart crossings to share immediate traffic states, hazards, or approaching emergency vehicles to synchronize green waves or warnings.
* **Central Server & Mobility Heatmaps:** Periodic or event-driven synchronization with a backend server to aggregate anonymized flow data, feeding a "soft" urban traffic heatmap dashboard.

--- 


## Bill of Materials (BOM)

The hardware prototype consists of the following components:

| Component | Quantity / Notes |
| :--- | :--- |
| **Arduino Uno Q 4GB / Hacker Kit** | Main processing unit |
| **8MP Sony IMX219 Camera Module** | Video acquisition for computer vision |
| **Arduino Media Carrier** | Media streaming and peripheral management |
| **Movement Sensor** | Preliminary detection of approaching bodies |
| **Distance Sensor** | Measuring user proximity |
| **RGB LED / LED** | Dynamic light signaling |
| **Breadboard & Jumper Wires** | Prototyping circuit wiring |


---

# System Architecture & Network

1. **Sensory Detection:** The motion sensor and camera capture visual and physical flows near the crossing.
2. **Edge Processing:** Local AI processes frames via the vision module to classify users (e.g., cyclist vs. pedestrian).
3. **Local Actuation:** The microcontroller instantly activates the corresponding custom LED color and updates traffic intensity metrics.
4. **P2P Node Layer:** Nodes talk directly to neighboring intersections (e.g., warning downstream nodes if an ambulance is approaching).
5. **Central Server & Analytics:** When connectivity is available, nodes push aggregated statistical payloads to the backend server to render real-time urban heatmaps.

--- 

# Repository Structure

```
src/
  node/        edge software for the Arduino UNO Q (App Lab app: python/ + sketch/), PC tools, SPEC.md
  server/      central server: server.py (standard library, works offline) + flask_app.py (PythonAnywhere),
               map dashboard (/), cyclist view (/cyclist), phone alerts (/app), offline console (/console)
  firmware/    ESP32 radio gateway (ESP-NOW), ESP32 vibrating wristband, UNO R4 WiFi as a second intersection
  client/      load test: 12 simulated intersections posting to /api/traffic
  dataset/     dataset builder for Edge Impulse (Open Images + Roboflow + own videos)
docs/          step-by-step guides (Italian): PIANO.md, TUTORIAL.md, GUIDA_DEMO.md
```

# Quick Start

Secrets live in a `.env` file at the repository root (never committed): copy `.env.example` to `.env` and set
`SC_TOKEN`. The server, `client.py` and the node read it automatically. Every device write (node events,
`POST /api/traffic`, *Simulate hazard*) needs `Authorization: Bearer <SC_TOKEN>`; pages and dashboard reads stay public.

```bash
cd src/server
python server.py --port 8000
```
Dashboard: `http://localhost:8000` · cyclist view: `/cyclist` · phone alerts: `/app?node=incrocio-1` · offline console: `/console`.

Simulated grid of 12 intersections on the local server:
```bash
SC_TRAFFIC_URL=http://localhost:8000/api/traffic python src/client/client.py
```

**PythonAnywhere** only needs the server files. Use a sparse checkout so the clone contains just these paths:
`src/server/`, `src/node/python/events.py` (event catalogue) and `src/node/python/web/app.html` (phone page).
Add a `.env` with `SC_TOKEN` next to them. The WSGI file can keep `from server import app as application`.
The step-by-step guide is in [docs/GUIDA_DEMO.md](docs/GUIDA_DEMO.md).

Full node pipeline on a video, without the board (YOLO on the PC, same rules as the UNO Q):
```bash
python src/node/tools/replay.py video.mp4 --server http://127.0.0.1:8000
```

Automated tests (no hardware needed): `python src/node/tools/test_suite.py`.
Deploying on the UNO Q, the LEDs, the ESP32 wristbands and the UNO R4: see [docs/GUIDA_DEMO.md](docs/GUIDA_DEMO.md).

# What a Node Understands

The node tracks every object over time (ground point, homography to meters, tracker robust to 2-5 FPS) and raises
C-ITS-style hazard events (ETSI cause codes), each with a lifecycle (new / cancel), the users it concerns and whether
it must be forwarded to the intersection the vehicle is heading to:

| Event | Example |
|---|---|
| speeding, sudden acceleration, harsh braking | km/h with calibration, relative speed without |
| solid line crossing, wrong way, U-turn, forbidden turn | lanes and lines drawn on the camera view |
| bike lane invasion (repeated, hotspot), close pass on a cyclist (< 1.5 m) | cyclist safety |
| pedestrian not yielded, near miss, predicted collision risk | vulnerable road users |
| pedestrian outside crosswalk, slow pedestrian, crowd, pedestrian waiting | crossing behaviour |
| illegal stop on crosswalk / bike lane, congestion | lifecycle new -> cancel |
| emergency vehicle | orange LED, forwarded along its route |

Every closed track produces a summary (entry/exit street, speed with V85, time per zone, crosswalk crossing with
direction, trajectory); statistics and a heatmap are aggregated every minute. Hazards travel to the server over HTTP
(store-and-forward when offline). The server forwards them along the vehicle's direction (2 intersections, 3 for
emergency vehicles and fast vehicles), to the neighbours when the direction is unknown, and along the shortest path
when a destination is chosen.

**Cyclist wristband (demo).** The nearest intersection talks *directly* to an ESP32 wristband
(`src/firmware/esp32_bracciale`, UDP port 48100). There is no server and no internet, only a shared local Wi-Fi (even a
phone hotspot). The wristband LED blinks while a car is entering or stopped in the bike lane and switches off as soon
as the camera no longer sees it. The same message can travel with no Wi-Fi network at all: an ESP32 "radio" wired to the
intersection's MCU broadcasts it over ESP-NOW (or the board's Bluetooth LE advertises it), so wristbands near the
intersection are warned even where there is no network.

**Offline peer-to-peer between intersections (designed, not in the demo).** Intersections can also exchange hazards
directly without the server over the SC-P2P protocol (`src/node/python/peer.py`: UDP multicast on the LAN, or
ESP-NOW with no network at all), so a district keeps warning its users even when the backend is unreachable.

--- 

## Contributors

* [Belli Gabriele](https://github.com/sGabellee)
* [Carbone Gaetano](https://github.com/gwetano)
* [Carbone Modestino](https://github.com/modestinocarbone)
* [Cetraro Michele](https://github.com/Mic-Hub-ux)