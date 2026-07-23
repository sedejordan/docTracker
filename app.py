from datetime import datetime
from flask import Flask, render_template, request, redirect, session
import sqlite3

app = Flask(__name__)
app.secret_key = "secretkey"

APP_PASSWORD = "Password123"

def get_db():
    conn = sqlite3.connect("documents.db")
    conn.row_factory = sqlite3.Row
    return conn

def get_documents(search_query=""):
    with get_db() as conn:
        cursor = conn.cursor()

        if search_query:
            cursor.execute(
                "SELECT * FROM documents WHERE title LIKE ? ORDER BY expiry_date ASC",
                (f"%{search_query}%",)
            )
        else:
            cursor.execute(
                "SELECT * FROM documents ORDER BY expiry_date ASC"
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
    if not session.get("logged_in"):
        return redirect("/login")
    
@app.route("/")
def home():
    auth = require_login()
    if auth:
        return auth

    search_query = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")

    docs = get_documents(search_query)

    documents = []

    

    for doc in docs:
        doc_id = doc["id"]
        title = doc["title"]
        expiry_date = doc["expiry_date"]

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

        # Validation
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
                        "INSERT INTO documents (title, expiry_date) VALUES (?, ?)",
                        (title, expiry_date)
                    )

                    conn.commit()

                return redirect("/")

    return render_template("add.html", error=error)

@app.route("/delete/<int:doc_id>")
def delete_document(doc_id):
    auth = require_login()
    if auth:
        return auth

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

        conn.commit()

    return redirect("/")

@app.route("/edit/<int:doc_id>", methods=["GET", "POST"])
def edit_document(doc_id):
    auth = require_login()
    if auth:
        return auth

    error = None

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
        doc = cursor.fetchone()

        if not doc:
            return "Document not found"

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
                    cursor.execute(
                        "UPDATE documents SET title = ?, expiry_date = ? WHERE id = ?",
                        (title, expiry_date, doc_id)
                    )

                    conn.commit()
                    return redirect("/")
    return render_template("edit.html", doc=doc, error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        password = request.form["password"]

        if password == APP_PASSWORD:
            session["logged_in"] = True
            return redirect("/")
        else:
            error = "Wrong password"

    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

if __name__ == "__main__":
    app.run(debug=True)