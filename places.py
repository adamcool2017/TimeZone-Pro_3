import difflib
import gzip
import io
import json
import os
import time
import unicodedata
import urllib.request
import zipfile
from pathlib import Path
from zoneinfo import available_timezones

CACHE_FILE = Path(__file__).resolve().with_name("timezone_pro_places.json.gz")
CACHE_VERSION = 6
BASE = "https://download.geonames.org/export/dump/"
CITY_URLS = (BASE + "cities500.zip", BASE + "cities1000.zip")
COUNTRIES_URL = BASE + "countryInfo.txt"
REGIONS_URL = BASE + "admin1CodesASCII.txt"

FALLBACK = [
    ["Manama", "Bahrain", "Asia/Bahrain", "", 0, "", 26.2154, 50.5832],
    ["Muharraq", "Bahrain", "Asia/Bahrain", "", 0, "", 26.2572, 50.6119],
    ["London", "United Kingdom", "Europe/London", "England", 0, "", 51.5072, -0.1276],
    ["Edinburgh", "United Kingdom", "Europe/London", "Scotland", 0, "", 55.9533, -3.1883],
    ["New York", "United States", "America/New_York", "New York", 0, "", 40.7128, -74.006],
    ["Tokyo", "Japan", "Asia/Tokyo", "Tokyo", 0, "", 35.6895, 139.6917],
    ["Dubai", "United Arab Emirates", "Asia/Dubai", "Dubai", 0, "", 25.2048, 55.2708],
    ["Paris", "France", "Europe/Paris", "", 0, "", 48.8566, 2.3522],
    ["Sydney", "Australia", "Australia/Sydney", "New South Wales", 0, "", -33.8688, 151.2093],
]

STOP_WORDS = {
    "a", "an", "the", "of", "in", "at", "for", "to", "me", "it", "is", "s",
    "what", "whats", "time", "now", "current", "local", "clock", "show",
}
SHORTCUTS = {"us", "uk", "uae", "la", "sf", "kl", "hk", "ksa", "usa", "nyc", "drc"}
ALIAS_GROUPS = [
    ("usa", "us", "united states", "united states of america", "america"),
    ("uk", "united kingdom", "britain", "great britain"),
    ("uae", "united arab emirates", "emirates"),
    ("ksa", "saudi arabia"),
    ("south korea", "korea", "republic of korea"),
    ("czechia", "czech republic"),
    ("netherlands", "holland"),
    ("myanmar", "burma"),
    ("eswatini", "swaziland"),
    ("north macedonia", "macedonia"),
    ("turkiye", "turkey"),
    ("cote d ivoire", "ivory coast"),
    ("timor leste", "east timor"),
    ("cabo verde", "cape verde"),
    ("democratic republic of the congo", "dr congo", "drc"),
    ("vatican city", "vatican", "holy see"),
    ("new york city", "new york", "nyc"),
    ("los angeles", "la"),
    ("san francisco", "sf"),
    ("kuala lumpur", "kl"),
    ("hong kong", "hk"),
    ("mumbai", "bombay"),
    ("kolkata", "calcutta"),
    ("chennai", "madras"),
    ("bengaluru", "bangalore"),
    ("yangon", "rangoon"),
    ("kyiv", "kiev"),
    ("beijing", "peking"),
    ("ho chi minh city", "saigon"),
    ("istanbul", "constantinople"),
    ("cologne", "koln"),
    ("saint petersburg", "st petersburg", "leningrad"),
    ("gothenburg", "goteborg"),
    ("munich", "munchen"),
    ("florence", "firenze"),
    ("rome", "roma"),
    ("naples", "napoli"),
    ("venice", "venezia"),
    ("turin", "torino"),
    ("vienna", "wien"),
    ("prague", "praha"),
    ("warsaw", "warszawa"),
    ("lisbon", "lisboa"),
    ("moscow", "moskva"),
    ("athens", "athina"),
    ("copenhagen", "kobenhavn"),
    ("brussels", "bruxelles"),
    ("seville", "sevilla"),
    ("belgrade", "beograd"),
    ("bucharest", "bucuresti"),
    ("geneva", "geneve"),
    ("the hague", "den haag"),
    ("kathmandu", "katmandu"),
    ("mecca", "makkah"),
    ("medina", "madinah"),
    ("sanaa", "sana a"),
    ("tel aviv", "tel aviv yafo"),
    ("xian", "xi an"),
]
ALIAS_MAP = {}
for _group in ALIAS_GROUPS:
    for _member in _group:
        ALIAS_MAP[_member] = [other for other in _group if other != _member]

LEADING = (
    "what is the current time in ", "what is the time in ", "what time is it in ",
    "current local time in ", "current time in ", "local time in ",
    "show me cities in ", "show cities in ", "find cities in ", "list cities in ",
    "cities in ", "city in ", "places in ", "show me ", "look up ", "search for ",
    "find ", "search ", "show ", "time in ", "clock in ", "the time in ",
)

ROWS = []
NAMES = []
HAY = []
ZONES = []
COUNTRY_MAP = {}
NAME_INDEX = {}
BUCKETS = {}


def normalize(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    for ch in "_-/.,'’":
        text = text.replace(ch, " ")
    return " ".join(text.split())


def download(url):
    last = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "TimeZonePro/5.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last


def build_rows():
    available = available_timezones()
    countries = {}
    for line in download(COUNTRIES_URL).decode("utf-8").splitlines():
        fields = line.split("\t")
        if not line.startswith("#") and len(fields) > 4 and len(fields[0]) == 2:
            countries[fields[0]] = fields[4]
    regions = {}
    for line in download(REGIONS_URL).decode("utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) > 1:
            regions[fields[0]] = fields[1]
    archive = None
    for url in CITY_URLS:
        try:
            archive = download(url)
            break
        except Exception:
            continue
    if archive is None:
        raise OSError("Could not download the city database")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        member = next(name for name in bundle.namelist() if name.endswith(".txt"))
        text = bundle.read(member).decode("utf-8")
    rows = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 18 or fields[17] not in available:
            continue
        code = fields[8]
        try:
            latitude, longitude = float(fields[4]), float(fields[5])
        except ValueError:
            latitude = longitude = None
        rows.append([
            fields[1],
            countries.get(code, code),
            fields[17],
            regions.get(f"{code}.{fields[10]}", ""),
            int(fields[14]) if fields[14].isdigit() else 0,
            "",
            latitude,
            longitude,
        ])
    rows.sort(key=lambda row: -row[4])
    return rows


def read_cache():
    try:
        with gzip.open(CACHE_FILE, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("version") == CACHE_VERSION and data.get("rows"):
            return data["rows"]
    except (OSError, ValueError, EOFError):
        pass
    return None


def write_cache(rows):
    temporary = CACHE_FILE.with_suffix(".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump({"version": CACHE_VERSION, "rows": rows}, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, CACHE_FILE)


def load():
    rows = read_cache()
    if not rows:
        try:
            rows = build_rows()
            write_cache(rows)
        except Exception as exc:
            print(f"City database download failed, using the small fallback list: {exc}", flush=True)
            rows = list(FALLBACK)
    seen = {(normalize(r[0]), normalize(r[1])) for r in rows}
    for row in FALLBACK:
        if (normalize(row[0]), normalize(row[1])) not in seen:
            rows.append(row)
    names, hay, zones = [], [], []
    country_map, name_index, zone_cache, norm_cache = {}, {}, {}, {}

    def cached(text):
        value = norm_cache.get(text)
        if value is None:
            value = norm_cache[text] = normalize(text)
        return value

    for i, row in enumerate(rows):
        name = normalize(row[0])
        country = cached(row[1])
        names.append(name)
        hay.append(f" {name} {cached(row[3])} {country}")
        zone = zone_cache.get(row[2])
        if zone is None:
            zone = zone_cache[row[2]] = normalize(row[2])
        zones.append(zone)
        if country:
            country_map.setdefault(country, []).append(i)
        name_index.setdefault(name, []).append(i)
    ROWS[:] = rows
    NAMES[:] = names
    HAY[:] = hay
    ZONES[:] = zones
    COUNTRY_MAP.clear()
    COUNTRY_MAP.update(country_map)
    NAME_INDEX.clear()
    NAME_INDEX.update(name_index)
    BUCKETS.clear()
    print(f"Loaded {len(ROWS):,} places", flush=True)


def info():
    return {"places": len(ROWS), "countries": len(COUNTRY_MAP)}


def put(pool, index, level):
    if level < pool.get(index, 99):
        pool[index] = level


def clean_query(raw):
    text = normalize(raw)
    for phrase in LEADING:
        if text.startswith(phrase):
            text = text[len(phrase):].strip()
            break
    return " ".join(token for token in text.split() if token not in {"cities", "city", "places"})


def variants(tokens):
    base = [t for t in tokens if t not in STOP_WORDS] or tokens
    phrase = " ".join(base)
    out = [(base, 3 if phrase in SHORTCUTS else 0)]
    seen = {tuple(base)}
    alts = [alt.split() for alt in ALIAS_MAP.get(phrase, [])]
    for position, token in enumerate(base):
        for alt in ALIAS_MAP.get(token, []):
            alts.append(base[:position] + alt.split() + base[position + 1:])
    for tokens_alt in alts:
        if tuple(tokens_alt) not in seen and len(out) < 8:
            seen.add(tuple(tokens_alt))
            out.append((tokens_alt, 0))
    return out


def fuzzy(pool, token):
    if len(token) < 3:
        return
    if not BUCKETS:
        for name in NAME_INDEX:
            if name:
                BUCKETS.setdefault(name[0], []).append(name)
    candidates = [n for n in BUCKETS.get(token[0], []) if abs(len(n) - len(token)) <= 2]
    for name in difflib.get_close_matches(token, candidates, n=6, cutoff=0.74):
        for i in NAME_INDEX[name][:3]:
            put(pool, i, 6)
    countries = [c for c in COUNTRY_MAP if abs(len(c) - len(token)) <= 2]
    for country in difflib.get_close_matches(token, countries, n=3, cutoff=0.78):
        for i in COUNTRY_MAP[country][:200]:
            put(pool, i, 7)


def as_place(i):
    row = ROWS[i]
    return {
        "city": row[0], "country": row[1], "zone": row[2],
        "region": row[3], "latitude": row[6], "longitude": row[7],
    }


def search(query, limit=30):
    if "/" in query:
        text = normalize(query)
        return [as_place(i) for i in range(len(ZONES)) if text in ZONES[i]][:limit]
    text = clean_query(query)
    if not text:
        return []
    options = variants(text.split())
    pool = {}
    for tokens, penalty in options:
        joined = " ".join(tokens)
        for i in COUNTRY_MAP.get(joined, ())[:300]:
            put(pool, i, -1)
        count = 0
        needles = [" " + token for token in tokens]
        for i, hay in enumerate(HAY):
            if all(needle in hay for needle in needles):
                name = NAMES[i]
                level = 0 if name == joined else 1 if name.startswith(tokens[0]) else 2
                put(pool, i, level + penalty)
                count += 1
                if count >= 800:
                    break
    base = options[0][0]
    if not pool and len(base) > 1:
        best, found = 0, []
        needles = [" " + token for token in base]
        for i, hay in enumerate(HAY):
            hits = sum(needle in hay for needle in needles)
            if hits > best:
                best, found = hits, [i]
            elif hits == best and hits > 0 and len(found) < 300:
                found.append(i)
        for i in found:
            put(pool, i, 5 + len(base) - best)
    if not pool:
        fuzzy(pool, max(base, key=len))
    ranked = sorted(pool.items(), key=lambda item: (item[1], item[0]))
    results, seen = [], set()
    for i, _ in ranked:
        place = as_place(i)
        key = (place["city"], place["country"], place["zone"], place["region"])
        if key not in seen:
            seen.add(key)
            results.append(place)
        if len(results) >= limit:
            break
    return results