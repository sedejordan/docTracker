"""
app.py

Main Flask application for DocTracker.

Each user has their own account (see the users table). Every document
is tied to the user who created it via a user_id column, and every
database query that touches documents filters by
"WHERE user_id = <the logged-in user>" - that filter is what keeps
one user's documents private from everyone else.

Passwords are never stored as plain text - werkzeug.security scrambles
("hashes") them before saving, and checks a login attempt by hashing
the attempt and comparing hashes, not the raw passwords.

The file upload feature (letting someone attach the actual document,
not just a title) is temporarily disabled - see the comments below
marked "FILE UPLOAD FEATURE: DISABLED FOR MVP". It's commented out,
not deleted, so it can be switched back on once there's persistent
file storage in place (e.g. S3, Supabase Storage, or a Render disk).
"""

from datetime import datetime, timedelta
from flask import (
    Flask, render_template, request, redirect, session,
    url_for, abort
    # send_from_directory is only needed to serve uploaded files.
    # Re-add it to the import line above when file uploads are re-enabled.
)
from werkzeug.security import generate_password_hash, check_password_hash
# secure_filename is only needed for file uploads (sanitizes uploaded filenames).
# Re-add this import when file uploads are re-enabled:
# from werkzeug.utils import secure_filename
import os
import secrets
import smtplib
from email.message import EmailMessage
import psycopg2

from database import init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

# Render provides this automatically once a PostgreSQL database is
# created and linked to this web service. See README for local setup.
DATABASE_URL = os.environ.get("DATABASE_URL")

# Gmail SMTP sends the password reset emails. GMAIL_ADDRESS is the Gmail
# account sending the email. GMAIL_APP_PASSWORD is NOT your normal Gmail
# password - it's a 16-character app password generated under Google
# Account > Security > App Passwords (requires 2-Step Verification to be
# turned on first).
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

# How long a password reset link stays valid before the user has to
# request a new one.
RESET_TOKEN_LIFETIME = timedelta(hours=1)

# Creates the users/documents tables if they don't exist yet. This runs
# every time the app starts (e.g. every deploy), which is safe because
# CREATE TABLE IF NOT EXISTS does nothing if the tables are already there.
# This means we never need shell/terminal access to run database.py by
# hand - useful since Shell access is a paid Render feature.
try:
    init_db()
except Exception as e:
    print(f"Warning: could not initialize database tables on startup: {e}")

# --- FILE UPLOAD FEATURE: DISABLED FOR MVP ---
# Uploaded files were being stored on Render's local disk, which is wiped
# on every redeploy/restart. Re-enable this once we have persistent storage
# (Render Persistent Disk, S3, Supabase Storage, etc.).
# UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
# ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "docx", "doc", "txt"}
# os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def get_db():
    """Opens a new PostgreSQL connection. Callers are responsible for closing it."""
    return psycopg2.connect(DATABASE_URL)


def get_user_by_email(email):
    """Returns (id, email, password_hash) for this email, or None if no account exists."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, password_hash FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        cursor.close()
    finally:
        conn.close()
    return user


def get_user_by_reset_token(token):
    """
    Returns (id, email, reset_token_expiry) for a valid, unexpired reset
    token, or None if the token doesn't exist or has expired. Used by the
    reset-password page to check a link is still good before letting
    someone set a new password.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, email, reset_token_expiry FROM users WHERE reset_token = %s",
            (token,)
        )
        user = cursor.fetchone()
        cursor.close()
    finally:
        conn.close()

    if user is None:
        return None

    user_id, email, expiry = user
    if expiry is None or datetime.utcnow() > expiry:
        return None

    return user


def send_password_reset_email(to_email, reset_link):
    """
    Emails a password reset link via Gmail SMTP. Any failure (wrong app
    password, Gmail outage, etc.) is caught and logged rather than
    crashing the request - the caller shows the same generic "check your
    email" message either way, so we don't reveal whether sending worked.
    """
    message = EmailMessage()
    message["Subject"] = "Reset your DocTracker password"
    message["From"] = GMAIL_ADDRESS
    message["To"] = to_email
    message.set_content(
        f"We received a request to reset your DocTracker password.\n\n"
        f"Reset your password here: {reset_link}\n\n"
        f"This link expires in 1 hour. If you didn't request this, you can ignore this email."
    )
    message.add_alternative(
        f"""
            <p>We received a request to reset your DocTracker password.</p>
            <p><a href="{reset_link}">Click here to choose a new password</a></p>
            <p>This link expires in 1 hour. If you didn't request this, you can safely ignore this email.</p>
        """,
        subtype="html"
    )

    try:
        # Gmail's SMTP server, over an encrypted (SSL) connection on port 465.
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(message)
    except Exception as e:
        print(f"Warning: failed to send password reset email: {e}")


# --- FILE UPLOAD FEATURE: DISABLED FOR MVP (see note above) ---
# def allowed_file(filename):
#     return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_documents(user_id, search_query=""):
    """
    Returns every document belonging to user_id, optionally filtered
    by a title search. This WHERE user_id = %s filter is the core of
    account privacy - it's the only thing stopping one user from
    seeing another user's documents.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        if search_query:
            cursor.execute(
                "SELECT id, title, expiry_date FROM documents "
                "WHERE user_id = %s AND title ILIKE %s ORDER BY expiry_date ASC",
                (user_id, f"%{search_query}%")
            )
        else:
            cursor.execute(
                "SELECT id, title, expiry_date FROM documents "
                "WHERE user_id = %s ORDER BY expiry_date ASC",
                (user_id,)
            )
        docs = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    return docs


def get_status(expiry_date_str):
    """Works out how many days are left until expiry, and the label/color/icon to show."""
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
    """
    Call this at the top of any route that should only be visible to
    logged-in users. Returns a redirect to /login if nobody is logged
    in, or None if it's safe to continue.
    """
    if not session.get("user_id"):
        return redirect(url_for("login"))


def get_owned_document(doc_id, user_id):
    """
    Fetches a single document, but ONLY if it belongs to user_id.
    Every route that edits/deletes/views a specific document by ID
    must go through this function rather than a plain "SELECT ... WHERE
    id = ...", otherwise a user could edit/delete another user's
    document just by guessing or incrementing the ID in the URL.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, expiry_date, user_id FROM documents WHERE id = %s AND user_id = %s",
            (doc_id, user_id)
        )
        doc = cursor.fetchone()
        cursor.close()
    finally:
        conn.close()

    if doc is None:
        return None

    return {"id": doc[0], "title": doc[1], "expiry_date": doc[2], "user_id": doc[3]}


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

    for doc_id, title, expiry_date in docs:
        days_left, status, color, icon = get_status(expiry_date)

        if status_filter and status != status_filter:
            continue

        if days_left < 0:
            display_days = f"{abs(days_left)} days overdue"
        else:
            display_days = f"{days_left} days left"

        documents.append({
            "id": doc_id,
            "title": title,
            "expiry_date": expiry_date,
            "display_days": display_days,
            "status": status,
            "color": color,
            "icon": icon
            # "has_file" removed - file upload feature is disabled for MVP.
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

        # --- FILE UPLOAD FEATURE: DISABLED FOR MVP (see note near the top) ---
        # if not error:
        #     file_path = None
        #     file = request.files.get("document_file")
        #     if file and file.filename:
        #         if allowed_file(file.filename):
        #             unique_name = f"{session['user_id']}_{int(datetime.utcnow().timestamp())}_{secure_filename(file.filename)}"
        #             file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
        #             file_path = unique_name
        #         else:
        #             error = "That file type isn't allowed"

        if not error:
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO documents (title, expiry_date, user_id) VALUES (%s, %s, %s)",
                    (title, expiry_date, session["user_id"])
                )
                conn.commit()
                cursor.close()
            finally:
                conn.close()
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

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id))
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    # --- FILE UPLOAD FEATURE: DISABLED FOR MVP (see note near the top) ---
    # if doc["file_path"]:
    #     filepath = os.path.join(app.config["UPLOAD_FOLDER"], doc["file_path"])
    #     if os.path.exists(filepath):
    #         os.remove(filepath)

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
                conn = get_db()
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE documents SET title = %s, expiry_date = %s WHERE id = %s AND user_id = %s",
                        (title, expiry_date, doc_id, user_id)
                    )
                    conn.commit()
                    cursor.close()
                finally:
                    conn.close()
                return redirect("/")

    return render_template("edit.html", doc=doc, error=error)


# --- FILE UPLOAD FEATURE: DISABLED FOR MVP (see note near the top) ---
# @app.route("/view/<int:doc_id>")
# def view_file(doc_id):
#     auth = require_login()
#     if auth:
#         return auth
#
#     doc = get_owned_document(doc_id, session["user_id"])
#     if not doc or not doc["file_path"]:
#         abort(404)
#
#     return send_from_directory(app.config["UPLOAD_FOLDER"], doc["file_path"])


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
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
                if cursor.fetchone():
                    error = "An account with that email already exists"
                else:
                    password_hash = generate_password_hash(password)
                    # RETURNING id hands back the new row's ID in the same
                    # query - Postgres doesn't have SQLite's cursor.lastrowid.
                    cursor.execute(
                        "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                        (email, password_hash)
                    )
                    new_user_id = cursor.fetchone()[0]
                    conn.commit()
                    session["user_id"] = new_user_id
                    session["email"] = email
                cursor.close()
            finally:
                conn.close()

            if not error:
                return redirect("/")

    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, email, password_hash FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            cursor.close()
        finally:
            conn.close()

        if user and check_password_hash(user[2], password):
            session["user_id"] = user[0]
            session["email"] = user[1]
            return redirect("/")
        else:
            error = "Invalid email or password"

    return render_template("login.html", error=error)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None

    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = get_user_by_email(email)

        if user:
            user_id = user[0]
            token = secrets.token_urlsafe(32)
            expiry = datetime.utcnow() + RESET_TOKEN_LIFETIME

            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET reset_token = %s, reset_token_expiry = %s WHERE id = %s",
                    (token, expiry, user_id)
                )
                conn.commit()
                cursor.close()
            finally:
                conn.close()

            reset_link = url_for("reset_password", token=token, _external=True)
            send_password_reset_email(email, reset_link)

        # Same message whether or not the email is registered - this stops
        # this form being used to check which emails have an account here.
        message = "If an account exists for that email, we've sent a password reset link."

    return render_template("forgot_password.html", message=message)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = get_user_by_reset_token(token)

    if not user:
        return render_template("reset_password.html", invalid=True)

    user_id = user[0]
    error = None

    if request.method == "POST":
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if len(password) < 8:
            error = "Password must be at least 8 characters"
        elif password != confirm_password:
            error = "Passwords don't match"
        else:
            password_hash = generate_password_hash(password)
            conn = get_db()
            try:
                cursor = conn.cursor()
                # Clear the token so this link can't be reused after it's
                # already been used once to set a new password.
                cursor.execute(
                    "UPDATE users SET password_hash = %s, reset_token = NULL, reset_token_expiry = NULL WHERE id = %s",
                    (password_hash, user_id)
                )
                conn.commit()
                cursor.close()
            finally:
                conn.close()
            return redirect(url_for("login"))

    return render_template("reset_password.html", invalid=False, error=error)


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    auth = require_login()
    if auth:
        return auth

    error = None
    success = None

    if request.method == "POST":
        current_password = request.form["current_password"]
        new_password = request.form["new_password"]
        confirm_password = request.form["confirm_password"]

        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT password_hash FROM users WHERE id = %s", (session["user_id"],))
            current_hash = cursor.fetchone()[0]

            if not check_password_hash(current_hash, current_password):
                error = "Current password is incorrect"
            elif len(new_password) < 8:
                error = "New password must be at least 8 characters"
            elif new_password != confirm_password:
                error = "New passwords don't match"
            else:
                new_hash = generate_password_hash(new_password)
                cursor.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (new_hash, session["user_id"])
                )
                conn.commit()
                success = "Password updated."

            cursor.close()
        finally:
            conn.close()

    return render_template("change_password.html", error=error, success=success)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


if __name__ == "__main__":
    app.run(debug=True)
