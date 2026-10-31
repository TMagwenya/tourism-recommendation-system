import sqlite3
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required ,current_user
from werkzeug.security import generate_password_hash, check_password_hash

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

# Required for Flask sessions.
# Replace this development key before deploying the application.
app.config["SECRET_KEY"] = "replace-this-with-a-random-secret-key"

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

@app.route("/recommendations")
@login_required
def recommendations():

    recommendations_list = generate_recommendations(current_user.id)

    return render_template(
        "recommendations.html",
        recommendations=recommendations_list
    )

@app.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences():
    if request.method == "POST":
        preferred_location = request.form.get("preferred_location", "").strip()
        budget_raw = request.form.get("budget", "").strip()
        preferred_activity = request.form.get("preferred_activity", "").strip()
        duration_raw = request.form.get("duration_days", "").strip()
        interest_category = request.form.get("interest_category", "").strip()

        try:
            budget = float(budget_raw)
            duration_days = int(duration_raw)
        except ValueError:
            flash("Please enter a valid budget and duration.", "error")
            return render_template("preferences.html")

        if budget < 0 or duration_days < 1:
            flash("Budget must be zero or more, and duration must be at least one day.", "error")
            return render_template("preferences.html")

        connection = get_db_connection()
        try:
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
            connection.commit()
        finally:
            connection.close()

        flash("Your travel preferences have been saved successfully!", "success")
        return redirect(url_for("dashboard"))

    return render_template("preferences.html")

@app.route("/my-preferences")
@login_required
def my_preferences():
    connection = get_db_connection()


    try:
        preferences_list = connection.execute(
        """
        SELECT preferred_location, budget, preferred_activity,
               duration_days, interest_category
        FROM tourist_preferences
        WHERE user_id = ?
        ORDER BY preference_id DESC
        """,
        (current_user.id,)
    ).fetchall()
    finally:
        connection.close()

    return render_template(
        "my_preferences.html",
        preferences_list=preferences_list
)
               
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
    flash("You have been logged out successfully.", "success")
    return redirect(url_for("home"))

def classify_budget(budget):
    if budget < 100000:
        return "low"
    elif budget <= 500000:
        return "medium"
    else:
        return "high"
def calculate_activity_score(preferred_activity, destination_activities):
    preferred = preferred_activity.lower().strip()
    activities = destination_activities.lower()

    if preferred in activities:
        return 100

    return 0

def calculate_budget_score(user_budget, destination_budget):
    user_category = classify_budget(user_budget)

    if user_category == destination_budget.lower():
        return 100

    return 0

def calculate_location_score(preferred_location, destination_location):
    preferred = preferred_location.lower().strip()
    destination = destination_location.lower().strip()

    if preferred == "":
        return 100

    if preferred in destination or destination in preferred:
        return 100

    return 0
def calculate_duration_score(user_duration, destination_duration):
    difference = abs(user_duration - destination_duration)

    if difference == 0:
        return 100
    elif difference == 1:
        return 75
    elif difference == 2:
        return 50
    elif difference == 3:
        return 25
    else:
        return 0

def calculate_total_score(activity_score, budget_score,
                          location_score, duration_score):

    total_score = (
        (activity_score * 0.40) +
        (budget_score * 0.25) +
        (location_score * 0.20) +
        (duration_score * 0.15)
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

        recommendations = []

        for destination in destinations:

            activity_score = calculate_activity_score(
                preference["preferred_activity"],
                destination["activities"]
            )

            budget_score = calculate_budget_score(
                preference["budget"],
                destination["budget_level"]
            )

            location_score = calculate_location_score(
                preference["preferred_location"],
                destination["location"]
            )

            duration_score = calculate_duration_score(
                preference["duration_days"],
                destination["recommended_duration"]
            )

            total_score = calculate_total_score(
                activity_score,
                budget_score,
                location_score,
                duration_score
            )

            destination_data = dict(destination)
            destination_data["match_score"] = total_score

            recommendations.append(destination_data)

        recommendations.sort(
            key=lambda item: item["match_score"],
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
            "Mangochi",
            "A beautiful freshwater lake known for its clear water, beaches, and colourful fish.",
            "medium",
            "Swimming, Snorkelling, Kayaking, Boat Trips,Scuba diving",
            3
        ),
        (
            "Liwonde National Park",
            "Southern Malawi",
            "A wildlife destination known for elephants, hippos, crocodiles, and safari experiences.",
            "high",
            "Wildlife Safari, Bird Watching, Boat Safari,Helicopter rides",
            2
        ),
        (
            "Mount Mulanje",
            "Mulanje",
            "A spectacular mountain destination offering hiking trails, waterfalls, and scenic landscapes.",
            "medium",
            "Hiking, Mountain Climbing, Nature Walks, Photography",
            3
        ),
        (
            "Nyika National Park",
            "Northern Malawi",
            "A highland national park featuring rolling grasslands, wildlife, and beautiful scenery.",
            "high",
            "Wildlife Viewing, Hiking, Bird Watching, Photography",
            3
        ),
        (
            "Lake Chilwa",
            "Southern Region",
            "A shallow lake and wetland area known for birdlife, fishing communities, and natural scenery.",
            "low",
            "Bird Watching, Photography, Cultural Experiences",
            1
        ),
        (
            "Majete Wildlife reserve",
            "Southern Region",
            "Majete now hosts more than 4000 animals, including the Big 5! Elephants, lion, leopard, black rhinos, buffalos, elands, nyalas, zebras and hippos can be seen in the reserve. ",
            "low",
            "Game drive,Walking Safari,Boat Safari",
            3
        ),
        (
            "Kasungu National Park",
            "Southern Region",
            "The Park has over 300 species of birds which include a variety of waterbirds such as the palearctic migrant White Stork. and woodland birds such as the Shelley’s Sunbird",
            "medium",
            "Sport Fishing,Bird Watching,historical sites visits, Game Viewing",
            1
        ),
        (
            "Nkhotankhota Wildlife Reserve",
            "Northern Region",
            "Nkhotakota is a rugged terrain crossed by many rivers tumbling down the edge of the escarpment as they make their way to the lake",
            "medium",
            "Fishing,Walking Safari,Canoeing,Bird Watching",
            1
        ),
        (
            "Vwaza Wildlife Reserve",
            "Northern Region",
            "It offers four of the five big games namely Elephant, Buffalo, Lion, Leopard and animal species large numbers of hippopotamus, and many others.",
            "low",
            "Game Viewing,Janda Hill,Bird Watching",
            1
        )
            
    ]
    
    for destination in destinations:
        existing = connection.execute(
            "SELECT 1 FROM destinations WHERE name = ? AND location = ?",
            (destination[0], destination[1])
        ).fetchone()

        if existing is None:
            connection.execute("""
                INSERT INTO destinations (
                    name, location, description,
                    budget_level, activities, recommended_duration
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, destination)

    connection.commit()
    connection.close()

 

@app.route("/check-destinations")
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
    