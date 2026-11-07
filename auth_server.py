import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, jsonify, request

APP_DIR = Path(__file__).resolve().parent
PEPPER_FILE = APP_DIR / "timezone_pro_pepper.key"


def load_local_env():
    env_path = APP_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        print(f"Could not read the .env file: {exc}", flush=True)


load_local_env()

DB_PATH = Path(os.environ.get("TZP_AUTH_DB_PATH", str(APP_DIR / "timezone_pro_accounts.sqlite3")))
SMTP_HOST = os.environ.get("TZP_SMTP_HOST", "smtp.gmail.com").strip()
SMTP_USERNAME = os.environ.get("TZP_SMTP_USERNAME", "").strip()
SMTP_APP_PASSWORD = os.environ.get("TZP_SMTP_APP_PASSWORD", "").replace(" ", "").strip()
ADMIN_EMAIL = os.environ.get("TZP_ADMIN_EMAIL", "").strip() or SMTP_USERNAME
PLACEHOLDER_VALUES = {"your_gmail_address@gmail.com", "your_16_letter_app_password", ""}


def load_pepper():
    configured = os.environ.get("TZP_CODE_PEPPER")
    if configured:
        return configured
    try:
        saved = PEPPER_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    except OSError:
        pass
    generated = secrets.token_urlsafe(32)
    try:
        PEPPER_FILE.write_text(generated, encoding="utf-8")
    except OSError:
        pass
    return generated


OTP_PEPPER = load_pepper()
OTP_LIFETIME_SECONDS = 10 * 60
SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60
MAX_VERIFY_ATTEMPTS = 5
SEND_COOLDOWN_SECONDS = 45
MAX_SAVED_CITIES = 20000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8_000_000
_rate_lock = threading.Lock()
_send_email_last = {}
_send_ip_last = {}


def now_ts():
    return int(time.time())


def utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY, created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS otp_codes (
            email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, requested_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,
            expires_at INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(email) REFERENCES users(email)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS saved_cities (
            email TEXT PRIMARY KEY, cities_json TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(email) REFERENCES users(email)
        )""")
        cleanup_expired(conn)


def cleanup_expired(conn):
    now = now_ts()
    conn.execute("DELETE FROM otp_codes WHERE expires_at < ?", (now,))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(
        re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip())
    )


def code_digest(email, code):
    return hmac.new(OTP_PEPPER.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def mail_is_configured():
    return (
        SMTP_USERNAME not in PLACEHOLDER_VALUES
        and SMTP_APP_PASSWORD not in PLACEHOLDER_VALUES
    )


def send_mail(recipient, subject, body):
    if not mail_is_configured():
        raise RuntimeError(
            "Email is not configured. Open the .env file beside auth_server.py and fill in "
            "TZP_SMTP_USERNAME and TZP_SMTP_APP_PASSWORD."
        )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"TimeZone Pro <{SMTP_USERNAME}>"
    message["To"] = recipient
    message.set_content(body)
    first_error = None
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=20) as smtp:
            smtp.login(SMTP_USERNAME, SMTP_APP_PASSWORD)
            smtp.send_message(message)
        return
    except Exception as exc:
        first_error = exc
    try:
        with smtplib.SMTP(SMTP_HOST, 587, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(SMTP_USERNAME, SMTP_APP_PASSWORD)
            smtp.send_message(message)
        return
    except Exception as second_error:
        raise RuntimeError(f"{type(first_error).__name__}: {first_error}; {type(second_error).__name__}: {second_error}")


def enforce_send_limits(email, ip):
    now = time.monotonic()
    with _rate_lock:
        if now - _send_email_last.get(email, 0.0) < SEND_COOLDOWN_SECONDS:
            return False
        if now - _send_ip_last.get(ip, 0.0) < 5:
            return False
        _send_email_last[email] = now
        _send_ip_last[ip] = now
        if len(_send_email_last) > 5000:
            cutoff = now - SEND_COOLDOWN_SECONDS * 4
            for key, value in list(_send_email_last.items()):
                if value < cutoff:
                    _send_email_last.pop(key, None)
        if len(_send_ip_last) > 5000:
            cutoff = now - 60
            for key, value in list(_send_ip_last.items()):
                if value < cutoff:
                    _send_ip_last.pop(key, None)
        return True


def email_for_token(token):
    if not isinstance(token, str) or not token or len(token) > 256:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT email, expires_at FROM sessions WHERE token_hash = ?",
            (token_digest(token),),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < now_ts():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(token),))
            return None
        return row["email"]


@app.get("/health")
def health():
    return jsonify(ok=True, service="TimeZone Pro accounts", mail_configured=mail_is_configured())


@app.post("/api/request-code")
def request_code():
    payload = request.get_json(silent=True) or {}
    email = payload.get("email", "")
    if not valid_email(email):
        return jsonify(ok=False, error="Enter a valid email address."), 400
    email = email.strip().casefold()
    if not mail_is_configured():
        return jsonify(
            ok=False,
            error="The server's email settings are missing. Fill in the .env file next to auth_server.py.",
        ), 503
    if not enforce_send_limits(email, request.remote_addr or "unknown"):
        return jsonify(ok=False, error="Please wait before requesting another code."), 429
    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        with db() as conn:
            cleanup_expired(conn)
            conn.execute(
                "INSERT OR REPLACE INTO otp_codes(email, code_hash, expires_at, attempts, requested_at) VALUES (?, ?, ?, 0, ?)",
                (email, code_digest(email, code), now_ts() + OTP_LIFETIME_SECONDS, now_ts()),
            )
        send_mail(
            email,
            "Your TimeZone Pro verification code",
            f"Your TimeZone Pro verification code is: {code}\n\nIt expires in 10 minutes and can be used once. If you did not request it, ignore this email.\n",
        )
    except Exception as exc:
        with db() as conn:
            conn.execute("DELETE FROM otp_codes WHERE email = ?", (email,))
        print(f"Could not send verification email: {type(exc).__name__}: {exc}", flush=True)
        return jsonify(ok=False, error="We couldn't send the email. Check the Gmail address and App Password in the .env file."), 503
    return jsonify(ok=True, message="If the email can receive messages, a verification code has been sent.")


@app.post("/api/verify-code")
def verify_code():
    payload = request.get_json(silent=True) or {}
    email = payload.get("email", "")
    code = payload.get("code", "")
    if not valid_email(email) or not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        return jsonify(ok=False, error="Enter a valid email address and 6-digit code."), 400
    email = email.strip().casefold()
    token = secrets.token_urlsafe(32)
    is_new = False
    with db() as conn:
        row = conn.execute("SELECT * FROM otp_codes WHERE email = ?", (email,)).fetchone()
        if row is None:
            return jsonify(ok=False, error="Request a new code first."), 400
        if row["expires_at"] < now_ts():
            conn.execute("DELETE FROM otp_codes WHERE email = ?", (email,))
            return jsonify(ok=False, error="That code has expired. Request a new one."), 400
        if not hmac.compare_digest(row["code_hash"], code_digest(email, code)):
            attempts = row["attempts"] + 1
            if attempts >= MAX_VERIFY_ATTEMPTS:
                conn.execute("DELETE FROM otp_codes WHERE email = ?", (email,))
                return jsonify(ok=False, error="Too many incorrect attempts. Request a new code."), 429
            conn.execute("UPDATE otp_codes SET attempts=? WHERE email=?", (attempts, email))
            return jsonify(ok=False, error=f"That code is not correct. Attempts remaining: {MAX_VERIFY_ATTEMPTS - attempts}."), 400
        conn.execute("DELETE FROM otp_codes WHERE email = ?", (email,))
        existing = conn.execute("SELECT email FROM users WHERE email = ?", (email,)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO users(email, created_at) VALUES (?, ?)", (email, utc_iso()))
            is_new = True
        conn.execute(
            "INSERT OR REPLACE INTO sessions(token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_digest(token), email, now_ts() + SESSION_LIFETIME_SECONDS, utc_iso()),
        )
    if is_new and ADMIN_EMAIL:
        try:
            send_mail(ADMIN_EMAIL, "New TimeZone Pro signup", f"A new account verified its email: {email}\nVerified at UTC: {utc_iso()}\n")
        except Exception as exc:
            print(f"Account created, but signup notification failed: {type(exc).__name__}: {exc}", flush=True)
    return jsonify(ok=True, email=email, token=token, is_new_account=is_new)


@app.post("/api/session/validate")
def validate_session():
    payload = request.get_json(silent=True) or {}
    email = email_for_token(payload.get("token", ""))
    if not email:
        return jsonify(ok=False, error="Your saved sign-in expired. Sign in again."), 401
    return jsonify(ok=True, email=email)


@app.post("/api/logout")
def logout():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token", "")
    if isinstance(token, str) and token and len(token) <= 256:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(token),))
    return jsonify(ok=True)


@app.post("/api/cities/load")
def load_cities():
    payload = request.get_json(silent=True) or {}
    email = email_for_token(payload.get("token", ""))
    if not email:
        return jsonify(ok=False, error="Sign in again to sync your cities."), 401
    with db() as conn:
        row = conn.execute("SELECT cities_json FROM saved_cities WHERE email=?", (email,)).fetchone()
    try:
        cities = json.loads(row["cities_json"]) if row else None
    except ValueError:
        cities = None
    return jsonify(ok=True, cities=cities)


@app.post("/api/cities/save")
def save_cities():
    payload = request.get_json(silent=True) or {}
    email = email_for_token(payload.get("token", ""))
    if not email:
        return jsonify(ok=False, error="Sign in again to sync your cities."), 401
    incoming = payload.get("cities")
    if not isinstance(incoming, list) or len(incoming) > MAX_SAVED_CITIES:
        return jsonify(ok=False, error="Invalid city list."), 400
    safe = []
    for item in incoming:
        if not isinstance(item, dict):
            return jsonify(ok=False, error="Invalid city entry."), 400
        city, zone = item.get("city"), item.get("zone")
        if not isinstance(city, str) or not city.strip() or len(city) > 120:
            return jsonify(ok=False, error="Invalid city name."), 400
        if not isinstance(zone, str) or not zone.strip() or len(zone) > 120:
            return jsonify(ok=False, error="Invalid time-zone name."), 400
        latitude, longitude = item.get("latitude"), item.get("longitude")
        if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
            latitude = None
        if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
            longitude = None
        safe.append({
            "city": city.strip(),
            "country": str(item.get("country", ""))[:120],
            "zone": zone.strip(),
            "region": str(item.get("region", ""))[:120],
            "latitude": latitude,
            "longitude": longitude,
        })
    with db() as conn:
        conn.execute(
            "INSERT INTO saved_cities(email, cities_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET cities_json=excluded.cities_json, updated_at=excluded.updated_at",
            (email, json.dumps(safe, ensure_ascii=False), utc_iso()),
        )
    return jsonify(ok=True, saved=len(safe))


init_db()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("TZP_AUTH_HOST", "127.0.0.1"),
        port=int(os.environ.get("TZP_AUTH_PORT", "5000")),
        debug=False,
    )