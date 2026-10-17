"""
Geocoder module for the iPhone Photo Manager.
Offline reverse geocoding via reverse_geocoder, with per-batch coordinate
deduplication; coordinates inside China fall back to BigDataCloud so the
location comes back in Chinese.
"""

import logging
import reverse_geocoder as rg
import pycountry
from typing import Optional

logger = logging.getLogger(__name__)

# reverse_geocoder ships localized proper names: for a few places its
# "name" field carries a German spelling ("Zuerich") instead of the
# English exonym ("Zurich"). Normalize at the source - here, where the
# string is born - so the database, search filters, translation cache
# and every display layer agree on one spelling without per-screen
# remap hacks. Conservative by design: only confirmed offenders.
EXONYM_MAP = {
    "Zuerich": "Zurich",
}


def normalize_location_text(text: str) -> str:
    """Replace every known localized spelling with its English exonym.

    Substring-based because callers pass assembled location strings
    ("Zuerich, Switzerland"), not bare city names.
    """
    for src, dst in EXONYM_MAP.items():
        text = text.replace(src, dst)
    return text

# In-memory cache for coordinates: (lat_round, lng_round) -> "Location String"
_geocode_cache = {}


def batch_reverse_geocode(coordinates: list[tuple[float, float]]) -> list[Optional[str]]:
    """Batch geocode a list of (latitude, longitude) tuples instantly using offline reverse_geocoder."""
    if not coordinates:
        return []

    locations = []
    
    # Extract unique coordinates to geocode
    to_geocode = []
    coord_keys = []
    for lat, lng in coordinates:
        if lat is None or lng is None:
            coord_keys.append(None)
            continue
            
        lat_r = round(lat, 2)
        lng_r = round(lng, 2)
        coord_key = (lat_r, lng_r)
        coord_keys.append(coord_key)
        
        if coord_key not in _geocode_cache:
            to_geocode.append(coord_key)
            # Add a placeholder to avoid duplicate queries in the same batch
            _geocode_cache[coord_key] = None

    if to_geocode:
        logger.info(f"Offline geocoding {len(to_geocode)} unique clusters...")
        # reverse_geocoder is incredibly fast and takes a list of tuples
        results = rg.search(to_geocode)
        
        # Concurrently fetch BDC for CN coords to avoid sequential network delays
        cn_coords = [ck for ck, res in zip(to_geocode, results) if res.get('cc') == 'CN']
        bdc_results = {}
        if cn_coords:
            import requests
            session = requests.Session()

            def fetch_bdc(ck):
                try:
                    url = f"https://api.bigdatacloud.net/data/reverse-geocode-client?latitude={ck[0]}&longitude={ck[1]}&localityLanguage=zh"
                    resp = session.get(url, timeout=3)
                    if resp.status_code == 200:
                        data = resp.json()
                        c_name = data.get("countryName", "中国")
                        if c_name == "中华人民共和国":
                            c_name = "中国"
                        p_sub = data.get("principalSubdivision", "")
                        city_name = data.get("city", "")
                        c_parts = []
                        if city_name:
                            c_parts.append(city_name)
                        if p_sub and p_sub != city_name:
                            c_parts.append(p_sub)
                        if c_name:
                            c_parts.append(c_name)
                        if c_parts:
                            return ck, ", ".join(c_parts)
                except Exception:
                    pass
                return ck, None

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=20) as executor:
                for ck, loc in executor.map(fetch_bdc, cn_coords):
                    if loc:
                        bdc_results[ck] = loc
            session.close()

        for coord_key, res in zip(to_geocode, results):
            if coord_key in bdc_results:
                loc_str = bdc_results[coord_key]
                _geocode_cache[coord_key] = loc_str
                continue

            name = normalize_location_text(res.get('name', ''))
            admin1 = normalize_location_text(res.get('admin1', ''))
            admin2 = normalize_location_text(res.get('admin2', ''))
            cc = res.get('cc', '')
            
            # Map country code to full country name
            country = cc
            if cc:
                try:
                    c = pycountry.countries.get(alpha_2=cc)
                    if c:
                        country = c.name
                except Exception:
                    pass
            
            # Build parts
            parts = []
            if name:
                parts.append(name)
            if admin2 and admin2 != name:
                parts.append(admin2)
            if admin1 and admin1 != admin2 and admin1 != name:
                parts.append(admin1)
            if country:
                parts.append(country)
                
            loc_str = ", ".join(parts)
            _geocode_cache[coord_key] = loc_str

    # Map back to original list
    for coord_key in coord_keys:
        if coord_key is None:
            locations.append(None)
        else:
            locations.append(_geocode_cache.get(coord_key))

    return locations


def reverse_geocode(latitude: float, longitude: float) -> Optional[str]:
    """Geocode a single coordinate."""
    return batch_reverse_geocode([(latitude, longitude)])[0]
