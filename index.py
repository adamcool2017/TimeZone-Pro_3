import gzip
import json
import os
import threading
import urllib.request
from pathlib import Path

from flask import Response, jsonify, request, send_file

import places
from auth_server import app

APP_DIR = Path(__file__).resolve().parent
PAGE = APP_DIR / "index.html"
SATELLITE_PAGE = APP_DIR / "satellite.html"
LOGO = APP_DIR / "logo.svg"
GEO_URL = "https://raw.githubusercontent.com/datasets/geo-countries/master/data/countries.geojson"
GEO_CACHE = APP_DIR / "timezone_pro_countries_web.json.gz"
GEO_LOCK = threading.Lock()
HOSTED = "PORT" in os.environ

if HOSTED:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def build_countries():
    request_object = urllib.request.Request(GEO_URL, headers={"User-Agent": "TimeZonePro/5.0"})
    with urllib.request.urlopen(request_object, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    out = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        name = props.get("ADMIN") or props.get("name") or props.get("NAME")
        geometry = feature.get("geometry") or {}
        polygons = geometry.get("coordinates", [])
        if geometry.get("type") == "Polygon":
            polygons = [polygons]
        elif geometry.get("type") != "MultiPolygon":
            continue
        rings = [polygon[0] for polygon in polygons if polygon and polygon[0]]
        if not name or not rings:
            continue
        biggest = max(rings, key=len)
        centre = [
            sum(point[0] for point in biggest) / len(biggest),
            sum(point[1] for point in biggest) / len(biggest),
        ]
        shapes = []
        for ring in rings:
            size = len(ring)
            step = 1 if size < 120 else 2 if size < 600 else 3 if size < 2000 else 5
            points = [[round(p[0], 2), round(p[1], 2)] for p in ring[::step]]
            if len(points) >= 3:
                shapes.append(points)
        out.append({"n": name, "p": shapes, "c": [round(centre[0], 2), round(centre[1], 2)], "d": len(biggest)})
    return out


def countries_blob():
    with GEO_LOCK:
        if GEO_CACHE.exists():
            try:
                return GEO_CACHE.read_bytes()
            except OSError:
                pass
        raw = json.dumps(build_countries(), separators=(",", ":")).encode("utf-8")
        blob = gzip.compress(raw, 9)
        temporary = GEO_CACHE.with_suffix(".tmp")
        temporary.write_bytes(blob)
        os.replace(temporary, GEO_CACHE)
        return blob


@app.get("/")
def home():
    return send_file(PAGE, mimetype="text/html", max_age=0)


@app.get("/satellite")
def satellite():
    return send_file(SATELLITE_PAGE, mimetype="text/html", max_age=0)


@app.get("/logo.svg")
@app.get("/favicon.ico")
def logo():
    return send_file(LOGO, mimetype="image/svg+xml", max_age=86400)


@app.get("/api/search")
def search_places():
    return jsonify(ok=True, results=places.search(request.args.get("q", "")[:80]))


@app.get("/api/info")
def place_info():
    return jsonify(ok=True, **places.info())


@app.get("/api/countries")
def countries():
    try:
        blob = countries_blob()
    except Exception as exc:
        print(f"Map data failed: {exc}", flush=True)
        return jsonify(ok=False, error="Map data is unavailable right now."), 503
    return Response(
        blob,
        mimetype="application/json",
        headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=86400"},
    )


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def warm_map_data():
    try:
        countries_blob()
    except Exception as exc:
        print(f"Map data will be retried on first request: {exc}", flush=True)


places.load()
threading.Thread(target=warm_map_data, daemon=True).start()


if __name__ == "__main__":
    host = os.environ.get("TZP_AUTH_HOST", "0.0.0.0" if HOSTED else "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("TZP_AUTH_PORT", "5000")))
    try:
        from waitress import serve
    except ImportError:
        app.run(host=host, port=port, debug=False)
    else:
        serve(app, host=host, port=port, threads=8)