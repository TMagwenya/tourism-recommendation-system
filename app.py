import sqlite3
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin
from werkzeug.security import generate_password_hash

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

@app.route("/register", methods=["GET", "POST"])
def register():


 if request.method == "POST":

    # Get the submitted form data
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    # Validate the submitted information
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

    # Hash the password before saving it
    password_hash = generate_password_hash(password)

    connection = get_db_connection()

    try:
        # Save the new user in SQLite
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

    flash("Registration successful! You can now log in.", "success")
    return redirect(url_for("login"))

 return render_template("register.html")



@app.route("/login", methods=["GET"])
def login():
    return render_template("login.html")


if __name__ == "__main__":
    initialize_database()
    app.run(debug=True,port=5001)
    