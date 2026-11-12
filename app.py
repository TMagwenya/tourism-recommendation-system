import html
import json
import logging
import os
import re
import socket
import sqlite3
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash,jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required ,current_user
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

# Verified available with the existing API key (including tool calling).
# Used only when the primary model keeps failing with a transient error.
GEMINI_FALLBACK_MODEL = os.getenv(
    "GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"
).strip()

GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAYS = (1.0, 2.0, 4.0)
GEMINI_MAX_RETRY_AFTER_SECONDS = 30.0

gemini_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY else None
)






class User(UserMixin):
    def __init__(self, user_id, username, email, password_hash):
        self.id = user_id
        self.username = username
        self.email = email
        self.password_hash = password_hash

app=Flask(__name__)
#Project paths
BASE_DIR=Path(__file__).resolve().parent
DATABASE=BASE_DIR /"tourism.db"
SCHEMA_FILE = BASE_DIR / "database" / "schema.sql"

# Flask session signing key. It is loaded from .env (never hardcoded) and must
# be a stable random value: regenerating it on every start would invalidate
# existing sessions and log every user out.
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError(
        "SECRET_KEY is missing or too short. Add a strong random value to "
        ".env as SECRET_KEY=<value> (at least 32 characters). Generate one "
        "with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

app.config["SECRET_KEY"] = SECRET_KEY

# Cookies are HttpOnly and SameSite by default. SESSION_COOKIE_SECURE stays
# off for local HTTP development; set SESSION_COOKIE_SECURE=true in .env for
# HTTPS deployments so the browser only sends the cookie over TLS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv(
        "SESSION_COOKIE_SECURE", "false"
    ).strip().lower() in {"1", "true", "yes", "on"},
)

# Basic server-side email validation for registration.
EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

DESTINATION_IMAGES_DIR = BASE_DIR / "static" / "images" / "destinations"


def is_valid_email(email):
    """Return True for a plausible single email address."""
    if not email or len(email) > 254 or ".." in email:
        return False

    return EMAIL_PATTERN.fullmatch(email) is not None


@app.template_global()
def destination_image_exists(folder, filename):
    """Return True only when the requested destination image exists on disk."""
    if not folder or not filename or Path(filename).name != filename:
        return False

    try:
        directory = (DESTINATION_IMAGES_DIR / folder).resolve()
        directory.relative_to(DESTINATION_IMAGES_DIR.resolve())
    except (OSError, ValueError):
        return False

    return (directory / filename).is_file()


@app.template_global()
def destination_gallery_images(folder):
    """Return the gallery images that actually exist for a destination."""
    if not folder:
        return []

    try:
        directory = (DESTINATION_IMAGES_DIR / folder).resolve()
        directory.relative_to(DESTINATION_IMAGES_DIR.resolve())
    except (OSError, ValueError):
        return []

    if not directory.is_dir():
        return []

    images = []

    for path in directory.iterdir():
        match = re.fullmatch(r"gallery(\d+)\.jpg", path.name)

        if match and path.is_file():
            images.append((int(match.group(1)), path.name))

    return [name for _, name in sorted(images)]

# ---------------------------------------------------------------------------
# Gemini reliability: dedicated logging, error classification and retries
# ---------------------------------------------------------------------------

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
APPLICATION_LOG_FILE = LOG_DIR / "application.log"

APPLICATION_LOG_FILE_RESOLVED = str(APPLICATION_LOG_FILE.resolve())

_file_handler = RotatingFileHandler(
    APPLICATION_LOG_FILE,
    maxBytes=1_000_000,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s"
))

if not any(
    isinstance(handler, RotatingFileHandler)
    and getattr(handler, "baseFilename", "") == APPLICATION_LOG_FILE_RESOLVED
    for handler in app.logger.handlers
):
    app.logger.addHandler(_file_handler)

app.logger.setLevel(logging.INFO)

AI_HIGH_DEMAND_MESSAGE = (
    "The AI assistant is temporarily experiencing high demand. "
    "Please try again shortly."
)
AI_QUOTA_MESSAGE = (
    "The AI assistant has reached its usage limit for now. "
    "Please wait a few minutes and try again."
)
AI_NETWORK_MESSAGE = (
    "The connection to the AI service could not be established. "
    "Check your internet connection and try again."
)
AI_AUTH_MESSAGE = (
    "The Gemini API key in .env is not valid. Create a new "
    "key at https://aistudio.google.com/apikey and restart "
    "the application."
)
AI_UNEXPECTED_MESSAGE = (
    "Sorry, the tourism assistant is temporarily unavailable. "
    "Please try again."
)


def redact_sensitive_text(text):
    """Remove API keys and key query parameters before logging."""
    if not isinstance(text, str):
        text = str(text)

    if GEMINI_API_KEY:
        text = text.replace(GEMINI_API_KEY, "[REDACTED]")

    return re.sub(
        r"(?i)((?:key|api_key|apikey)=)[^&\s\"']+",
        r"\1[REDACTED]",
        text,
    )


def get_gemini_error_code(error):
    """Return the HTTP status code carried by a Gemini API error."""
    if isinstance(error, genai_errors.APIError):
        return error.code
    return None


def is_gemini_auth_error(error):
    """True for permanent authentication/authorisation failures."""
    if get_gemini_error_code(error) in (401, 403):
        return True

    error_text = str(error)
    return (
        "API key not valid" in error_text
        or "UNAUTHENTICATED" in error_text
        or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in error_text
        or "PERMISSION_DENIED" in error_text
    )


def is_gemini_quota_error(error):
    """True for HTTP 429 / RESOURCE_EXHAUSTED responses."""
    return (
        get_gemini_error_code(error) == 429
        or "RESOURCE_EXHAUSTED" in str(error)
    )


def is_gemini_unavailable(error):
    """True for Gemini 5xx capacity/availability failures."""
    if get_gemini_error_code(error) in (500, 502, 503, 504):
        return True

    error_text = str(error).upper()
    return "UNAVAILABLE" in error_text or "503" in error_text


def is_network_error(error):
    """True for DNS, connection and timeout failures."""
    if isinstance(
        error,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.TimeoutException,
            ConnectionError,
            TimeoutError,
            socket.gaierror,
            socket.timeout,
        ),
    ):
        return True

    error_text = str(error).lower()
    markers = (
        "getaddrinfo failed",
        "name or service not known",
        "nodename nor servname provided",
        "temporary failure in name resolution",
        "connection refused",
        "connection reset",
        "network is unreachable",
        "failed to establish a new connection",
        "max retries exceeded",
        "connecterror",
        "timed out",
    )
    return any(marker in error_text for marker in markers)


def is_retryable_error(error):
    """Transient failures that are worth retrying with backoff."""
    return (
        is_gemini_unavailable(error)
        or is_gemini_quota_error(error)
        or is_network_error(error)
    )


def parse_retry_after_seconds(error):
    """Read a Retry-After hint from headers or Gemini error details."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)

    if headers is not None:
        try:
            value = headers.get("retry-after")
        except Exception:
            value = None

        if value:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(value))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    seconds = (
                        retry_at - datetime.now(timezone.utc)
                    ).total_seconds()
                    return max(0.0, seconds)
                except (TypeError, ValueError):
                    pass

    def find_retry_delay(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "retryDelay" and isinstance(value, str):
                    match = re.fullmatch(r"([0-9.]+)s", value.strip())
                    if match:
                        return float(match.group(1))
                found = find_retry_delay(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = find_retry_delay(item)
                if found is not None:
                    return found
        return None

    try:
        delay = find_retry_delay(getattr(error, "details", None))
    except Exception:
        delay = None

    return delay


def send_gemini_message(chat, prompt):
    """Send a chat message, retrying transient failures with backoff.

    Retries three additional times after the initial attempt using delays of
    about 1, 2 and 4 seconds. A Retry-After hint from Gemini is respected
    (clamped to a sensible maximum). Permanent errors are re-raised at once.
    """
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        try:
            return chat.send_message(prompt)
        except Exception as error:
            if attempt >= GEMINI_MAX_RETRIES or not is_retryable_error(error):
                raise

            delay = parse_retry_after_seconds(error)
            if delay is None:
                delay = GEMINI_RETRY_DELAYS[attempt]
            delay = min(max(delay, 0.0), GEMINI_MAX_RETRY_AFTER_SECONDS)

            app.logger.warning(
                "Transient Gemini error on attempt %d of %d (%s: %s). "
                "Retrying in %.1f seconds.",
                attempt + 1,
                GEMINI_MAX_RETRIES + 1,
                type(error).__name__,
                redact_sensitive_text(str(error))[:500],
                delay,
            )
            time.sleep(delay)

    raise RuntimeError("Gemini request failed without raising an exception")


login_manager=LoginManager()
login_manager.init_app(app)

#Where authentication users are redirected
login_manager.login_view="login"

@login_manager.user_loader
def load_user(user_id):
    connection = get_db_connection()

    try:
        user = connection.execute(
            "SELECT user_id, username, email, password_hash "
            "FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if user is None:
            return None

        return User(
            user_id=user["user_id"],
            username=user["username"],
            email=user["email"],
            password_hash=user["password_hash"]
        )

    finally:
        connection.close()

def get_db_connection():
    connection = sqlite3.connect(DATABASE)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection

def initialize_database():
    connection = get_db_connection()

    try:
        schema = SCHEMA_FILE.read_text(encoding="utf-8")
        connection.executescript(schema)
        connection.commit()
    finally:
        connection.close()


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")

@app.route("/user-profile")
@login_required
def user_profile():

    return render_template(
        "user_profile.html",
        user=current_user
    )



@app.route("/recommendations")
@login_required
def recommendations():

    recommendations_list = generate_recommendations(current_user.id)

    return render_template(
        "recommendations.html",
        recommendations=recommendations_list
    )
@app.route("/navigation")
@login_required
def navigation():
    connection = get_db_connection()

    try:
        destinations = connection.execute(
            "SELECT * FROM destinations ORDER BY name"
        ).fetchall()
    finally:
        connection.close()

    return render_template(
        "navigation.html",
        destinations=destinations
    )



@app.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences():

    if request.method == "POST":

        preferred_location = request.form.get("preferred_location", "").strip()
        budget_raw = request.form.get("budget", "").strip()
        preferred_activity = request.form.get("preferred_activity", "").strip()
        interest_category = request.form.get("interest_category", "").strip()
        duration_raw = request.form.get("duration_days", "").strip()

        submitted_preferences = {
            "preferred_location": preferred_location,
            "budget": budget_raw,
            "preferred_activity": preferred_activity,
            "duration_days": duration_raw,
            "interest_category": interest_category
        }

        missing_fields = []

        if not preferred_location:
            missing_fields.append("a preferred location")

        if not interest_category:
            missing_fields.append("a main interest")

        if missing_fields:
            flash(
                "Please choose " + " and ".join(missing_fields) + ".",
                "error"
            )

            return render_template(
                "preferences.html",
                preferences=submitted_preferences
            )

        try:
            budget = float(budget_raw)
            duration_days = int(duration_raw)

            if budget < 0 or duration_days < 1:
                raise ValueError

        except ValueError:
            flash("Please enter a valid budget and travel duration.", "error")

            return render_template(
                "preferences.html",
                preferences=submitted_preferences
            )

        connection = get_db_connection()

        try:
            existing = connection.execute(
                """
                SELECT preference_id
                FROM tourist_preferences
                WHERE user_id = ?
                """,
                (current_user.id,)
            ).fetchone()

            if existing:
                connection.execute(
                    """
                    UPDATE tourist_preferences
                    SET preferred_location = ?,
                        budget = ?,
                        preferred_activity = ?,
                        duration_days = ?,
                        interest_category = ?
                    WHERE user_id = ?
                    """,
                    (
                        preferred_location,
                        budget,
                        preferred_activity,
                        duration_days,
                        interest_category,
                        current_user.id
                    )
                )

                #flash("Your preferences have been updated successfully!", "success")

            else:
                connection.execute(
                    """
                    INSERT INTO tourist_preferences (
                        user_id,
                        preferred_location,
                        budget,
                        preferred_activity,
                        duration_days,
                        interest_category
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current_user.id,
                        preferred_location,
                        budget,
                        preferred_activity,
                        duration_days,
                        interest_category
                    )
                )

                #flash("Your preferences have been saved successfully!", "success")

            connection.commit()

        finally:
            connection.close()

        return redirect(url_for("recommendations"))

    connection = get_db_connection()

    try:
        saved_preferences = connection.execute(
            """
            SELECT *
            FROM tourist_preferences
            WHERE user_id = ?
            ORDER BY preference_id DESC
            LIMIT 1
            """,
            (current_user.id,)
        ).fetchone()

    finally:
        connection.close()

    return render_template(
        "preferences.html",
        preferences=saved_preferences
    )


@app.route("/my-preferences")
@login_required
def my_preferences():
    connection = get_db_connection()

    try:
        preferences_list = connection.execute(
            """
            SELECT preference_id, preferred_location, budget,
                   preferred_activity, duration_days, interest_category
            FROM tourist_preferences
            WHERE user_id = ?
            ORDER BY preference_id DESC
            LIMIT 1
            """,
            (current_user.id,)
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "my_preferences.html",
        preferences_list=preferences_list
    )
    
@app.route("/delete-preferences", methods=["POST"])
@login_required
def delete_preferences():
    connection = get_db_connection()

    try:
        connection.execute(
            """
            DELETE FROM tourist_preferences
            WHERE user_id = ?
            """,
            (current_user.id,)
        )

        connection.commit()

        #flash("Your preferences have been deleted successfully!", "success")

    finally:
        connection.close()

    return redirect(url_for("my_preferences"))    
               
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not email or not password or not confirm_password:
            flash("Please fill in all fields.", "error")
            return render_template("register.html")

        if not is_valid_email(email):
            flash("Please enter a valid email address.", "error")
            return render_template("register.html")

        if len(username) < 3 or len(username) > 50:
            flash("Username must be between 3 and 50 characters.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")
            return render_template("register.html")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        password_hash = generate_password_hash(password)
        connection = get_db_connection()

        try:
            connection.execute(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES (?, ?, ?)
                """,
                (username, email, password_hash)
            )
            connection.commit()

        except sqlite3.IntegrityError:
            flash("That username or email is already registered.", "error")
            return render_template("register.html")

        finally:
            connection.close()

        #flash("Registration successful! You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter your email and password.", "error")
            return render_template("login.html")

        connection = get_db_connection()

        try:
            user = connection.execute(
                """
                SELECT user_id, username, email, password_hash
                FROM users
                WHERE email = ?
                """,
                (email,)
            ).fetchone()
        finally:
            connection.close()

        if user is None or not check_password_hash(
            user["password_hash"], password
        ):
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        logged_in_user = User(
            user_id=user["user_id"],
            username=user["username"],
            email=user["email"],
            password_hash=user["password_hash"]
        )

        login_user(logged_in_user)
  
        #flash("Login successful!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    
    logout_user()
    
    return redirect(url_for("login"))

BUDGET_LOW_LIMIT = 100000
BUDGET_MEDIUM_LIMIT = 500000


def classify_budget(budget):
    if budget < BUDGET_LOW_LIMIT:
        return "low"
    elif budget <= BUDGET_MEDIUM_LIMIT:
        return "medium"
    else:
        return "high"


def get_budget_scale():
    """Return the budget thresholds used by classify_budget()."""
    return {
        "low": f"below MWK {BUDGET_LOW_LIMIT:,}",
        "medium": (
            f"MWK {BUDGET_LOW_LIMIT:,} to "
            f"MWK {BUDGET_MEDIUM_LIMIT:,} inclusive"
        ),
        "high": f"above MWK {BUDGET_MEDIUM_LIMIT:,}",
    }


# Destinations do not all store the region in the same way (some use
# "Southern Region", others use a district such as "Mangochi" or
# "Mulanje"). These keywords let us work out the real region of a place.
REGION_KEYWORDS = {
    "southern": (
        "southern", "mangochi", "mulanje", "zomba", "thyolo", "phalombe",
        "blantyre", "chikwawa", "nsanje", "machinga", "balaka", "liwonde"
    ),
    "central": (
        "central", "kasungu", "lilongwe", "salima", "mchinji", "dedza",
        "ntcheu", "dowa", "ntchisi", "nkhotakota"
    ),
    "northern": (
        "northern", "nyika", "vwaza", "nkhata", "rumphi", "karonga",
        "mzimba", "chitipa", "likoma", "matete"
    )
}

# Interest categories are wider than activity names, so each interest is
# linked to the activities that satisfy it.
INTEREST_KEYWORDS = {
    "nature": (
        "nature", "hiking", "waterfall", "scenic", "wildlife",
        "photography", "mountain", "forest", "lake", "camping"
    ),
    "adventure": (
        "hiking", "climbing", "safari", "kayak", "canoe", "biking",
        "camping", "scuba", "snorkel", "boat", "trekking",
        "horse riding", "game drive"
    ),
    "wildlife": ("wildlife", "safari", "game drive"),
    "culture": ("cultural", "historical", "village", "heritage", "site"),
    "beaches": (
        "swimming", "beach", "snorkel", "kayak", "boat", "lake",
        "scuba", "fishing"
    ),
    "relaxation": (
        "relax", "swimming", "boat", "photography", "scenic",
        "nature walk", "lake"
    )
}


def normalize_region(place_name):
    """Return 'northern', 'central', 'southern' or '' for a place name."""
    text = (place_name or "").lower().strip()

    if not text or "anywhere" in text:
        return ""

    for region, keywords in REGION_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return region

    return ""


def calculate_activity_score(preferred_activity, destination_activities):
    preferred = (preferred_activity or "").lower().strip()
    activities = (destination_activities or "").lower()

    if not preferred or not activities:
        return 0

    if preferred in activities:
        return 100

    for keyword in INTEREST_KEYWORDS.get(preferred, ()):
        if keyword in activities:
            return 100

    return 0


def calculate_budget_score(user_budget, destination_budget):
    user_category = classify_budget(user_budget)

    if user_category == (destination_budget or "").lower().strip():
        return 100

    return 0


def calculate_location_score(preferred_location, destination_location,
                             destination_name=""):
    preferred = (preferred_location or "").lower().strip()

    # "Anywhere in Malawi", or no choice at all, accepts every region.
    if not preferred or "anywhere" in preferred:
        return 100

    preferred_region = normalize_region(preferred)
    destination_region = (
        normalize_region(destination_name)
        or normalize_region(destination_location)
    )

    if preferred_region and destination_region:
        if preferred_region == destination_region:
            return 100

        return 0

    # Fall back to text matching for places that are not recognised above.
    destination = (destination_location or "").lower().strip()

    if preferred in destination or destination in preferred:
        return 100

    return 0


def matches_preferred_region(preferred_location, destination_location,
                             destination_name=""):
    """Check whether a destination belongs to the chosen region."""
    preferred = (preferred_location or "").lower().strip()

    if not preferred or "anywhere" in preferred:
        return True

    preferred_region = normalize_region(preferred)

    if not preferred_region:
        return True

    destination_region = (
        normalize_region(destination_name)
        or normalize_region(destination_location)
    )

    return destination_region == preferred_region


def calculate_total_score(activity_score, budget_score,
                          location_score):

    total_score = (
        (activity_score * 0.25) +
        (budget_score * 0.30) +
        (location_score * 0.45) 
        
    )

    return round(total_score, 2)

def generate_recommendations(user_id):
    connection = get_db_connection()

    try:
        preference = connection.execute(
            """
            SELECT *
            FROM tourist_preferences
            WHERE user_id = ?
            ORDER BY preference_id DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()

        if preference is None:
            return []

        destinations = connection.execute(
            "SELECT * FROM destinations"
        ).fetchall()

        preferred_location = preference["preferred_location"]

        # The preferences form saves the choice as "interest_category",
        # so use it whenever "preferred_activity" is empty.
        preferred_activity = (
            (preference["preferred_activity"] or "").strip()
            or (preference["interest_category"] or "").strip()
        )

        recommendations = []

        for destination in destinations:

            # A tourist who chose a specific region must only receive
            # destinations in that region. Without this check a Northern
            # destination can outrank Southern ones because of its
            # activity and budget points.
            if not matches_preferred_region(
                preferred_location,
                destination["location"],
                destination["name"]
            ):
                continue

            activity_score = calculate_activity_score(
                preferred_activity,
                destination["activities"]
            )

            budget_score = calculate_budget_score(
                preference["budget"],
                destination["budget_level"]
            )

            location_score = calculate_location_score(
                preferred_location,
                destination["location"],
                destination["name"]
            )

            total_score = calculate_total_score(
                activity_score,
                budget_score,
                location_score
            )

            # Exclude destinations that match neither the activity nor
            # the budget preference.
            if activity_score == 0 and budget_score == 0:
                continue

            destination_data = dict(destination)
            destination_data["activity_score"] = activity_score
            destination_data["budget_score"] = budget_score
            destination_data["location_score"] = location_score
            destination_data["match_score"] = total_score

            recommendations.append(destination_data)

        recommendations.sort(
            key=lambda item: (
                item["match_score"],
                item["location_score"],
                item["budget_score"],
                item["activity_score"]
            ),
            reverse=True
        )

        return recommendations

    finally:
        connection.close()

def add_malawi_destinations():
    connection = get_db_connection()

    destinations = [
        (
            "Lake Malawi",
            "Southern Region",
            "A beautiful freshwater lake known for its clear water, beaches, and colourful fish.",
            "medium",
            "Swimming, Snorkelling, Kayaking, Boat Trips,Scuba diving",
            
        ),
        (
            "Liwonde National Park",
            "Southern Malawi",
            "A wildlife destination known for elephants, hippos, crocodiles, and safari experiences.",
            "high",
            "Wildlife Safari, Bird Watching, Boat Safari,Helicopter rides",
            
        ),
        (
            "Mount Mulanje",
            "Southern Region",
            "A spectacular mountain destination offering hiking trails, waterfalls, and scenic landscapes.",
            "medium",
            "Hiking, Mountain Climbing, Nature Walks, Photography",
            
        ),
        (
            "Nyika National Park",
            "Northern Malawi",
            "A highland national park featuring rolling grasslands, wildlife, and beautiful scenery.",
            "high",
            "Wildlife Viewing, Hiking, Bird Watching, Photography",
            
        ),
        (
            "Lake Chilwa",
            "Southern Region",
            "A shallow lake and wetland area known for birdlife, fishing communities, and natural scenery.",
            "low",
            "Bird Watching, Photography, Cultural Experiences",
            
        ),
        (
            "Majete Wildlife reserve",
            "Southern Region",
            "Majete  hosts more than 4000 animals, including the Big 5! . ",
            "low",
            "Wildlife viewing,Game drive,Walking Safari,Boat Safari",
            
        ),
        (
            "Kasungu National Park",
            "Central Region",
            "The Park has over 300 species of birds which include a variety of waterbirds.",
            "medium",
            "Sport Fishing,Wildlife viewing,Bird Watching,historical sites visits, Wildlife Safari",
            
        ),
        (
            "Nkhotankhota Wildlife Reserve",
            "Central Region",
            "Nkhotakota is a rugged terrain crossed by many rivers tumbling down the edge of the escarpment as they make their way to the lake",
            "medium",
            "Wildlife Safari,Fishing,Walking Safari,Canoeing,Bird Watching",
            
        ),
        (
            "Vwaza Wildlife Reserve",
            "Northern Region",
            "It offers four of the five big games namely Elephant, Buffalo, Lion, Leopard and animal species large numbers of hippopotamus, and many others.",
            "low",
            "Wildlife Viewing,Janda Hill,Bird Watching",
            
        ),
        (
            "Zomba Plateu",
            "Southern Region",
            "Panoramic vantage points offering sweeping views across the Shire Valley, Lake Chilwa, and Mulanje Massif on clear days",
            "medium",
            "Horse Riding,mountain hiking"
        ),
        (
            "Fort Matete Site",
            "Northern Region",
            "Fort Matete is a historical defensive post located in Nkhata Bay District along Lake Malawi ,marking the colonial and regional anti-slave-trade military operations of the late 19th century",
            "low",
            "Historical Sightseeing, Bird Watching, Hiking Trails, Cultural Village Tours, Lake Photography"
        ),
        (
          "Malape Pillars",
          "Southern Region",
          "The Malape Pillars are a striking geological feature at the foot of the Chikala Hills, consisting of towering, labyrinthine sandstone columns and spires sculpted over millions of years by differential wind and water erosion",
          "low",
          "Hiking, Biking, Bird Watching, Geological Photography, Cultural Exploration"  
        ),
        (
          "Fort Mangochi",
          "Southern Region",
          "Fort Mangochi is a highly preserved late-19th-century historical ruin situated on the high plateau of Mangochi Mountain, originally built as a defensive stronghold by the Yao people under Chief Jalasi before being captured and reinforced by British colonial forces to deter the regional slave trade",
          "low",
          "Historical Exploration, Mountain Trekking, Scenic Photography, Bird Watching, Cultural Heritage Tours"
        ),
        (
          "Zoa Falls",
          "Southern Region",
          "Zoa Falls is a breathtaking, rocky waterfall hidden along the Ruo River in Thyolo District near the Mozambique border, featuring dramatic cascades, a seasonal summer sandbank pool, and historical remnants left by pre-independence colonial settlers",
          "low",
          "Waterfall Viewing, Nature Hiking, Wilderness Camping, Scenic Photography, Historical Sightseeing"
        ),
        (
          "Mpoto Lagoon (Phalombe)",
          "Southern Region",
          "Mpoto Lagoon is an off-the-beaten-path, raw water body in Phalombe District that is fed by rivers from the Mulanje Massif and flows into the Lake Chilwa basin, offering an undisturbed wilderness experience bordering a traditional fishing village",
          "low",
          "Canoeing, Wilderness Camping, Fishing, Bird Watching, Scenic Photography"  
        )
            
    ]
    
    for destination in destinations:
        existing = connection.execute(
            "SELECT destination_id FROM destinations WHERE name = ?",
            (destination[0],)
        ).fetchone()

        if existing is None:
            connection.execute("""
                INSERT INTO destinations (
                    name, location, description,
                    budget_level, activities
                ) VALUES (?, ?, ?, ?, ?)
            """, destination)

        else:
            connection.execute(
                """
                UPDATE destinations
                SET location = ?,
                    description = ?,
                    budget_level = ?,
                    activities = ?
                WHERE destination_id = ?
                """,
                (
                    destination[1],
                    destination[2],
                    destination[3],
                    destination[4],
                    existing["destination_id"]
                )
            )

    connection.commit()
    connection.close()
    
@app.route("/booking", methods=["GET", "POST"])
@login_required
def booking():

    connection = get_db_connection()

    try:
        # Create booking tables if they do not exist
        connection.execute("""
            CREATE TABLE IF NOT EXISTS accommodations (
                accommodation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT NOT NULL,
                description TEXT,
                contact TEXT
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS booking_requests (
                booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                accommodation_id INTEGER NOT NULL,
                check_in DATE NOT NULL,
                check_out DATE NOT NULL,
                guests INTEGER NOT NULL,
                status TEXT DEFAULT 'Pending',
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (accommodation_id)
                    REFERENCES accommodations(accommodation_id)
            )
        """)

        if request.method == "POST":
            accommodation_id = request.form.get("accommodation_id")
            check_in = request.form.get("check_in")
            check_out = request.form.get("check_out")
            guests_raw = request.form.get("guests")

            try:
                guests = int(guests_raw)

                if (not check_in or not check_out
                        or check_out <= check_in or guests < 1):
                    raise ValueError

                accommodation = connection.execute(
                    "SELECT accommodation_id FROM accommodations WHERE accommodation_id = ?",
                    (accommodation_id,)
                ).fetchone()

                if not accommodation:
                    flash("Please select a valid accommodation.", "error")
                    return redirect(url_for("booking"))

                connection.execute("""
                    INSERT INTO booking_requests
                    (user_id, accommodation_id, check_in, check_out, guests)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    current_user.id,
                    accommodation_id,
                    check_in,
                    check_out,
                    guests
                ))

                connection.commit()
                flash("Your booking request has been submitted!", "success")

            except (ValueError, TypeError):
                flash("Please enter valid dates and number of guests.", "error")

            return redirect(url_for("booking"))

        accommodations = connection.execute(
            "SELECT * FROM accommodations ORDER BY name"
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "booking.html",
        accommodations=accommodations
    )
# ---------------------------------------------------------------------------
# Tourism assistant (chatbot)
# ---------------------------------------------------------------------------

WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


def clean_text(value):
    """Remove HTML tags and extra whitespace from a text fragment."""
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)

    return re.sub(r"\s+", " ", text).strip()


def search_wikipedia(query):
    """Return short Wikipedia results for a query."""
    response = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "utf8": 1,
            "srlimit": 3,
        },
        headers=SEARCH_HEADERS,
        timeout=15,
    )
    response.raise_for_status()

    results = response.json().get("query", {}).get("search", [])
    items = []

    for result in results:
        title = result.get("title", "")

        if not title:
            continue

        items.append({
            "title": title,
            "summary": clean_text(result.get("snippet")),
            "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
        })

    return items


def search_news(query):
    """Return recent news results from Google News RSS."""
    response = requests.get(
        "https://news.google.com/rss/search",
        params={
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        },
        headers=SEARCH_HEADERS,
        timeout=15,
    )
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = []

    for item in root.findall("./channel/item")[:5]:
        items.append({
            "title": clean_text(item.findtext("title")),
            "source": clean_text(item.findtext("source")),
            "published": clean_text(item.findtext("pubDate")),
            "url": clean_text(item.findtext("link")),
        })

    return items


def get_current_weather(place):
    """Return current weather and a short forecast using Open-Meteo."""
    geo_response = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={
            "name": place,
            "count": 1,
            "language": "en",
            "format": "json",
        },
        headers=SEARCH_HEADERS,
        timeout=15,
    )
    geo_response.raise_for_status()

    results = geo_response.json().get("results") or []

    if not results:
        return {"error": f"I could not find a place called '{place}'."}

    location = results[0]

    forecast_response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "precipitation,weather_code,wind_speed_10m"
            ),
            "daily": (
                "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "timezone": "auto",
            "forecast_days": 3,
        },
        headers=SEARCH_HEADERS,
        timeout=15,
    )
    forecast_response.raise_for_status()
    forecast = forecast_response.json()

    current = forecast.get("current", {})
    daily = forecast.get("daily", {})

    return {
        "place": location.get("name"),
        "country": location.get("country"),
        "timezone": forecast.get("timezone"),
        "current": {
            "time": current.get("time"),
            "condition": WEATHER_CODES.get(
                current.get("weather_code"), "Unknown"
            ),
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_kmh": current.get("wind_speed_10m"),
        },
        "daily": {
            "dates": daily.get("time", []),
            "max_c": daily.get("temperature_2m_max", []),
            "min_c": daily.get("temperature_2m_min", []),
            "rain_chance_percent": daily.get(
                "precipitation_probability_max", []
            ),
        },
    }


def search_internet(query: str) -> str:
    """Search the internet for current news and general information.

    Use this tool for news, events, prices, opening hours, recent changes or
    general background information. For weather, use the get_weather tool
    instead.

    Args:
        query: The search question, for example "Malawi tourism news" or
            "Lake Malawi water levels 2026".
    """
    text = (query or "").strip()

    if not text:
        return json.dumps({"error": "No search query was provided."})

    try:
        results = {
            "query": text,
            "news": search_news(text),
            "wikipedia": search_wikipedia(text),
            "note": (
                "News results come from Google News and may be recent. "
                "Mention the source URLs when you use this information."
            ),
        }

        return json.dumps(results, default=str)

    except Exception as error:
        return json.dumps({
            "error": f"The internet search failed: {error}"
        })


def get_weather(place: str) -> str:
    """Get the current weather and a three-day forecast for a place.

    Args:
        place: The town, city or destination, for example "Lilongwe" or
            "Mangochi".
    """
    try:
        return json.dumps(
            {"weather": get_current_weather(place)},
            default=str
        )

    except Exception as error:
        return json.dumps({
            "error": f"The weather lookup failed: {error}"
        })


def format_mwk(amount):
    """Format a Malawi Kwacha amount for the assistant prompt."""
    if amount is None:
        return "not set"

    amount = float(amount)

    if amount.is_integer():
        return f"MWK {amount:,.0f}"

    return f"MWK {amount:,.2f}"


def get_latest_preference(user_id):
    """Return the newest saved preference row for a tourist."""
    connection = get_db_connection()

    try:
        return connection.execute(
            """
            SELECT preferred_location, budget, preferred_activity,
                   duration_days, interest_category
            FROM tourist_preferences
            WHERE user_id = ?
            ORDER BY preference_id DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()

    finally:
        connection.close()


def build_preference_summary(preference):
    """Build the authoritative preference summary used by the chatbot."""
    if preference is None:
        return None

    budget = preference["budget"]
    preferred_activity = (preference["preferred_activity"] or "").strip()
    interest_category = (preference["interest_category"] or "").strip()

    return {
        "preferred_location": preference["preferred_location"],
        "budget_mwk": budget,
        "budget_category": (
            classify_budget(budget) if budget is not None else None
        ),
        "preferred_activity": preferred_activity or None,
        "interest_category": interest_category or None,
        "activity_used_for_matching": (
            preferred_activity or interest_category or None
        ),
        "duration_days": preference["duration_days"],
    }


def format_authoritative_preferences(preference_summary):
    """Format saved preferences for every chatbot request."""
    if not preference_summary:
        return "No saved preferences were found for this tourist."

    return (
        f"- Preferred location: "
        f"{preference_summary['preferred_location'] or 'not set'}\n"
        f"- Budget: {format_mwk(preference_summary['budget_mwk'])}\n"
        f"- Tourist budget category: "
        f"{preference_summary['budget_category'] or 'unknown'}\n"
        f"- Activity used for matching: "
        f"{preference_summary['activity_used_for_matching'] or 'not set'}\n"
        f"- Duration: "
        f"{preference_summary['duration_days'] or 'not set'} days"
    )


def format_authoritative_recommendations(recommendations):
    """Format the recommendation engine output for the chatbot prompt."""
    if not recommendations:
        return "No destinations matched the saved preferences."

    lines = []

    for item in recommendations:
        lines.append(
            f"- {item['name']} ({item['location']}): "
            f"match_score {item['match_score']}, "
            f"activity_score {item['activity_score']}, "
            f"budget_score {item['budget_score']}, "
            f"location_score {item['location_score']}, "
            f"destination budget_level {item['budget_level']}"
        )

    return "\n".join(lines)


def get_recommendations_tool(user_id):
    """Build the recommendation tool for one logged-in tourist."""

    def get_recommendations() -> str:
        """Return this tourist's recommendations from the app's engine.

        Use this tool whenever the question is about recommended
        destinations, match scores, why destinations were recommended,
        what the tourist can afford, or which destinations fit the saved
        preferences. The scores and ordering come from
        generate_recommendations() and must not be recalculated or changed.
        """
        recommendations = generate_recommendations(user_id)

        return json.dumps({
            "scoring_weights": {
                "activity": 0.25,
                "budget": 0.30,
                "location": 0.45,
            },
            "recommendations": recommendations,
            "note": (
                "These are the only valid recommendations. Use match_score "
                "and the score breakdown exactly as returned. Never invent, "
                "recalculate, reorder, add or remove recommendations."
            ),
        }, default=str)

    return get_recommendations


TIME_SENSITIVE_PATTERN = re.compile(
    r"\b(current|currently|today|now|latest|recent|news|trends?|"
    r"202[0-9]|prices?|costs?|fees?|rates?|opening|events?|"
    r"festivals?|season|availability)\b",
    re.IGNORECASE,
)


def is_time_sensitive_question(message):
    """Return True when a question needs current information."""
    return bool(TIME_SENSITIVE_PATTERN.search(message or ""))


def build_live_search_context(query):
    """Run the configured live search and format its results for the prompt."""
    try:
        result = json.loads(search_internet(query))
    except Exception as error:
        return f"Live search failed: {error}"

    if result.get("error"):
        return f"Live search failed: {result['error']}"

    news = result.get("news") or []
    wikipedia = result.get("wikipedia") or []

    if not news and not wikipedia:
        return (
            "No live results were found. Tell the tourist that the "
            "information cannot be verified right now."
        )

    lines = []

    for item in news[:5]:
        lines.append(
            f"- NEWS: {item.get('title')} | {item.get('source')} | "
            f"{item.get('url')}"
        )

    for item in wikipedia[:3]:
        lines.append(
            f"- BACKGROUND: {item.get('title')} | {item.get('url')}"
        )

    return "\n".join(lines)


def search_tourism_database_tool(user_id):
    """Build the database tool for one logged-in tourist."""

    def search_tourism_database(query: str) -> str:
        """Search the local Malawi tourism database.

        Use this tool whenever the question is about the destinations stored
        in this project, their region, budget level or activities, the
        tourist's saved travel preferences (including the app-calculated
        budget_category), available accommodation, or the tourist's booking
        requests.

        Args:
            query: Keywords describing what the tourist is looking for, for
                example "wildlife destinations in the southern region" or
                "my saved preferences and bookings".
        """
        connection = get_db_connection()

        try:
            preference = connection.execute(
                """
                SELECT preferred_location, budget, preferred_activity,
                       duration_days, interest_category
                FROM tourist_preferences
                WHERE user_id = ?
                ORDER BY preference_id DESC
                LIMIT 1
                """,
                (user_id,)
            ).fetchone()

            destinations = connection.execute(
                """
                SELECT name, location, description, budget_level, activities
                FROM destinations
                ORDER BY name
                """
            ).fetchall()

            accommodations = connection.execute(
                """
                SELECT name, location, description, contact
                FROM accommodations
                ORDER BY name
                """
            ).fetchall()

            bookings = connection.execute(
                """
                SELECT a.name AS accommodation, a.location, b.check_in,
                       b.check_out, b.guests, b.status
                FROM booking_requests b
                JOIN accommodations a
                  ON a.accommodation_id = b.accommodation_id
                WHERE b.user_id = ?
                ORDER BY b.check_in DESC
                """,
                (user_id,)
            ).fetchall()

            words = [
                word for word in re.findall(r"[a-z0-9]+", query.lower())
                if len(word) > 2
            ]

            def relevance(row):
                text = " ".join(
                    str(row[key]) for key in row.keys()
                ).lower()

                return sum(1 for word in words if word in text)

            scored_destinations = sorted(
                (
                    (relevance(destination), destination)
                    for destination in destinations
                ),
                key=lambda item: item[0],
                reverse=True
            )

            # Put the best matches first and drop rows that share no keyword.
            matching_destinations = [
                dict(destination)
                for score, destination in scored_destinations
                if score > 0
            ]

            # The destination table is small, so return everything when the
            # keywords do not match a specific record.
            if not matching_destinations:
                matching_destinations = [
                    dict(destination) for destination in destinations
                ]

            database_result = {
                "tourist_saved_preferences": build_preference_summary(
                    preference
                ),
                "budget_scale": get_budget_scale(),
                "destination_budget_level_meaning": (
                    "Each destination's budget_level is the destination's "
                    "own price tier. It is not the tourist's budget category."
                ),
                "expense_data_note": (
                    "The database does not store accommodation prices, "
                    "transport costs or park entrance fees. Never invent "
                    "those amounts."
                ),
                "recommendation_note": (
                    "Do not treat this destination list as recommendations. "
                    "Use get_recommendations for the tourist's "
                    "recommendations and match scores."
                ),
                "destinations": matching_destinations,
                "accommodations": [dict(row) for row in accommodations],
                "tourist_bookings": [dict(row) for row in bookings],
            }

            return json.dumps(database_result, default=str)

        finally:
            connection.close()

    return search_tourism_database


@app.route("/chatbot/message", methods=["POST"])
@login_required
def chatbot_message():

    if not gemini_client:
        return jsonify({
            "reply": (
                "The AI assistant is not configured. "
                "Add a valid GEMINI_API_KEY to the .env file."
            )
        }), 500

    data = request.get_json(silent=True) or {}
    message_raw = data.get("message", "")
    message = message_raw.strip() if isinstance(message_raw, str) else ""
    history = data.get("history", [])

    if not isinstance(history, list):
        history = []

    if not message:
        return jsonify({"reply": "Please enter a question."}), 400

    # The browser adds the current question to the history before sending it.
    if (history and isinstance(history[-1], dict)
            and history[-1].get("role") == "user"
            and history[-1].get("text") == message):
        history = history[:-1]

    conversation = ""

    for item in history[-10:]:
        if not isinstance(item, dict):
            continue

        role = item.get("role", "")
        text = item.get("text", "")

        if role in ("user", "model") and isinstance(text, str):
            speaker = "Tourist" if role == "user" else "Assistant"
            conversation += f"\n{speaker}: {text[:1500]}"

    database_tool = search_tourism_database_tool(current_user.id)
    recommendations_tool = get_recommendations_tool(current_user.id)

    try:
        preference = get_latest_preference(current_user.id)
        preference_summary = build_preference_summary(preference)
        recommendations = generate_recommendations(current_user.id)

        preferences_text = format_authoritative_preferences(
            preference_summary
        )
        recommendations_text = format_authoritative_recommendations(
            recommendations
        )
        live_search_text = (
            build_live_search_context(message)
            if is_time_sensitive_question(message)
            else "No live search was needed for this question."
        )

        prompt = f"""
You are the Malawi Tourism Conversational Assistant for the Explore Malawi website.

You can help in these ways:
1. Answer general tourism questions naturally from your own knowledge.
2. Use the search_tourism_database tool for the destinations, activities,
   accommodation, saved preferences and bookings stored in this project.
   Always use the tool instead of guessing about those records.
3. Use the get_recommendations tool (or the authoritative recommendations
   below) for recommended destinations, match scores and explanations. Both
   come from the app's recommendation engine.
4. Use the search_internet tool for current trends, prices, events, opening
   hours or other time-sensitive information, and the get_weather tool for
   current weather and forecasts.

PREVIOUS CONVERSATION (may be outdated):
{conversation}

AUTHORITATIVE SAVED PREFERENCES (latest database row; overrides anything above):
{preferences_text}

AUTHORITATIVE RECOMMENDATIONS (computed by generate_recommendations; overrides anything above):
{recommendations_text}

LIVE SEARCH RESULTS (from the configured live search; use these for current facts):
{live_search_text}

Tourist's latest question:
{message}

Rules:
- The tourist's budget category is exactly the "budget_category" value above
  and in the database tool. Never classify the raw budget amount yourself,
  and never use a destination's budget_level as the tourist's category.
- A destination's budget_level describes that destination's price tier only.
- Use the recommendations, match scores and score breakdown exactly as the app
  returns them. Do not invent, recalculate, reorder, add or remove
  recommendations, and do not recommend destinations outside the saved region
  when a specific region is selected.
- If the latest database data contradicts an earlier message in the
  conversation, trust the latest database data and correct the earlier answer.
- For current or time-sensitive information, rely only on the LIVE SEARCH
  RESULTS above (or a targeted search_internet call). Every current fact or
  trend must be supported by those results and cite a source link. Do not add
  unsourced trends. If the results are missing, insufficient or say they
  failed, tell the tourist the information cannot be verified right now.
  Never invent current facts.
- Never invent accommodation prices, transport costs, park entrance fees or
  other travel expenses. Only quote amounts that appear in tool results;
  otherwise tell the tourist to confirm with the provider.
- Do not invent hotel contacts, opening hours or other specific facts.
- Write in plain text with short paragraphs and simple numbered lists.
  Avoid Markdown headings and tables.
- When citing an internet source, use a short Markdown link such as
  [Reuters](URL) instead of showing the full URL.
- Keep answers friendly, clear and reasonably short.
"""

        generation_config = types.GenerateContentConfig(
            tools=[
                database_tool,
                recommendations_tool,
                search_internet,
                get_weather,
            ],
            temperature=0.4,
        )

        def open_chat(model_name):
            return gemini_client.chats.create(
                model=model_name,
                config=generation_config,
            )

        try:
            response = send_gemini_message(open_chat(GEMINI_MODEL), prompt)
        except Exception as primary_error:
            can_use_fallback = (
                bool(GEMINI_FALLBACK_MODEL)
                and GEMINI_FALLBACK_MODEL != GEMINI_MODEL
                and is_retryable_error(primary_error)
            )

            if not can_use_fallback:
                raise

            app.logger.warning(
                "Primary Gemini model %s failed after retries (%s: %s). "
                "Trying fallback model %s.",
                GEMINI_MODEL,
                type(primary_error).__name__,
                redact_sensitive_text(str(primary_error))[:500],
                GEMINI_FALLBACK_MODEL,
            )

            response = send_gemini_message(
                open_chat(GEMINI_FALLBACK_MODEL),
                prompt,
            )

        return jsonify({
            "reply": response.text or "Sorry, I couldn't generate an answer."
        })

    except Exception as error:
        app.logger.error(
            "Gemini chatbot request failed: %s: %s\n%s",
            type(error).__name__,
            redact_sensitive_text(str(error))[:2000],
            redact_sensitive_text(traceback.format_exc()),
        )

        if is_gemini_auth_error(error):
            return jsonify({"reply": AI_AUTH_MESSAGE}), 500

        if is_gemini_quota_error(error):
            return jsonify({"reply": AI_QUOTA_MESSAGE}), 429

        if is_gemini_unavailable(error):
            return jsonify({"reply": AI_HIGH_DEMAND_MESSAGE}), 503

        if is_network_error(error):
            return jsonify({"reply": AI_NETWORK_MESSAGE}), 503

        return jsonify({"reply": AI_UNEXPECTED_MESSAGE}), 500
        
@app.route("/chatbot")
@login_required
def chatbot():
    return render_template("chatbot.html")

@app.route("/destination/<int:destination_id>")
@login_required
def destination_details(destination_id):

    

    connection = get_db_connection()

    try:
        destination = connection.execute(
            """
            SELECT *
            FROM destinations
            WHERE destination_id = ?
            """,
            (destination_id,)
        ).fetchone()

    finally:
        connection.close()

    if not destination:
        flash("Destination not found.", "error")
        return redirect(url_for("recommendations"))

    return render_template(
        "destination_details.html",
        destination=destination
    )
        
@app.route("/check-destinations")
@login_required
def check_destinations():
    connection = get_db_connection()

    destinations = connection.execute(
        "SELECT * FROM destinations"
    ).fetchall()

    connection.close()

    return {
        "number_of_destinations": len(destinations),
        "destinations": [dict(row) for row in destinations]
    }
    
   
if __name__ == "__main__":
    initialize_database()
    add_malawi_destinations()
    app.run(debug=True,port=5001)
    