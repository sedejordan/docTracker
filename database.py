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
from psycopg2 import pool
from contextlib import contextmanager

# Connection pool
db_pool = None

def init_pool():
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 20,  # min 1, max 20 connections
            dsn=os.environ.get("DATABASE_URL")
        )

@contextmanager
def get_connection():
    """Get a connection from the pool with context manager."""
    if db_pool is None:
        init_pool()
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db():
    """Get a database connection from the pool."""
    if db_pool is None:
        init_pool()
    return db_pool.getconn()


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
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token VARCHAR(255);")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token_expiry TIMESTAMP;")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verification_sent_at TIMESTAMP;")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_tier VARCHAR(50) DEFAULT 'free';")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(50) DEFAULT 'active';")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_expiry TIMESTAMP;")

    # cursor.execute("""
    #     -- Update users with more than 20 documents to Pro tier
    #     -- This prevents them from being locked out when the limit is enforced
    
    #     UPDATE users 
    #     SET subscription_tier = 'pro' 
    #     WHERE id IN (
    #         SELECT user_id 
    #         FROM documents 
    #         GROUP BY user_id 
    #         HAVING COUNT(*) > 20
    #     );
    # """)

    # cursor.execute("""
    #     -- Set subscription_status and expiry for migrated users
    #     UPDATE users 
    #     SET subscription_status = 'active',
    #     WHERE subscription_tier = 'pro' 
    #         AND subscription_status = 'free';  -- Only update if they were on free tier
    # """)

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

     # Now start a NEW transaction for adding columns
    try:
        cursor.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_reminder_sent DATE;
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS reminder_state TEXT;
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS snoozed_until DATE;
        """)
        conn.commit()
        print("✅ Added reminder columns to documents table")
    except Exception as e:
        print(f"⚠️ Could not add reminder columns: {e}")
        conn.rollback()

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
        conn.commit()

    except psycopg2.errors.DuplicateTable:
        # Constraint already exists - that's fine, nothing to do
        print("ℹ️ Unique constraint already exists, skipping")
        conn.commit()
    except Exception as e:
        print(f"⚠️ Could not add unique constraint: {e}")
        conn.rollback()
        # If you want to see the actual error details
        # raise

    cursor.execute("""
                CREATE TABLE IF NOT EXISTS newsletter_subscribers (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unsubscribed_at TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE);
            """)

    cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_verification_token ON users(verification_token);
                CREATE INDEX IF NOT EXISTS idx_users_email_verified ON users(email_verified);
                CREATE INDEX IF NOT EXISTS idx_newsletter_email ON newsletter_subscribers(email);
            """)

    cursor.close()
    conn.close()

def create_verification_token(user_id):
    """Create and store a verification token for a user."""
    token = secrets.token_urlsafe(32)
    expiry = datetime.utcnow() + timedelta(hours=24)  # 24 hours to verify
    
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET verification_token = %s, verification_token_expiry = %s, email_verification_sent_at = %s WHERE id = %s",
            (token, expiry, datetime.utcnow(), user_id)
        )
        conn.commit()
        cursor.close()
        return token
    finally:
        conn.close()

def verify_email_token(token):
    """Verify a user's email using their verification token."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, email FROM users WHERE verification_token = %s AND verification_token_expiry > %s AND email_verified = FALSE",
            (token, datetime.utcnow())
        )
        user = cursor.fetchone()
        
        if user:
            # Mark email as verified and clear token
            cursor.execute(
                "UPDATE users SET email_verified = TRUE, verification_token = NULL, verification_token_expiry = NULL WHERE id = %s",
                (user[0],)
            )
            conn.commit()
            cursor.close()
            return user
        cursor.close()
        return None
    finally:
        conn.close()

def is_email_verified(user_id):
    """Check if a user's email is verified."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT email_verified FROM users WHERE id = %s",
            (user_id,)
        )
        result = cursor.fetchone()
        cursor.close()
        return result[0] if result else False
    finally:
        conn.close()


if __name__ == "__main__":
    # Running "python database.py" directly sets up the tables.
    # Do this once, right after connecting the Postgres database on Render.
    init_db()
    print("Database tables are ready.")