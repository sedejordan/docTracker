import sqlite3

def init_db():
    conn = sqlite3.connect("documents.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            expiry_date TEXT NOT NULL
        );
    """)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()