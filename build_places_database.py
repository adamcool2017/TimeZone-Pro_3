"""Build the browser's place database from the same GeoNames cities1000 data
used by the TimeZone Pro desktop source. Uses only Python's standard library.

Run in this folder with:  py build_places_database.py
It replaces places-data.js with a full static database for GitHub Pages.
"""
from __future__ import annotations

import io
import json
import os
import urllib.request
import zipfile
from pathlib import Path
from zoneinfo import available_timezones

BASE = "https://download.geonames.org/export/dump/"
FILES = {
    "cities": BASE + "cities1000.zip",
    "countries": BASE + "countryInfo.txt",
    "regions": BASE + "admin1CodesASCII.txt",
}
OUTPUT = Path(__file__).with_name("places-data.js")


def download(url: str, label: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "TimeZoneProWeb/1.0"})
    chunks: list[bytes] = []
    size = 0
    print(f"Downloading {label}…")
    with urllib.request.urlopen(request, timeout=60) as response:
        while True:
            block = response.read(1024 * 256)
            if not block:
                break
            chunks.append(block)
            size += len(block)
            print(f"\r  {size / 1048576:.1f} MB", end="", flush=True)
    print()
    return b"".join(chunks)


def main() -> None:
    zones = available_timezones()

    country_text = download(FILES["countries"], "country names").decode("utf-8")
    countries: dict[str, str] = {}
    for line in country_text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) > 4 and len(fields[0]) == 2:
            countries[fields[0]] = fields[4]

    region_text = download(FILES["regions"], "region names").decode("utf-8")
    regions: dict[str, str] = {}
    for line in region_text.splitlines():
        fields = line.split("\t")
        if len(fields) > 1:
            regions[fields[0]] = fields[1]

    archive_data = download(FILES["cities"], "GeoNames cities1000 database")
    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
        member = next(name for name in archive.namelist() if name.endswith(".txt"))
        city_text = archive.read(member).decode("utf-8")

    places: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for line in city_text.splitlines():
        fields = line.split("\t")
        if len(fields) < 18:
            continue
        city = fields[1]
        country_code = fields[8]
        zone = fields[17]
        if zone not in zones:
            continue
        key = (city.casefold(), country_code, zone, fields[10])
        if key in seen:
            continue
        seen.add(key)
        population = int(fields[14]) if fields[14].isdigit() else 0
        places.append({
            "city": city,
            "country": countries.get(country_code, country_code),
            "region": regions.get(f"{country_code}.{fields[10]}", ""),
            "zone": zone,
            "lat": float(fields[4]) if fields[4] else None,
            "lon": float(fields[5]) if fields[5] else None,
            "_population": population,
        })

    # Match the desktop app: put larger populated places first.
    places.sort(key=lambda p: -int(p["_population"]))
    for place in places:
        place.pop("_population", None)
    payload = "window.TIMEZONE_PRO_PLACES = " + json.dumps(
        places, ensure_ascii=False, separators=(",", ":")
    ) + ";\n"
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, OUTPUT)
    print(f"Done! Wrote {len(places):,} searchable places to {OUTPUT.name}.")
    print("Refresh the website. Keep index.html, satellite.html, and places-data.js together.")


if __name__ == "__main__":
    main()
