"""
Randonaut-style random point generator with OSM-based safety filtering,
accurate ellipsoidal math, and real routing-distance validation.

Location detection uses a 3-tier fallback:
1. Hardware GPS (via plyer)
2. IP-based geolocation (via ipapi.co)
3. Manual coordinate entry

Enhancements over v0.3:
- Colored terminal output (colorama)
- POI enrichment: shows interesting nearby features at the destination
- Bearing + cardinal direction from origin to destination
- Result logging to randonaut_log.json
- Config file (randonaut_config.json) for saved preferences
- Google Street View link in result
- "Press Enter to exit" so the console stays open when run as .exe
"""

import random
import math
import time
import json
import os
import requests
from datetime import datetime
from geopy.distance import geodesic

# ── Colorama (graceful fallback if not installed) ──────────────────────────
try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    C_OK      = Fore.GREEN
    C_FAIL    = Fore.RED
    C_WARN    = Fore.YELLOW
    C_INFO    = Fore.CYAN
    C_BOLD    = Style.BRIGHT
    C_RESET   = Style.RESET_ALL
except ImportError:
    C_OK = C_FAIL = C_WARN = C_INFO = C_BOLD = C_RESET = ""

# ── Plyer (graceful fallback if not installed) ─────────────────────────────
try:
    from plyer import gps
    PLYER_AVAILABLE = hasattr(gps, "configure")
except (ImportError, AttributeError):
    PLYER_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────
NOMINATIM_URL    = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_429_BACKOFF_S = 5
OSRM_PROFILE_PATHS = {"drive": "driving", "bike": "cycling", "walk": "walking"}
OSRM_BASE_URL    = "https://router.project-osrm.org/route/v1"
HEADERS          = {"User-Agent": "personal-randonaut-script/0.4 (Local testing)"}
CONFIG_FILE      = "randonaut_config.json"
LOG_FILE         = "randonaut_log.json"

# ── Safety tag sets ────────────────────────────────────────────────────────
DISALLOWED_LANDUSE = {
    "residential", "industrial", "military", "farmland", "farmyard",
    "orchard", "greenhouse_horticulture", "construction", "quarry",
}
DISALLOWED_CLASSES       = {"building"}
ALLOWED_IF_BUILDING_TYPE = {"public", "civic", "commercial", "retail", "train_station", "supermarket"}
SENSITIVE_AMENITIES      = {
    "school", "kindergarten", "college", "university",
    "hospital", "clinic", "doctors", "dentist", "nursing_home",
    "prison", "police", "fire_station", "military",
    "childcare", "social_facility",
}
SENSITIVE_BUILDING_TYPES = {
    "school", "kindergarten", "hospital", "university", "college",
    "government", "military", "prison",
}

# Tags we consider interesting for POI enrichment (what's near the destination)
INTERESTING_POI_TAGS = [
    '"amenity"~"cafe|restaurant|bar|pub|food_court|fast_food|ice_cream"',
    '"leisure"~"park|playground|nature_reserve|garden|pitch|trail"',
    '"natural"~"peak|viewpoint|wood|water|beach|cliff"',
    '"tourism"~"viewpoint|attraction|artwork|museum|gallery"',
    '"highway"="trailhead"',
    '"amenity"="library"',
    '"shop"~"convenience|supermarket|bakery"',
]

# ── Helper: colored print shortcuts ───────────────────────────────────────
def p_ok(msg):   print(f"{C_OK}  [✓]{C_RESET} {msg}")
def p_fail(msg): print(f"{C_FAIL}  [✗]{C_RESET} {msg}")
def p_warn(msg): print(f"{C_WARN}  [!]{C_RESET} {msg}")
def p_info(msg): print(f"{C_INFO}  [i]{C_RESET} {msg}")
def p_head(msg): print(f"\n{C_BOLD}{msg}{C_RESET}")

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG FILE
# ═══════════════════════════════════════════════════════════════════════════

def load_config():
    """Load saved preferences from config file if it exists."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(settings, origin_lat, origin_lon):
    """Save current settings so next run can offer them as defaults."""
    cfg = {
        "last_origin_lat":  origin_lat,
        "last_origin_lon":  origin_lon,
        "last_mode":        settings["mode"],
        "last_radius_m":    settings["radius_m"],
        "last_max_distance_m": settings["max_distance_m"],
    }
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        p_warn(f"Couldn't save config: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# RESULT LOGGING
# ═══════════════════════════════════════════════════════════════════════════

def log_result(origin_lat, origin_lon, result, settings):
    """Append result to the local JSON log file."""
    entry = {
        "timestamp":       datetime.now().isoformat(),
        "origin":          {"lat": origin_lat, "lon": origin_lon},
        "destination":     {"lat": result["lat"], "lon": result["lon"]},
        "display_name":    result["display_name"],
        "mode":            result["mode"],
        "route_distance_m": result["route_distance_m"],
        "route_duration_s": result["route_duration_s"],
        "settings":        settings,
        "nearby_pois":     result.get("nearby_pois", []),
        "osm_map":         f"https://www.openstreetmap.org/?mlat={result['lat']}&mlon={result['lon']}#map=19/{result['lat']}/{result['lon']}",
        "street_view":     f"https://www.google.com/maps?q=&layer=c&cbll={result['lat']},{result['lon']}",
    }

    log = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                log = json.load(f)
        except Exception:
            pass

    log.append(entry)
    try:
        with open(LOG_FILE, "w") as f:
            json.dump(log, f, indent=2)
        p_info(f"Result logged to {LOG_FILE} ({len(log)} total entries)")
    except Exception as e:
        p_warn(f"Couldn't write log: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# 3-TIER LOCATION DETECTION
# ═══════════════════════════════════════════════════════════════════════════

_gps_location = None

def _on_location(**kwargs):
    global _gps_location
    _gps_location = (kwargs.get("lat"), kwargs.get("lon"))

def get_hardware_gps(timeout_s=5):
    if not PLYER_AVAILABLE:
        p_info("Plyer not installed — skipping hardware GPS.")
        return None
    global _gps_location
    _gps_location = None
    try:
        gps.configure(on_location=_on_location)
        gps.start()
        start = time.time()
        while time.time() - start < timeout_s:
            if _gps_location and _gps_location[0] is not None:
                gps.stop()
                return _gps_location
            time.sleep(0.5)
        gps.stop()
        p_warn("Hardware GPS timed out.")
        return None
    except NotImplementedError:
        p_info("Hardware GPS not supported on this OS.")
        return None
    except Exception as e:
        p_warn(f"Hardware GPS error: {e}")
        try: gps.stop()
        except: pass
        return None

def get_ip_location():
    try:
        resp = requests.get("https://ipapi.co/json/", headers=HEADERS, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        lat, lon = data.get("latitude"), data.get("longitude")
        if lat and lon:
            p_ok(f"IP Location: {data.get('city')}, {data.get('region')} ({lat:.4f}, {lon:.4f})")
            return float(lat), float(lon)
    except Exception as e:
        p_warn(f"IP Location failed: {e}")
    return None

def get_manual_location():
    p_head("=== Manual Location Entry ===")
    while True:
        try:
            lat = float(input("  Enter Latitude:  ").strip())
            lon = float(input("  Enter Longitude: ").strip())
            return lat, lon
        except ValueError:
            p_warn("Invalid input — enter decimal numbers (e.g. 38.7893).")

def get_current_location(cfg):
    p_head("=== Detecting Starting Location ===")

    # Offer to reuse last known origin from config
    if cfg.get("last_origin_lat") and cfg.get("last_origin_lon"):
        last = f"{cfg['last_origin_lat']:.5f}, {cfg['last_origin_lon']:.5f}"
        reuse = input(f"  Reuse last origin ({last})? [Y/n]: ").strip().lower()
        if reuse in ("", "y", "yes"):
            p_ok(f"Using saved origin: {last}")
            return cfg["last_origin_lat"], cfg["last_origin_lon"]

    print("  Tier 1: Attempting Hardware GPS...")
    loc = get_hardware_gps()
    if loc:
        p_ok(f"Hardware GPS: {loc[0]:.6f}, {loc[1]:.6f}")
        return loc

    print("  Tier 2: Attempting IP Geolocation...")
    loc = get_ip_location()
    if loc:
        return loc

    print("  Tier 3: Manual Entry")
    return get_manual_location()

# ═══════════════════════════════════════════════════════════════════════════
# CORE RANDONAUT LOGIC
# ═══════════════════════════════════════════════════════════════════════════

def random_point_in_radius(lat, lon, radius_m):
    """Uniformly random point within radius_m meters using precise Earth math."""
    r       = radius_m * math.sqrt(random.random())
    bearing = random.uniform(0, 360)
    dest    = geodesic(meters=r).destination((lat, lon), bearing)
    return dest.latitude, dest.longitude

def bearing_to_cardinal(degrees):
    """Convert a bearing in degrees to a compass direction string."""
    dirs = ["N","NE","E","SE","S","SW","W","NW"]
    idx  = round(degrees / 45) % 8
    return dirs[idx]

def get_bearing(origin_lat, origin_lon, dest_lat, dest_lon):
    """
    Calculate the initial bearing from origin to destination.
    Returns degrees (0 = north, 90 = east, etc.)
    """
    lat1, lon1 = math.radians(origin_lat), math.radians(origin_lon)
    lat2, lon2 = math.radians(dest_lat),   math.radians(dest_lon)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    return bearing

def reverse_geocode(lat, lon):
    params = {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 1}
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        p_warn(f"Nominatim failed: {e}")
        return None

def overpass_query(lat, lon, search_radius_m=15, max_retries=3):
    query = f"""
    [out:json][timeout:15];
    (
      way(around:{search_radius_m},{lat},{lon})["building"];
      way(around:{search_radius_m},{lat},{lon})["landuse"];
      relation(around:{search_radius_m},{lat},{lon})["landuse"];
      way(around:{search_radius_m},{lat},{lon})["amenity"];
      way(around:{search_radius_m},{lat},{lon})["leisure"];
    );
    out tags;
    """
    for mirror_idx, mirror_url in enumerate(OVERPASS_MIRRORS):
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(mirror_url, data={"data": query}, headers=HEADERS, timeout=20)
                if resp.status_code == 429:
                    wait = OVERPASS_429_BACKOFF_S * attempt
                    p_warn(f"Overpass 429 on mirror {mirror_idx+1}, backing off {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json().get("elements", [])
            except requests.RequestException as e:
                p_warn(f"Overpass error ({mirror_url}): {e}")
                time.sleep(1.0)
    return None

def enrich_pois(lat, lon, radius_m=250):
    """
    Query Overpass for interesting named POIs near the destination.
    Returns a list of human-readable strings like "café: Blue Bottle Coffee (120m away)"
    """
    tag_filters = "\n".join(
        f'  node(around:{radius_m},{lat},{lon})[{t}]["name"];'
        for t in INTERESTING_POI_TAGS
    )
    query = f"""
    [out:json][timeout:15];
    (
{tag_filters}
    );
    out body;
    """
    pois = []
    try:
        resp = requests.post(OVERPASS_MIRRORS[0], data={"data": query}, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return pois
        elements = resp.json().get("elements", [])
        seen = set()
        for el in elements:
            tags  = el.get("tags", {})
            name  = tags.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            # Work out what kind of place it is
            kind = (tags.get("amenity") or tags.get("leisure") or
                    tags.get("natural") or tags.get("tourism") or
                    tags.get("highway") or tags.get("shop") or "place")
            # Approximate distance from destination
            el_lat, el_lon = el.get("lat", lat), el.get("lon", lon)
            dist_m = geodesic((lat, lon), (el_lat, el_lon)).meters
            pois.append({"name": name, "kind": kind, "dist_m": round(dist_m)})
        pois.sort(key=lambda x: x["dist_m"])
        return pois[:8]   # cap at 8 results
    except Exception:
        return pois

def is_safe_overpass(elements):
    if elements is None: return False, "no data returned"
    if not elements:     return True,  "no nearby building/landuse features"

    for el in elements:
        tags     = el.get("tags", {})
        building = tags.get("building")
        landuse  = tags.get("landuse")
        amenity  = tags.get("amenity")
        leisure  = tags.get("leisure")

        if amenity in SENSITIVE_AMENITIES:      return False, f"sensitive amenity ({amenity})"
        if building in SENSITIVE_BUILDING_TYPES: return False, f"sensitive building ({building})"
        if amenity or leisure:                   continue
        if landuse in DISALLOWED_LANDUSE:        return False, f"disallowed landuse ({landuse})"

        if building and building not in ALLOWED_IF_BUILDING_TYPE and building != "yes_public":
            if building in ("house","residential","garage","shed","apartments",
                            "detached","terrace","semidetached_house","yes"):
                return False, f"building footprint ({building})"

    return True, "passed overpass check"

def is_safe_point(geo_data):
    if geo_data is None: return False, "no data returned"

    osm_class   = geo_data.get("class", "")
    osm_type    = geo_data.get("type", "")
    addresstype = geo_data.get("addresstype", "")

    if osm_type in SENSITIVE_AMENITIES or osm_type in SENSITIVE_BUILDING_TYPES:
        return False, f"sensitive location ({osm_type})"
    if osm_class in DISALLOWED_CLASSES and osm_type not in ALLOWED_IF_BUILDING_TYPE:
        return False, f"building footprint ({osm_type})"
    if osm_class == "landuse" and (osm_type in DISALLOWED_LANDUSE or addresstype in DISALLOWED_LANDUSE):
        return False, f"disallowed landuse ({osm_type or addresstype})"

    residential_markers = {"house", "residential", "apartments", "yes"}
    if osm_class != "highway" and (
        addresstype in residential_markers
        or osm_type in residential_markers
        or (osm_class == "building" and osm_type in residential_markers)
    ):
        return False, f"residential address ({osm_type}, {addresstype})"

    return True, f"{osm_type or addresstype or osm_class or 'unknown'}"

def get_route_info(origin_lat, origin_lon, dest_lat, dest_lon, mode):
    profile = OSRM_PROFILE_PATHS.get(mode, "walking")
    url     = f"{OSRM_BASE_URL}/{profile}/{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    try:
        resp = requests.get(url, params={"overview": "false", "alternatives": "false", "steps": "false"},
                             headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"): return None
        route = data["routes"][0]
        return {"distance_m": route["distance"], "duration_s": route["duration"]}
    except (requests.RequestException, KeyError, ValueError):
        return None

def passes_travel_limit(route_info, search_radius_m, max_distance_m=None, max_duration_s=None):
    if route_info is None: return False, "routing failed"

    snap_limit_m = search_radius_m * 1.5
    if route_info["distance_m"] > snap_limit_m:
        return False, f"OSRM snap too far ({route_info['distance_m']:.0f}m > {snap_limit_m:.0f}m buffer)"

    if max_distance_m and route_info["distance_m"] > max_distance_m:
        return False, f"{route_info['distance_m']:.0f}m exceeds {max_distance_m:.0f}m limit"
    if max_duration_s and route_info["duration_s"] > max_duration_s:
        return False, f"{route_info['duration_s']/60:.1f}min exceeds {max_duration_s/60:.0f}min limit"

    return True, f"{route_info['distance_m']:.0f}m, {route_info['duration_s']/60:.1f}min"

# ═══════════════════════════════════════════════════════════════════════════
# SEARCH ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════

def find_safety_passed_point(origin_lat, origin_lon, radius_m=800, max_attempts=15, delay=1.5):
    for attempt in range(1, max_attempts + 1):
        lat, lon     = random_point_in_radius(origin_lat, origin_lon, radius_m)
        geo          = reverse_geocode(lat, lon)
        nom_safe, nom_reason = is_safe_point(geo)
        ov_safe,  ov_reason  = is_safe_overpass(overpass_query(lat, lon))

        safe         = nom_safe and ov_safe
        display_name = geo.get("display_name", "unknown") if geo else "unknown"

        status_str = f"{C_OK}OK{C_RESET}" if safe else f"{C_FAIL}REJECTED{C_RESET}"
        nom_col    = C_OK if nom_safe else C_FAIL
        ov_col     = C_OK if ov_safe  else C_FAIL
        print(f"  {C_INFO}[{attempt}/{max_attempts}]{C_RESET} → {status_str}")
        print(f"           nominatim: {nom_col}{'pass' if nom_safe else 'FAIL'}{C_RESET} ({nom_reason})")
        print(f"           overpass:  {ov_col}{'pass' if ov_safe  else 'FAIL'}{C_RESET} ({ov_reason})")

        if safe:
            return {
                "lat": lat, "lon": lon,
                "nominatim_reason": nom_reason, "overpass_reason": ov_reason,
                "display_name": display_name, "stage1_attempts": attempt,
            }
        time.sleep(delay)
    return None

def prompt_user_settings(cfg):
    p_head("=== Randonaut Constraints ===")

    # Show last-used defaults from config
    last_mode   = cfg.get("last_mode", "walk")
    last_radius = cfg.get("last_radius_m")
    last_maxd   = cfg.get("last_max_distance_m")

    mode_hint = {"walk": "1", "bike": "2", "drive": "3"}.get(last_mode, "1")
    mode_input = input(f"  Mode (1: Walk, 2: Bike, 3: Drive) [last: {last_mode}]: ").strip()
    mode       = {"1": "walk", "2": "bike", "3": "drive"}.get(mode_input or mode_hint, last_mode)

    default_radius = last_radius or {"walk": 1000, "bike": 4000, "drive": 15000}[mode]
    rad_in = input(f"  Search radius in meters (default {default_radius:.0f}): ").strip()
    radius_m = float(rad_in) if rad_in else default_radius

    maxd_hint = f"{last_maxd:.0f}" if last_maxd else "none"
    md_in = input(f"  Max travel distance in meters [last: {maxd_hint}, blank = no limit]: ").strip()
    max_distance_m = float(md_in) if md_in else None

    print()
    return {"mode": mode, "radius_m": radius_m, "max_distance_m": max_distance_m, "max_duration_s": None}

def find_random_destination(origin_lat, origin_lon, settings, max_stage1_attempts=15, max_total_rerolls=8, delay=1.5):
    for reroll in range(1, max_total_rerolls + 1):
        p_head(f"--- Search Round {reroll}/{max_total_rerolls} ---")
        candidate = find_safety_passed_point(
            origin_lat, origin_lon,
            radius_m=settings["radius_m"],
            max_attempts=max_stage1_attempts,
            delay=delay,
        )
        if not candidate:
            continue

        route_info = get_route_info(
            origin_lat, origin_lon,
            candidate["lat"], candidate["lon"],
            settings["mode"],
        )
        travel_ok, travel_reason = passes_travel_limit(
            route_info, search_radius_m=settings["radius_m"],
            max_distance_m=settings["max_distance_m"],
        )

        col = C_OK if travel_ok else C_FAIL
        print(f"  {C_INFO}[stage 2 routing]{C_RESET} → {col}{'OK' if travel_ok else 'REJECTED'}{C_RESET} ({travel_reason})")

        if travel_ok:
            candidate.update({
                "mode":             settings["mode"],
                "route_distance_m": route_info["distance_m"],
                "route_duration_s": route_info["duration_s"],
                "reroll_round":     reroll,
            })
            return candidate
        time.sleep(delay)
    return None

# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cfg = load_config()

    origin_lat, origin_lon = get_current_location(cfg)
    settings = prompt_user_settings(cfg)

    result = find_random_destination(origin_lat, origin_lon, settings)

    p_head("═══════════════════════════════")
    p_head("          FINAL RESULT         ")
    p_head("═══════════════════════════════")

    if result:
        # Bearing / direction
        bearing  = get_bearing(origin_lat, origin_lon, result["lat"], result["lon"])
        cardinal = bearing_to_cardinal(bearing)

        # POI enrichment
        print(f"\n{C_INFO}Scanning for nearby points of interest...{C_RESET}")
        pois = enrich_pois(result["lat"], result["lon"])
        result["nearby_pois"] = pois

        # Distance in both m and miles for readability
        dist_m    = result["route_distance_m"]
        dist_mi   = dist_m * 0.000621371
        dur_min   = result["route_duration_s"] / 60

        print(f"\n  {C_BOLD}Destination:{C_RESET} {result['display_name']}")
        print(f"  {C_BOLD}Coordinates:{C_RESET} {result['lat']:.6f}, {result['lon']:.6f}")
        print(f"  {C_BOLD}Direction:  {C_RESET} {cardinal} ({bearing:.0f}°)")
        print(f"  {C_BOLD}Mode:       {C_RESET} {result['mode'].capitalize()}")
        print(f"  {C_BOLD}Route:      {C_RESET} {dist_m:.0f}m ({dist_mi:.2f}mi) — {dur_min:.1f}min")

        if pois:
            print(f"\n  {C_BOLD}Nearby POIs:{C_RESET}")
            for poi in pois:
                print(f"    {C_OK}•{C_RESET} {poi['name']} ({poi['kind']}, ~{poi['dist_m']}m away)")
        else:
            print(f"\n  {C_WARN}No named POIs found within 250m — sounds remote!{C_RESET}")

        print(f"\n  {C_BOLD}OSM Map:     {C_RESET} https://www.openstreetmap.org/?mlat={result['lat']}&mlon={result['lon']}#map=19/{result['lat']}/{result['lon']}")
        print(f"  {C_BOLD}Street View: {C_RESET} https://www.google.com/maps?q=&layer=c&cbll={result['lat']},{result['lon']}")

        save_config(settings, origin_lat, origin_lon)
        log_result(origin_lat, origin_lon, result, settings)

    else:
        p_fail("Couldn't find a point matching your constraints.")
        p_info("Try: bigger radius, longer max distance, or remove limits.")

    print()
    input("  Press Enter to exit...")
