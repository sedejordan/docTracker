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

# =============================================================================
# IMPORTS
# =============================================================================
import os
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool

# =============================================================================
# DATABASE CONNECTION POOL
# =============================================================================
# Connection pool manages database connections efficiently.
# Instead of opening/closing a connection for every request, we reuse
# connections from the pool. This is faster and prevents connection exhaustion.
#
# SimpleConnectionPool: min 1, max 5 connections.
# - min=1: Always keep at least one connection ready
# - max=5: Maximum 5 concurrent connections
# 
# If you have more than 5 concurrent users, they'll wait for a connection
# to become available. The free tier of Render typically runs 1-2 workers,
# so 5 connections is usually enough.

db_pool = None

def init_pool():
    """Initialize the database connection pool."""
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 5,  # min 1, max 5 connections
            dsn=os.environ.get("DATABASE_URL")
        )
        print(f"✅ Database connection pool initialized (max 5 connections)")

# =============================================================================
# CONNECTION MANAGEMENT
# =============================================================================

def get_db():
    """
    Get a connection from the pool.
    
    Usage:
        conn = get_db()
        try:
            cursor = conn.cursor()
            # ... do database work ...
        finally:
            put_db(conn)  # ALWAYS return the connection!
    
    If the pool is exhausted, it will close idle connections and retry.
    """
    if db_pool is None:
        init_pool()
    try:
        return db_pool.getconn()
    except Exception as e:
        print(f"Error getting DB connection: {e}")
        # If pool is exhausted, try to close idle connections and retry
        if "pool exhausted" in str(e):
            db_pool.closeall()
            init_pool()
            return db_pool.getconn()
        raise


def put_db(conn):
    """
    Return a connection to the pool.
    
    IMPORTANT: Always call this in a finally block to prevent
    connection leaks. A connection leak happens when you get a
    connection but never return it - eventually the pool runs out
    of connections and the app freezes.
    
    Example:
        conn = get_db()
        try:
            cursor = conn.cursor()
            # ... do work ...
        finally:
            put_db(conn)  # <-- ALWAYS do this
    """
    if db_pool is not None and conn is not None:
        db_pool.putconn(conn)


@contextmanager
def get_connection():
    """
    Get a connection from the pool with context manager.
    
    Usage:
        with get_connection() as conn:
            cursor = conn.cursor()
            # ... do database work ...
        # Connection automatically returned when context exits
    
    This is the recommended way to use connections - it guarantees
    the connection is always returned, even if an exception occurs.
    """
    if db_pool is None:
        init_pool()
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)


@contextmanager
def get_db_cursor():
    """
    Context manager for database connections with cursor.
    
    Usage:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM users")
            results = cursor.fetchall()
        # Connection is committed (or rolled back on error)
        # and returned to the pool automatically
    
    This is the most convenient way to use the database.
    It handles:
    - Getting a connection
    - Creating a cursor
    - Committing on success
    - Rolling back on error
    - Closing the cursor
    - Returning the connection
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        put_db(conn)


@contextmanager
def get_db_cursor_manual():
    """
    Context manager that returns BOTH connection and cursor.
    
    Usage:
        with get_db_cursor_manual() as (conn, cursor):
            cursor.execute("SELECT * FROM users")
            # You have access to conn for special operations
        # Connection automatically committed/rolled back and returned
    
    Use this when you need direct access to the connection object
    (e.g., for transactions that need to be committed at a specific time).
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        yield conn, cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        put_db(conn)

# =============================================================================
# DATABASE INITIALIZATION
# =============================================================================

def init_db():
    """
    Creates the database tables and indexes if they don't already exist.
    
    Safe to run multiple times - CREATE TABLE IF NOT EXISTS and
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS are no-ops if the table/column
    already exists.
    
    This runs every time the app starts (see app.py), which is safe and
    means we never need shell access to run migrations manually.
    
    Tables:
    - users: User accounts with authentication and subscription info
    - documents: User documents with expiry dates
    - newsletter_subscribers: Email subscribers
    
    Indexes:
    - On users.verification_token for fast token lookups
    - On users.email_verified for filtering unverified users
    - On newsletter_subscribers.email for quick lookups
    """
    conn = get_db()
    try:
        cursor = conn.cursor()

        # ---------------------------------------------------------------------
        # USERS TABLE
        # ---------------------------------------------------------------------
        # SERIAL PRIMARY KEY = auto-incrementing integer ID
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                email_verified BOOLEAN DEFAULT FALSE
            );
        """)

        # Add columns for password reset feature
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expiry TIMESTAMP;")

        # Add columns for email verification
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token VARCHAR(255);")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token_expiry TIMESTAMP;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verification_sent_at TIMESTAMP;")

        # Add columns for subscription management
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_tier VARCHAR(50) DEFAULT 'free';")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(50) DEFAULT 'active';")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_expiry TIMESTAMP;")

        # ---------------------------------------------------------------------
        # DOCUMENTS TABLE
        # ---------------------------------------------------------------------
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id)
            );
        """)

        # Add columns for reminder tracking
        cursor.execute("""
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_reminder_sent DATE;
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS reminder_state TEXT;
            ALTER TABLE documents ADD COLUMN IF NOT EXISTS snoozed_until DATE;
        """)
        print("✅ Added reminder columns to documents table")

        # ---------------------------------------------------------------------
        # UNIQUE CONSTRAINT - Prevent duplicate documents
        # ---------------------------------------------------------------------
        # A user shouldn't be able to add the same document (same title and
        # expiry date) twice. This constraint enforces that at the database level.
        #
        # First, clean up any existing duplicates before adding the constraint.
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
            
            # Keep only the lowest ID (oldest) for each duplicate group
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
        
        # Now add the unique constraint
        try:
            cursor.execute("""
                ALTER TABLE documents ADD CONSTRAINT unique_document_for_user 
                UNIQUE (user_id, title, expiry_date);
            """)
            print("✅ Added unique constraint for documents")
        except psycopg2.errors.DuplicateTable:
            # Constraint already exists - that's fine
            print("ℹ️ Unique constraint already exists, skipping")
        except Exception as e:
            print(f"⚠️ Could not add unique constraint: {e}")

        # ---------------------------------------------------------------------
        # NEWSLETTER SUBSCRIBERS TABLE
        # ---------------------------------------------------------------------
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS newsletter_subscribers (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                unsubscribed_at TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );
        """)

        # ---------------------------------------------------------------------
        # INDEXES - Speed up common queries
        # ---------------------------------------------------------------------
        # Indexes make SELECT queries faster by allowing the database to
        # find rows without scanning the entire table.
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_verification_token ON users(verification_token);
            CREATE INDEX IF NOT EXISTS idx_users_email_verified ON users(email_verified);
            CREATE INDEX IF NOT EXISTS idx_newsletter_email ON newsletter_subscribers(email);
        """)
        print("✅ Created database indexes")

        cursor.close()
        conn.commit()
        print("✅ Database tables initialized successfully")
        
    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        conn.rollback()
        raise
    finally:
        put_db(conn)  # Always return connection

# =============================================================================
# USER VERIFICATION FUNCTIONS
# =============================================================================

def create_verification_token(user_id):
    """
    Create and store a verification token for a user.
    
    Args:
        user_id: The user's ID
        
    Returns:
        The generated verification token (URL-safe string)
    
    The token is used in the email verification link:
    https://tracker.fritt.org/verify-email/{token}
    
    Tokens expire after 24 hours.
    """
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE users 
               SET verification_token = %s, 
                   verification_token_expiry = %s, 
                   email_verification_sent_at = %s 
               WHERE id = %s""",
            (token, expiry, datetime.now(timezone.utc), user_id)
        )
        conn.commit()
        cursor.close()
        return token
    finally:
        put_db(conn)


def verify_email_token(token):
    """
    Verify a user's email using their verification token.
    
    Args:
        token: The verification token from the email link
        
    Returns:
        (user_id, email) if token is valid and not expired, else None
    
    This function:
    1. Finds the user with the matching token
    2. Checks that the token hasn't expired
    3. Marks the user as verified
    4. Clears the token so it can't be reused
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, email 
               FROM users 
               WHERE verification_token = %s 
                 AND verification_token_expiry > %s 
                 AND email_verified = FALSE""",
            (token, datetime.now(timezone.utc))
        )
        user = cursor.fetchone()
        
        if user:
            cursor.execute(
                """UPDATE users 
                   SET email_verified = TRUE, 
                       verification_token = NULL, 
                       verification_token_expiry = NULL 
                   WHERE id = %s""",
                (user[0],)
            )
            conn.commit()
            cursor.close()
            return user
        
        cursor.close()
        return None
    finally:
        put_db(conn)


def is_email_verified(user_id):
    """
    Check if a user's email is verified.
    
    Args:
        user_id: The user's ID
        
    Returns:
        True if verified, False otherwise
    """
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
        put_db(conn)

# =============================================================================
# SCRIPT ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    """
    Running "python database.py" directly sets up the tables.
    Do this once, right after connecting the Postgres database on Render.
    
    In production, init_db() is also called on app startup in app.py,
    so you don't need to run this manually unless you're setting up a
    new environment.
    """
    print("🚀 Initializing database tables...")
    init_db()
    print("✅ Database tables are ready.")