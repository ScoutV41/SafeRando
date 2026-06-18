# SafeRando: A Safer Randonautica Clone

SafeRando is a lightweight, terminal-based application inspired by Randonautica that generates random geographical coordinates within a user-defined radius for exploration. Unlike traditional implementations, SafeRando prioritizes explorer safety and practicality by running candidate points through multi-stage OpenStreetMap (OSM) filters and validating real-world travel constraints before guiding you to a location.

No "quantum" BS just clean Python math and public street data to help you safely discover hidden spots in your neighborhood.

---

##  Features

* **3-Tier Location Detection:** Automatically resolves your starting point with smart fallbacks:
    1.  **Hardware GPS:** Attempts to ping physical device hardware (via `plyer`).
    2.  **IP Geolocation:** If GPS fails or isn't supported, it tracks your approximate location via your public IP address.
    3.  **Manual Entry:** Prompts manual entry if all network/device options fail.
* **Stage 1 Safety Filtering (OSM + Nominatim):** Drops points that land directly inside private property, active construction zones, industrial sites, military installations, quarries, or farmlands.
* **Sensitive Area Whitelist Protection:** Automatically blocks and rerolls any coordinate that lands near schools, daycares, universities, hospitals, clinics, police stations, or prisons.
* **Stage 2 Route Distance Validation (OSRM):** Calculates the *actual* walking, biking, or driving route to your destination instead of a straight line. If a point is across a river or a barrier and requires an absurdly long detour, the script automatically catches it and re-rolls.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
Ensure you have Python 3.8 or higher installed on your computer.

### 2. Install Required Libraries
Clone this repository or download `Rando.py`, then install the mandatory Python dependencies via terminal/command prompt:

```bash
pip install requests geopy plyer colorama
```

##  How to Use

Simply execute the script from your terminal:

```bash
python Rando.py

```

### Flow of Operation:

1. **Location Detection:** The script will attempt to grab your current coordinates.
2. **Mode Selection:** Choose your mode of transport (`1` for Walk, `2` for Bike, `3` for Drive).
3. **Set Your Constraints:** Input your desired search radius in meters (or hit enter to use a sensible default). You can also provide an optional maximum travel distance limit.
4. **Generation:** Watch the terminal filter out unsafe coordinate options in real time.
5. **Result:** If a valid coordinate matches your guidelines, the script prints out an absolute address, the physical route distance/time, and an OpenStreetMap navigation link.

---


##  Future Architecture Notice

This script is built using core logic designed to cleanly shift to a web-based front end. When porting this project to a web application (e.g., via Flask or FastAPI), Tier 1 and Tier 2 location detection will be replaced entirely on the front end by the native HTML5 Browser Geolocation API (`navigator.geolocation`) for pinpoint accuracy.

---

##  Credits and Disclaimer

* **Routing Data:** Powered by the open-source [Project OSRM](https://project-osrm.org/).
* **Map Data:** Provided by [OpenStreetMap](https://www.google.com/search?q=https://www.openstreetmap.org/) contributors via Nominatim and Overpass APIs.
* **Disclaimer:** This script is built for legal, safe recreational exploration. Always respect local trespassing laws, stay aware of your surroundings, and do not explore private properties or hazardous terrain. Use at your own risk. I do not take responsibility for any injury or harm done to you and/or your party.


