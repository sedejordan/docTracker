from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, session,
    url_for, send_from_directory, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import os
import sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

DB_PATH = os.path.join(os.getcwd(), "documents.db")
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "docx", "doc", "txt"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_documents(user_id, search_query=""):
    with get_db() as conn:
        cursor = conn.cursor()
        if search_query:
            cursor.execute(
                "SELECT * FROM documents WHERE user_id = ? AND title LIKE ? ORDER BY expiry_date ASC",
                (user_id, f"%{search_query}%")
            )
        else:
            cursor.execute(
                "SELECT * FROM documents WHERE user_id = ? ORDER BY expiry_date ASC",
                (user_id,)
            )
        docs = cursor.fetchall()
    return docs


def get_status(expiry_date_str):
    expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    today = datetime.today().date()
    days_left = (expiry_date - today).days

    if days_left > 365:
        return days_left, "Safe", "blue", "🟦"
    elif days_left > 180:
        return days_left, "Good", "green", "🟢"
    elif days_left > 90:
        return days_left, "Warning", "orange", "🟠"
    elif days_left >= 0:
        return days_left, "Urgent", "red", "🔴"
    else:
        return days_left, "Expired", "black", "⚫"


def require_login():
    if not session.get("user_id"):
        return redirect(url_for("login"))


def get_owned_document(doc_id, user_id):
    """Fetch a document only if it belongs to the logged-in user."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?",
            (doc_id, user_id)
        )
        return cursor.fetchone()


@app.route("/")
def home():
    auth = require_login()
    if auth:
        return auth

    user_id = session["user_id"]
    search_query = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")

    docs = get_documents(user_id, search_query)
    documents = []

    for doc in docs:
        days_left, status, color, icon = get_status(doc["expiry_date"])

        if status_filter and status != status_filter:
            continue

        if days_left < 0:
            display_days = f"{abs(days_left)} days overdue"
        else:
            display_days = f"{days_left} days left"

        documents.append({
            "id": doc["id"],
            "title": doc["title"],
            "expiry_date": doc["expiry_date"],
            "display_days": display_days,
            "status": status,
            "color": color,
            "icon": icon,
            "has_file": bool(doc["file_path"])
        })

    return render_template("index.html", documents=documents)


@app.route("/add", methods=["GET", "POST"])
def add_document():
    auth = require_login()
    if auth:
        return auth

    error = None

    if request.method == "POST":
        title = request.form["title"].strip()
        expiry_date = request.form["expiry_date"]

        if not title or not expiry_date:
            error = "Please fill in all fields"
        else:
            try:
                datetime.strptime(expiry_date, "%Y-%m-%d")
            except ValueError:
                error = "Invalid date format"

        if not error:
            file_path = None
            file = request.files.get("document_file")
            if file and file.filename:
                if allowed_file(file.filename):
                    unique_name = f"{session['user_id']}_{int(datetime.utcnow().timestamp())}_{secure_filename(file.filename)}"
                    file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
                    file_path = unique_name
                else:
                    error = "That file type isn't allowed"

        if not error:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO documents (title, expiry_date, user_id, file_path) VALUES (?, ?, ?, ?)",
                    (title, expiry_date, session["user_id"], file_path)
                )
                conn.commit()
            return redirect("/")

    return render_template("add.html", error=error)


@app.route("/delete/<int:doc_id>")
def delete_document(doc_id):
    auth = require_login()
    if auth:
        return auth

    user_id = session["user_id"]
    doc = get_owned_document(doc_id, user_id)
    if not doc:
        abort(404)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (doc_id, user_id))
        conn.commit()

    if doc["file_path"]:
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], doc["file_path"])
        if os.path.exists(filepath):
            os.remove(filepath)

    return redirect("/")


@app.route("/edit/<int:doc_id>", methods=["GET", "POST"])
def edit_document(doc_id):
    auth = require_login()
    if auth:
        return auth

    user_id = session["user_id"]
    error = None
    doc = get_owned_document(doc_id, user_id)

    if not doc:
        abort(404)

    if request.method == "POST":
        title = request.form["title"].strip()
        expiry_date = request.form["expiry_date"]

        if not title or not expiry_date:
            error = "Please fill in all fields"
        else:
            try:
                datetime.strptime(expiry_date, "%Y-%m-%d")
            except ValueError:
                error = "Invalid date format"
            else:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE documents SET title = ?, expiry_date = ? WHERE id = ? AND user_id = ?",
                        (title, expiry_date, doc_id, user_id)
                    )
                    conn.commit()
                return redirect("/")

    return render_template("edit.html", doc=doc, error=error)


@app.route("/view/<int:doc_id>")
def view_file(doc_id):
    auth = require_login()
    if auth:
        return auth

    doc = get_owned_document(doc_id, session["user_id"])
    if not doc or not doc["file_path"]:
        abort(404)

    return send_from_directory(app.config["UPLOAD_FOLDER"], doc["file_path"])


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if not email or not password:
            error = "Please fill in all fields"
        elif len(password) < 8:
            error = "Password must be at least 8 characters"
        else:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
                if cursor.fetchone():
                    error = "An account with that email already exists"
                else:
                    password_hash = generate_password_hash(password)
                    cursor.execute(
                        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                        (email, password_hash)
                    )
                    conn.commit()
                    session["user_id"] = cursor.lastrowid
                    session["email"] = email
                    return redirect("/")

    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
            user = cursor.fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["email"] = user["email"]
            return redirect("/")
        else:
            error = "Invalid email or password"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


if __name__ == "__main__":
    app.run(debug=True)
