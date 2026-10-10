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
    **Cyclists:** Magenta Light
    **Ambulances / Emergency:** Orange Flashing Light
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

## Contributors

* [Belli Gabriele](https://github.com/sGabellee)
* [Carbone Gaetano](https://github.com/gwetano)
* [Carbone Modestino](https://github.com/modestinocarbone)
* [Cetraro Michele](https://github.com/Mic-Hub-ux)