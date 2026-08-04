"""
database.py

Sets up the database tables for Fritt Tracker.

This project used to use SQLite (a database stored in a single file on
disk). We've switched to PostgreSQL because SQLite's file gets wiped
every time Render redeploys or restarts the app - PostgreSQL runs as
its own persistent service, so the data survives deploys.

Connection details come from the DATABASE_URL environment variable,
which Render provides automatically once you create a PostgreSQL
database and link it to this web service. Locally, you'd set this
yourself (see the README for how to point it at a local Postgres
install, or just develop against the same Render database).
"""

import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db():
    """
    Opens a new connection to the PostgreSQL database.
    Every function that talks to the database calls this to get its
    own connection, then closes it when done (see app.py).
    """
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    Creates the users and documents tables if they don't already exist.
    Safe to run multiple times - CREATE TABLE IF NOT EXISTS is a no-op
    if the table is already there.
    """
    conn = get_db()
    cursor = conn.cursor()

    # SERIAL PRIMARY KEY is PostgreSQL's equivalent of SQLite's
    # "INTEGER PRIMARY KEY AUTOINCREMENT" - an auto-incrementing ID.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL
        );
    """)

    # Added for password reset: a one-time token emailed to the user, and
    # when it expires. ADD COLUMN IF NOT EXISTS is safe to run repeatedly -
    # it does nothing if the column is already there. This runs every time
    # the app starts (see app.py), so existing users tables get these new
    # columns automatically without needing manual/shell access.
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT;")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expiry TIMESTAMP;")

    # file_path column intentionally omitted - the file upload feature
    # is disabled for the MVP. See app.py for matching notes on where
    # to re-add it later.
    #
    # "user_id INTEGER NOT NULL REFERENCES users(id)" is PostgreSQL's way
    # of saying "this must match an existing id in the users table" -
    # the same idea as the FOREIGN KEY line used under SQLite.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id)
        );
    """)

        # Prevent duplicate documents per user (same title + expiry date)
    try:
        # First, check if there are any duplicates
        cursor.execute("""
            SELECT COUNT(*) FROM (
                SELECT user_id, title, expiry_date, COUNT(*) 
                FROM documents 
                GROUP BY user_id, title, expiry_date 
                HAVING COUNT(*) > 1
            ) AS duplicates;
        """)
        duplicate_count = cursor.fetchone()[0]
        
        if duplicate_count > 0:
            print(f"⚠️ Found {duplicate_count} duplicate document groups. Removing duplicates...")
            
            # Safer approach: Use a CTE to delete duplicates
            cursor.execute("""
                WITH duplicates AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY user_id, title, expiry_date 
                               ORDER BY id
                           ) as rn
                    FROM documents
                )
                DELETE FROM documents
                WHERE id IN (
                    SELECT id FROM duplicates WHERE rn > 1
                );
            """)
            print(f"✅ Removed {cursor.rowcount} duplicate documents.")
        
        # Now safe to add the constraint
        cursor.execute("""
            ALTER TABLE documents ADD CONSTRAINT unique_document_for_user 
            UNIQUE (user_id, title, expiry_date);
        """)
        print("✅ Added unique constraint for documents")
        
    except psycopg2.errors.DuplicateTable:
        # Constraint already exists - that's fine, nothing to do
        print("ℹ️ Unique constraint already exists, skipping")
    except Exception as e:
        print(f"⚠️ Could not add unique constraint: {e}")
        # If you want to see the actual error details
        # raise

    try:
        cursor.execute("""
            ALTER TABLE documents ADD COLUMN last_reminder_sent DATE;
            ALTER TABLE documents ADD COLUMN reminder_state TEXT; -- warning, urgent, critical, expired
            ALTER TABLE documents ADD COLUMN snoozed_until DATE;
        """)
    except Exception as e:
        print(f"Could not add columns last_reminder_sent, reminder_state, snoozed_until: {e}")


    conn.commit()
    cursor.close()
    conn.close()


if __name__ == "__main__":
    # Running "python database.py" directly sets up the tables.
    # Do this once, right after connecting the Postgres database on Render.
    init_db()
    print("Database tables are ready.")