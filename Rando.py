"""
Randonaut-style random point generator with OSM-based safety filtering,
accurate ellipsoidal math, and real routing-distance validation.

Location detection uses a 3-tier fallback:
1. Hardware GPS (via plyer)
2. IP-based geolocation (via ipapi.co)
3. Manual coordinate entry
"""

import random
import math
import time
import requests
from geopy.distance import geodesic

# Safely import plyer. If the user doesn't have it installed, we can still fall back.
try:
    from plyer import gps
    # We explicitly check if the platform is supported to avoid the ModuleNotFoundError
    if hasattr(gps, 'configure'):
        PLYER_AVAILABLE = True
    else:
        PLYER_AVAILABLE = False
except (ImportError, AttributeError):
    PLYER_AVAILABLE = False

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_429_BACKOFF_S = 5  
OSRM_PROFILE_PATHS = {
    "drive": "driving",
    "bike": "cycling",
    "walk": "walking",
}
OSRM_BASE_URL = "https://router.project-osrm.org/route/v1"
HEADERS = {"User-Agent": "personal-randonaut-script/0.3 (aidenns316@gmail.com)"}

# Safety Tags
DISALLOWED_LANDUSE = {
    "residential", "industrial", "military", "farmland", "farmyard",
    "orchard", "greenhouse_horticulture", "construction", "quarry",
}
DISALLOWED_CLASSES = {"building"}  
ALLOWED_IF_BUILDING_TYPE = {
    "public", "civic", "commercial", "retail", "train_station", "supermarket",
}
SENSITIVE_AMENITIES = {
    "school", "kindergarten", "college", "university",
    "hospital", "clinic", "doctors", "dentist", "nursing_home",
    "prison", "police", "fire_station", "military",
    "childcare", "social_facility",
}
SENSITIVE_BUILDING_TYPES = {
    "school", "kindergarten", "hospital", "university", "college",
    "government", "military", "prison",
}

# --- 3-TIER LOCATION DETECTION ---

# Global variable to store plyer's asynchronous callback result
_gps_location = None

def _on_location(**kwargs):
    global _gps_location
    _gps_location = (kwargs.get('lat'), kwargs.get('lon'))

def get_hardware_gps(timeout_s=5):
    """Tier 1: Try to get hardware GPS using plyer."""
    if not PLYER_AVAILABLE:
        print("  [i] Plyer library not installed. Skipping hardware GPS.")
        return None

    global _gps_location
    _gps_location = None

    try:
        gps.configure(on_location=_on_location)
        gps.start()
        start_time = time.time()
        
        # Wait for the asynchronous callback
        while time.time() - start_time < timeout_s:
            if _gps_location and _gps_location[0] is not None:
                gps.stop()
                return _gps_location
            time.sleep(0.5)
            
        gps.stop()
        print("  [!] Hardware GPS timed out.")
        return None
    except NotImplementedError:
        print("  [i] Hardware GPS not implemented/supported on this OS.")
        return None
    except Exception as e:
        print(f"  [!] Hardware GPS error: {e}")
        try:
            gps.stop()
        except:
            pass
        return None

def get_ip_location():
    """Tier 2: IP-based geolocation."""
    try:
        resp = requests.get("https://ipapi.co/json/", headers=HEADERS, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        lat = data.get("latitude")
        lon = data.get("longitude")
        if lat and lon:
            print(f"  [✓] IP Location Detected: {data.get('city')}, {data.get('region')}")
            return float(lat), float(lon)
    except Exception as e:
        print(f"  [!] IP Location failed: {e}")
    return None

def get_manual_location():
    """Tier 3: Manual input fallback."""
    print("=== Manual Location Setup ===")
    while True:
        try:
            lat = float(input("Enter starting Latitude: ").strip())
            lon = float(input("Enter starting Longitude: ").strip())
            return lat, lon
        except ValueError:
            print("[!] Invalid input. Please enter numbers.")

def get_current_location():
    """Master location function executing the 3-tier fallback."""
    print("=== Detecting Starting Location ===")
    
    print("Tier 1: Attempting Hardware GPS...")
    loc = get_hardware_gps()
    if loc:
        print(f"  [✓] Hardware GPS Acquired: {loc[0]:.6f}, {loc[1]:.6f}")
        return loc
        
    print("\nTier 2: Attempting IP-Based Geolocation...")
    loc = get_ip_location()
    if loc:
        return loc
        
    print("\nTier 3: Falling back to Manual Entry.")
    return get_manual_location()

# --- RANDONAUT LOGIC ---

def random_point_in_radius(lat, lon, radius_m):
    """Uniformly random point within radius_m meters using precise Earth math."""
    r = radius_m * math.sqrt(random.random())
    bearing = random.uniform(0, 360)
    origin = (lat, lon)
    destination = geodesic(meters=r).destination(origin, bearing)
    return destination.latitude, destination.longitude

def reverse_geocode(lat, lon):
    params = {"lat": lat, "lon": lon, "format": "json", "zoom": 18, "addressdetails": 1}
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"  [!] Nominatim request failed: {e}")
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
    last_error = None
    for mirror_idx, mirror_url in enumerate(OVERPASS_MIRRORS):
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(mirror_url, data={"data": query}, headers=HEADERS, timeout=20)
                if resp.status_code == 429:
                    wait = OVERPASS_429_BACKOFF_S * attempt
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json().get("elements", [])
            except requests.RequestException as e:
                last_error = e
                time.sleep(1.0)
    return None

def is_safe_overpass(elements):
    if elements is None: return False, "no data returned"
    if not elements: return True, "no nearby building/landuse features"

    for el in elements:
        tags = el.get("tags", {})
        building, landuse, amenity, leisure = tags.get("building"), tags.get("landuse"), tags.get("amenity"), tags.get("leisure")

        if amenity in SENSITIVE_AMENITIES: return False, f"sensitive amenity ({amenity})"
        if building in SENSITIVE_BUILDING_TYPES: return False, f"sensitive building ({building})"
        if amenity or leisure: continue
        if landuse in DISALLOWED_LANDUSE: return False, f"disallowed landuse ({landuse})"

        if building and building not in ALLOWED_IF_BUILDING_TYPE and building != "yes_public":
            if building in ("house", "residential", "garage", "shed", "apartments", "detached", "terrace", "semidetached_house") or building == "yes":
                return False, f"building footprint ({building})"

    return True, "passed overpass polygon check"

def is_safe_point(geo_data):
    if geo_data is None: return False, "no data returned"

    osm_class, osm_type, addresstype = geo_data.get("class", ""), geo_data.get("type", ""), geo_data.get("addresstype", "")

    if osm_type in SENSITIVE_AMENITIES or osm_type in SENSITIVE_BUILDING_TYPES: return False, f"sensitive location ({osm_type})"
    if osm_class in DISALLOWED_CLASSES and osm_type not in ALLOWED_IF_BUILDING_TYPE: return False, f"building footprint ({osm_type})"
    if osm_class == "landuse" and (osm_type in DISALLOWED_LANDUSE or addresstype in DISALLOWED_LANDUSE): return False, f"disallowed landuse ({osm_type or addresstype})"

    residential_markers = {"house", "residential", "apartments", "yes"}
    if osm_class != "highway" and (addresstype in residential_markers or osm_type in residential_markers or (osm_class == "building" and osm_type in residential_markers)):
        return False, f"residential address ({osm_type}, {addresstype})"

    return True, f"{osm_type or addresstype or osm_class or 'unknown'}"

def get_route_info(origin_lat, origin_lon, dest_lat, dest_lon, mode):
    profile = OSRM_PROFILE_PATHS.get(mode, "walking")
    url = f"{OSRM_BASE_URL}/{profile}/{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    params = {"overview": "false", "alternatives": "false", "steps": "false"}

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
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
        return False, f"OSRM snapped to a distant road ({route_info['distance_m']:.0f}m). Exceeds radius buffer."

    if max_distance_m is not None and route_info["distance_m"] > max_distance_m:
        return False, f"route distance {route_info['distance_m']:.0f}m exceeds max {max_distance_m:.0f}m"

    if max_duration_s is not None and route_info["duration_s"] > max_duration_s:
        return False, f"route time {route_info['duration_s']/60:.1f}min exceeds max"

    return True, f"{route_info['distance_m']:.0f}m, {route_info['duration_s']/60:.1f}min"

def find_safety_passed_point(origin_lat, origin_lon, radius_m=800, max_attempts=15, delay=1.5):
    for attempt in range(1, max_attempts + 1):
        lat, lon = random_point_in_radius(origin_lat, origin_lon, radius_m)
        geo = reverse_geocode(lat, lon)
        nom_safe, nom_reason = is_safe_point(geo)
        ov_safe, ov_reason = is_safe_overpass(overpass_query(lat, lon))

        safe = nom_safe and ov_safe
        display_name = geo.get("display_name", "unknown") if geo else "unknown"

        print(f"  [stage 1: {attempt}/{max_attempts}] -> {'OK' if safe else 'REJECTED'}")
        print(f"             nominatim: {'pass' if nom_safe else 'FAIL'} ({nom_reason})")
        print(f"             overpass:  {'pass' if ov_safe else 'FAIL'} ({ov_reason})")

        if safe:
            return {
                "lat": lat, "lon": lon,
                "nominatim_reason": nom_reason, "overpass_reason": ov_reason,
                "display_name": display_name, "stage1_attempts": attempt,
            }
        time.sleep(delay)  
    return None  

def prompt_user_settings():
    print("\n=== Randonaut Constraints ===")
    mode_choice = input("Mode (1: Walk, 2: Bike, 3: Drive): ").strip()
    mode = {"1": "walk", "2": "bike", "3": "drive"}.get(mode_choice, "walk")

    default_radius = {"walk": 1000, "bike": 4000, "drive": 15000}[mode]
    rad_in = input(f"Search radius in meters (default {default_radius}): ").strip()
    radius_m = float(rad_in) if rad_in else default_radius

    md_in = input("Max travel distance in meters (blank = no limit): ").strip()
    max_distance_m = float(md_in) if md_in else None

    print()
    return {"mode": mode, "radius_m": radius_m, "max_distance_m": max_distance_m, "max_duration_s": None}

def find_random_destination(origin_lat, origin_lon, settings, max_stage1_attempts=15, max_total_rerolls=8, delay=1.5):
    for reroll in range(1, max_total_rerolls + 1):
        print(f"\n--- Search round {reroll}/{max_total_rerolls} ---")
        candidate = find_safety_passed_point(origin_lat, origin_lon, radius_m=settings["radius_m"], max_attempts=max_stage1_attempts, delay=delay)

        if not candidate: continue

        route_info = get_route_info(origin_lat, origin_lon, candidate["lat"], candidate["lon"], settings["mode"])
        travel_ok, travel_reason = passes_travel_limit(route_info, search_radius_m=settings["radius_m"], max_distance_m=settings["max_distance_m"])

        print(f"  [stage 2: routing check] -> {'OK' if travel_ok else 'REJECTED'} ({travel_reason})")

        if travel_ok:
            candidate.update({"mode": settings["mode"], "route_distance_m": route_info["distance_m"], "route_duration_s": route_info["duration_s"], "reroll_round": reroll})
            return candidate
        time.sleep(delay)
    return None

if __name__ == "__main__":
    origin_lat, origin_lon = get_current_location()
    settings = prompt_user_settings()
    
    result = find_random_destination(origin_lat, origin_lon, settings)

    print("\n=== FINAL RESULT ===")
    if result:
        print(f"Go to: {result['lat']:.6f}, {result['lon']:.6f}")
        print(f"What's there: {result['display_name']}")
        print(f"Mode: {result['mode']} | Distance: {result['route_distance_m']:.0f}m")
        print(f"Map: https://www.openstreetmap.org/?mlat={result['lat']}&mlon={result['lon']}#map=19/{result['lat']}/{result['lon']}")
    else:
        print("Couldn't find a point matching your constraints.")