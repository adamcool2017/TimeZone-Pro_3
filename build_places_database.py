"""Build TimeZone Pro's static browser databases for GitHub Pages.

Run in this folder with:
    py build_places_database.py

Creates data/places.json and data/countries.json.
Uses only Python's standard library.
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
URLS = {
    "cities": BASE + "cities1000.zip",
    "countries": BASE + "countryInfo.txt",
    "regions": BASE + "admin1CodesASCII.txt",
    "outlines": "https://raw.githubusercontent.com/datasets/geo-countries/master/data/countries.geojson",
}
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def download(url: str, label: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "TimeZoneProWeb/2.0"})
    chunks, size = [], 0
    print(f"Downloading {label}...")
    with urllib.request.urlopen(request, timeout=90) as response:
        while True:
            block = response.read(512 * 1024)
            if not block:
                break
            chunks.append(block)
            size += len(block)
            print(f"\r  {size / 1048576:.1f} MB", end="", flush=True)
    print()
    return b"".join(chunks)


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def parse_country_names(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) > 4 and len(fields[0]) == 2:
            result[fields[0]] = fields[4]
    return result


def parse_regions(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) > 1:
            result[fields[0]] = fields[1]
    return result


def build_places(zip_data: bytes, countries: dict[str, str],
                 regions: dict[str, str], valid_zones: set[str]) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
        member = next(name for name in archive.namelist() if name.endswith(".txt"))
        rows = []
        seen = set()
        with archive.open(member) as stream:
            for raw in stream:
                fields = raw.decode("utf-8").rstrip("\r\n").split("\t")
                if len(fields) < 18:
                    continue
                city, code, zone, admin = fields[1].strip(), fields[8], fields[17], fields[10]
                if not city or zone not in valid_zones:
                    continue
                key = (city.casefold(), code, zone, admin)
                if key in seen:
                    continue
                seen.add(key)
                population = int(fields[14]) if fields[14].isdigit() else 0
                try:
                    lat = float(fields[4]) if fields[4] else 999.0
                except ValueError:
                    lat = 999.0
                try:
                    lon = float(fields[5]) if fields[5] else 999.0
                except ValueError:
                    lon = 999.0
                rows.append((population, city, countries.get(code, code),
                             regions.get(f"{code}.{admin}", ""), zone, lat, lon))

    rows.sort(key=lambda item: -item[0])
    country_list = sorted({row[2] for row in rows})
    region_list = sorted({row[3] for row in rows} | {""})
    zone_list = sorted({row[4] for row in rows})
    ci = {value: i for i, value in enumerate(country_list)}
    ri = {value: i for i, value in enumerate(region_list)}
    zi = {value: i for i, value in enumerate(zone_list)}

    return {
        "n": [row[1] for row in rows],
        "c": [ci[row[2]] for row in rows],
        "r": [ri[row[3]] for row in rows],
        "z": [zi[row[4]] for row in rows],
        "la": [row[5] for row in rows],
        "lo": [row[6] for row in rows],
        "countries": country_list,
        "regions": region_list,
        "zones": zone_list,
    }


def ring_stats(ring: list) -> tuple[float, float, float]:
    points = [(float(p[0]), float(p[1])) for p in ring if len(p) >= 2]
    if len(points) < 3:
        return 0.0, 0.0, 0.0
    area2 = 0.0
    previous = points[-1]
    for point in points:
        area2 += previous[0] * point[1] - point[0] * previous[1]
        previous = point
    area = abs(area2) / 2
    return area, sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def build_outlines(raw: bytes) -> list[dict]:
    source = json.loads(raw.decode("utf-8"))
    result = []
    for feature in source.get("features", []):
        props = feature.get("properties") or {}
        name = props.get("ADMIN") or props.get("name") or props.get("NAME") or props.get("formal_en")
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        kind = geometry.get("type")
        if not name or not coords:
            continue
        polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
        rings, total, wx, wy = [], 0.0, 0.0, 0.0
        for polygon in polygons:
            if not polygon:
                continue
            ring = [[float(p[0]), float(p[1])] for p in polygon[0] if len(p) >= 2]
            if len(ring) < 3:
                continue
            area, cx, cy = ring_stats(ring)
            rings.append(ring)
            total += area
            wx += cx * area
            wy += cy * area
        if rings:
            all_points = [point for ring in rings for point in ring]
            center = ([wx / total, wy / total] if total else [
                sum(p[0] for p in all_points) / len(all_points),
                sum(p[1] for p in all_points) / len(all_points),
            ])
            result.append({"n": str(name), "d": total, "c": center, "p": rings})
    if not result:
        raise RuntimeError("No usable country outlines were found in the downloaded GeoJSON.")
    return result


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    zones = available_timezones()

    country_names = parse_country_names(download(URLS["countries"], "country names").decode("utf-8"))
    region_names = parse_regions(download(URLS["regions"], "region names").decode("utf-8"))

    places = build_places(download(URLS["cities"], "GeoNames cities1000"), country_names, region_names, zones)
    save_json(DATA / "places.json", places)
    print(f"Created data/places.json: {len(places['n']):,} places.")

    outlines = build_outlines(download(URLS["outlines"], "world country outlines"))
    save_json(DATA / "countries.json", outlines)
    print(f"Created data/countries.json: {len(outlines):,} outlines.")

    print("\nSUCCESS. Upload the entire data folder to GitHub.")
    print("Leave index.html, search.js, and satellite.html unchanged.")


if __name__ == "__main__":
    main()
