import sqlite3

DB_PATH = "documents.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            file_path TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
    """)

    conn.commit()
    conn.close()


def migrate_existing_db():
    """
    One-time migration for a database created before accounts existed.
    Adds the new columns if they're missing, and assigns any orphaned
    documents (no user_id) to a placeholder account so nothing is lost.
    Safe to run multiple times - it's a no-op once everything is migrated.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(documents)")
    columns = [row[1] for row in cursor.fetchall()]

    if "user_id" not in columns:
        cursor.execute("ALTER TABLE documents ADD COLUMN user_id INTEGER")
    if "file_path" not in columns:
        cursor.execute("ALTER TABLE documents ADD COLUMN file_path TEXT")

    cursor.execute("SELECT COUNT(*) FROM documents WHERE user_id IS NULL")
    orphaned = cursor.fetchone()[0]

    if orphaned:
        cursor.execute("SELECT id FROM users WHERE email = ?", ("legacy@doctracker.local",))
        legacy_user = cursor.fetchone()

        if not legacy_user:
            from werkzeug.security import generate_password_hash
            import secrets
            temp_password = secrets.token_urlsafe(12)
            cursor.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("legacy@doctracker.local", generate_password_hash(temp_password))
            )
            legacy_user_id = cursor.lastrowid
            print("=" * 60)
            print(f"Created a placeholder account for {orphaned} existing document(s):")
            print("  email:    legacy@doctracker.local")
            print(f"  password: {temp_password}")
            print("Log in with this once to see your old documents, then")
            print("re-add them under your real account (or change this")
            print("account's email/password directly in the database).")
            print("=" * 60)
        else:
            legacy_user_id = legacy_user[0]

        cursor.execute(
            "UPDATE documents SET user_id = ? WHERE user_id IS NULL",
            (legacy_user_id,)
        )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    migrate_existing_db()
