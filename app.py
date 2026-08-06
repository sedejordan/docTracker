"""
app.py

Main Flask application for Fritt Tracker.

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

from datetime import datetime, timedelta, timezone
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
import requests
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import flash
import csv
import io
import json
from functools import lru_cache
import time
from rave_python import Rave
import hashlib
import hmac

from database import init_db, get_db, put_db

# CHANGE!!!
# A secret token (store in environment variables as TRIGGER_SECRET)
# When you create the cron job, you'll pass this in the URL or a header
TRIGGER_SECRET = os.environ.get("TRIGGER_SECRET", "")

# --- ERROR MONITORING (Sentry) ---
# Optional - only enabled if SENTRY_DSN environment variable is set.
# Sentry has a free tier (5,000 errors/month).
SENTRY_DSN = os.environ.get("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.1,
        environment=os.environ.get("FLASK_ENV", "production"),
    )
    print("✅ Sentry error monitoring enabled")
else:
    print("ℹ️ Sentry not configured - set SENTRY_DSN to enable error monitoring")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

app.config['SERVER_NAME'] = os.environ.get("APP_URL", "tracker.fritt.org")

# --- SECURE COOKIE SETTINGS ---
# SESSION_COOKIE_SECURE: only send the login cookie over HTTPS, never plain
# HTTP. Render serves your site over HTTPS by default, so this is safe -
# but it does mean sessions/login won't work if you ever run this locally
# over plain http://localhost without HTTPS.
# SESSION_COOKIE_HTTPONLY: stops JavaScript from reading the cookie, so a
# malicious script (e.g. from a compromised third-party library) can't
# steal a logged-in user's session.
# SESSION_COOKIE_SAMESITE="Lax": stops the cookie being sent along with
# requests that originate from other websites, which is most of what
# CSRF protection (below) defends against.
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# --- CSRF PROTECTION ---
# Cross-Site Request Forgery: without this, a malicious website could embed
# a hidden form that submits to e.g. /delete/3 on YOUR site, and if a
# logged-in user visits that malicious page, the browser happily attaches
# their Fritt Tracker login cookie and the delete goes through - without them
# ever intending it. CSRFProtect requires every POST form to include a
# secret, single-use token (added via {{ csrf_token() }} in each template)
# that a third-party site has no way of knowing, so forged requests get
# rejected automatically.
csrf = CSRFProtect(app)

# --- RATE LIMITING ---
# Without this, nothing stops someone from scripting thousands of login
# attempts per second to brute-force a password, or hammering
# /forgot-password to spam someone's inbox. get_remote_address limits by
# IP address. Storage is in-memory, which is fine as long as this app runs
# as a single worker process (Render's free tier defaults to
# WEB_CONCURRENCY=1) - with multiple workers/instances, each would track
# its own separate counts, so you'd want a shared store like Redis instead.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    # Flask-Limiter decides whether it's enabled right here, at
    # construction time - NOT dynamically per request. Setting
    # app.config["RATELIMIT_ENABLED"] later (e.g. in tests) has no effect,
    # since by then this has already been locked in. DISABLE_RATE_LIMITING
    # lets tests turn this off before app.py is even imported - see
    # tests/conftest.py.
    enabled=os.environ.get("DISABLE_RATE_LIMITING", "false").lower() != "true"
)

# Initialize Flutterwave
rave = Rave(
    os.getenv("FLW_PUBLIC_KEY"),
    os.getenv("FLW_SECRET_KEY")
)

# Subscription tiers
SUBSCRIPTION_TIERS = {
    'free': {
        'name': 'Free',
        'doc_limit': 20,
        'price_monthly': 0,
        'price_yearly': 0
    },
    'pro': {
        'name': 'Pro',
        'doc_limit': 100,
        'price_monthly': 4.99,
        'price_yearly': 49.99
    },
    'business': {
        'name': 'Business',
        'doc_limit': 0,
        'price_monthly': 14.99,
        'price_yearly': 149.99
    }
}

# Simple in-memory cache for subscription status
_subscription_cache = {}
_cache_ttl = 60  # 60 seconds

# Get webhook secret from environment variables
FLW_WEBHOOK_SECRET = os.environ.get("FLW_WEBHOOK_SECRET", "")

def get_cached_subscription_status(user_id):
    """Get subscription status with simple caching."""
    cache_key = f"sub_{user_id}"
    now = datetime.now(timezone.utc)
    
    # Check if cached and not expired
    if cache_key in _subscription_cache:
        cached_data, cache_time = _subscription_cache[cache_key]
        if (now - cache_time).total_seconds() < _cache_ttl:
            return cached_data
    
    # Get fresh data
    result = get_subscription_status(user_id)
    _subscription_cache[cache_key] = (result, now)
    return result

def get_user_subscription(user_id):
    """Get user's current subscription tier."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT subscription_tier FROM users WHERE id = %s",
            (user_id,)
        )
        result = cursor.fetchone()
        cursor.close()
        return result[0] if result else 'free'
    finally:
        put_db(conn)

def can_add_document(user_id):
    """Check if user can add more documents based on subscription."""
    sub_status = get_subscription_status(user_id)
    
    # If Pro expired, trigger cleanup
    if sub_status['tier'] == 'pro' and not sub_status['is_active']:
        # Trim documents and revert to free
        deleted_count = trim_documents_to_free_limit(user_id)
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET subscription_tier = 'free', 
                    subscription_status = 'expired',
                    subscription_expiry = NULL
                WHERE id = %s
            """, (user_id,))
            conn.commit()
            cursor.close()
        finally:
            put_db(conn)
        # User is now on free tier
        tier = 'free'
    else:
        tier = sub_status['tier']
    
    # VIP has unlimited documents
    if tier == 'vip':
        return True
    
    # Get the limit for this tier
    limit = SUBSCRIPTION_TIERS.get(tier, {}).get('doc_limit', 20)
    
    # Count current documents
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM documents WHERE user_id = %s",
            (user_id,)
        )
        count = cursor.fetchone()[0]
        cursor.close()
        return count < limit
    finally:
        put_db(conn)

def trim_documents_to_free_limit(user_id):
    """
    When a user's Pro subscription expires, keep only the 20 documents
    farthest from expiry (i.e., the ones with the furthest expiry dates)
    and delete the rest.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        
        # Get all document IDs for this user, ordered by expiry_date DESC (farthest first)
        cursor.execute("""
            SELECT id 
            FROM documents 
            WHERE user_id = %s 
            ORDER BY expiry_date DESC
        """, (user_id,))
        all_docs = [row[0] for row in cursor.fetchall()]
        
        # If user has 20 or fewer documents, nothing to do
        if len(all_docs) <= 20:
            return 0
        
        # Keep the first 20 (farthest expiry), delete the rest
        docs_to_keep = all_docs[:20]
        docs_to_delete = all_docs[20:]
        
        if docs_to_delete:
            # Delete documents that are closest to expiry
            placeholders = ','.join(['%s'] * len(docs_to_delete))
            cursor.execute(
                f"DELETE FROM documents WHERE id IN ({placeholders}) AND user_id = %s",
                docs_to_delete + [user_id]
            )
            deleted_count = cursor.rowcount
            conn.commit()
            
            return deleted_count
        
        return 0
    finally:
        put_db(conn)

def get_document_count(user_id):
    """Get the number of documents a user has."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM documents WHERE user_id = %s",
            (user_id,)
        )
        count = cursor.fetchone()[0]
        cursor.close()
        return count
    finally:
        put_db(conn)  # IMPORTANT: Always return connection

# Render provides this automatically once a PostgreSQL database is
# created and linked to this web service. See README for local setup.
DATABASE_URL = os.environ.get("DATABASE_URL")

# Resend sends the password reset emails over its HTTPS API. We use this
# instead of SMTP because Render blocks outbound SMTP ports (465/587) on
# its free tier - HTTPS (port 443) isn't blocked, so an API-based email
# provider is the reliable option here.
# RESEND_API_KEY comes from resend.com (API Keys section).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "auth@fritt.org")

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

# Run migration for existing users
try:
    from migrate_subscriptions import run_subscription_migration
    run_subscription_migration()
except Exception as e:
    print(f"Note: Subscription migration not run: {e}")

# --- FILE UPLOAD FEATURE: DISABLED FOR MVP ---
# Uploaded files were being stored on Render's local disk, which is wiped
# on every redeploy/restart. Re-enable this once we have persistent storage
# (Render Persistent Disk, S3, Supabase Storage, etc.).
# UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
# ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "docx", "doc", "txt"}
# os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def get_user_by_email(email):
    """Returns (id, email, password_hash, email_verified) for this email, or None if no account exists."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, password_hash, email_verified FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        cursor.close()

         # Ensure we return a tuple with 4 elements if user exists
        if user:
            # User is already a tuple with 4 elements from the query
            return user
        return None
    finally:
        put_db(conn)

def get_utc_now():
    """Helper function to get timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)

def get_user_by_reset_token(token):
    """
    Returns (id, email, reset_token_expiry) for a valid, unexpired reset
    token, or None if the token doesn't exist or has expired.
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
        put_db(conn)

    if user is None:
        return None

    user_id, email, expiry = user
    
    # Handle both naive and aware datetimes
    if expiry is None:
        return None
    
    # If expiry is naive (no timezone), make it aware
    if expiry.tzinfo is None:
        # Assume the naive datetime is UTC
        expiry = expiry.replace(tzinfo=timezone.utc)
    
    # Now compare with timezone-aware current time
    if get_utc_now() > expiry:
        return None

    return user

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
        
        if result:
            return result[0]  # Returns True/False
        return False
    finally:
        put_db(conn)

def get_subscription_status(user_id):
    """Get user's subscription status and expiry."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT subscription_tier, subscription_status, subscription_expiry FROM users WHERE id = %s",
            (user_id,)
        )
        result = cursor.fetchone()
        cursor.close()
        
        if result:
            tier, status, expiry = result
            # Make expiry timezone-aware if it's naive
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            # Check if subscription is active (status='active' and not expired)
            is_active = True
            if tier != 'free' and expiry is not None:
                is_active = expiry > datetime.now(timezone.utc)
            elif tier == 'free':
                is_active = True  # Free tier never expires
            else:
                is_active = status == 'active'
            
            return {
                'tier': tier,
                'status': status,
                'expiry': expiry,
                'is_active': is_active
            }
        return {'tier': 'free', 'status': 'active', 'expiry': None, 'is_active': True}
    finally:
        put_db(conn)  # IMPORTANT: Always return connection

def send_welcome_email(user_email, user_id):
    """
    Send a welcome email to a newly verified user.
    """
    try:
        # Get user's name or use a generic greeting
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
            result = cursor.fetchone()
            cursor.close()
            user_name = result[0].split('@')[0] if result else "there"
        finally:
            put_db(conn)
        
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [user_email],
                "subject": "Welcome to Fritt Tracker! 🎉",
                "html": f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <style>
                            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                            .header {{ background: #2563eb; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
                            .content {{ padding: 30px; background: #f9fafb; }}
                            .button {{ background: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; }}
                            .footer {{ text-align: center; padding: 20px; color: #6b7280; font-size: 14px; }}
                            .tip {{ background: white; padding: 15px; border-radius: 8px; margin: 10px 0; }}
                        </style>
                    </head>
                    <body>
                        <div class="container">
                            <div class="header">
                                <h1>Welcome to Fritt Tracker, {user_name}! 👋</h1>
                            </div>
                            <div class="content">
                                <p>Your email has been verified successfully! You're all set to start tracking your important documents.</p>
                                
                                <div class="tip">
                                    <h3>🚀 Quick Start Guide</h3>
                                    <ul>
                                        <li>📄 <strong>Add your documents</strong> - Click the "Add Document" button to start tracking</li>
                                        <li>🔔 <strong>Get reminders</strong> - We'll email you before documents expire</li>
                                        <li>📊 <strong>Track everything</strong> - See all your documents in one dashboard</li>
                                    </ul>
                                </div>
                                
                                <div class="tip">
                                    <h3>💡 Pro Tips</h3>
                                    <ul>
                                        <li>Import multiple documents at once using <strong>CSV import</strong></li>
                                        <li>Set <strong>realistic expiry dates</strong> to get timely reminders</li>
                                        <li>Renew documents with one click when they expire</li>
                                    </ul>
                                </div>
                                
                                <p style="text-align: center; margin: 30px 0;">
                                    <a href="https://tracker.fritt.org/" class="button">Go to Your Dashboard →</a>
                                </p>
                                
                                <p style="color: #6b7280; font-size: 14px;">You're on the <strong>Free plan</strong> which includes up to 10 documents. Upgrade anytime for unlimited tracking.</p>
                            </div>
                            <div class="footer">
                                <p>Need help? Reply to this email - we're here to help!</p>
                                <p style="font-size: 12px;">
                                    <a href="https://tracker.fritt.org/terms" style="color: #6b7280;">Terms</a> • 
                                    <a href="https://tracker.fritt.org/privacy" style="color: #6b7280;">Privacy</a>
                                </p>
                            </div>
                        </div>
                    </body>
                    </html>
                """,
                "text": f"""
                    Welcome to Fritt Tracker, {user_name}!
                    
                    Your email has been verified successfully! You're all set to start tracking your important documents.
                    
                    Quick Start Guide:
                    - Add your documents - Click "Add Document" to start tracking
                    - Get reminders - We'll email you before documents expire
                    - Track everything - See all your documents in one dashboard
                    
                    Pro Tips:
                    - Import multiple documents at once using CSV import
                    - Set realistic expiry dates to get timely reminders
                    - Renew documents with one click when they expire
                    
                    Go to Your Dashboard: https://tracker.fritt.org/
                    
                    You're on the Free plan which includes up to 10 documents. Upgrade anytime for unlimited tracking.
                    
                    Need help? Reply to this email - we're here to help!
                    
                    ---
                    Fritt Tracker - Keep track of your important documents
                    https://tracker.fritt.org
                """
            },
            timeout=10
        )
        
        if response.status_code >= 400:
            print(f"Warning: Resend error sending welcome email: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"Error sending welcome email: {e}")
        return False

def send_password_reset_email(user_email, reset_link):
    try:
        # Force HTTPS in the reset link
        if reset_link.startswith('http://'):
            reset_link = reset_link.replace('http://', 'https://')
        
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [user_email],
                "subject": "Reset your Fritt Tracker password",
                "html": f"""
                    <p>We received a request to reset your Fritt Tracker password.</p>
                    <p><a href="{reset_link}">Click here to choose a new password</a></p>
                    <p>This link expires in 1 hour. If you didn't request this, you can safely ignore this email.</p>
                    <p style="font-size: 14px; color: #6b7280;">Or copy this link: {reset_link}</p>
                """,
                "text": f"""
                    We received a request to reset your Fritt Tracker password.
                    
                    Click here to choose a new password: {reset_link}
                    
                    This link expires in 1 hour.
                    
                    If you didn't request this, you can safely ignore this email.
                """
            },
            timeout=10
        )
        if response.status_code >= 400:
            print(f"Warning: Resend returned an error sending password reset email: {response.text}")
    except Exception as e:
        print(f"Warning: failed to send password reset email: {e}")

def send_verification_email(user_email, user_id):
    """
    Send email verification link to a new user.
    """
    try:
        # Create verification token
        token = secrets.token_urlsafe(32)
        # IMPORTANT: Store as timezone-aware UTC
        expiry = datetime.now(timezone.utc) + timedelta(hours=24)
        
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET verification_token = %s, verification_token_expiry = %s, email_verification_sent_at = %s WHERE id = %s",
                (token, expiry, datetime.now(timezone.utc), user_id)
            )
            conn.commit()
            cursor.close()
        finally:
            put_db(conn)
        
        # Force HTTPS in the verification link
        base_url = os.environ.get("APP_URL", "tracker.fritt.org")
        verification_link = f"https://{base_url}{url_for('verify_email', token=token)}"
        
        # Send via Resend
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [user_email],
                "subject": "Verify your email address for Fritt Tracker",
                "html": f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <style>
                            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                            .header {{ background: #2563eb; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
                            .content {{ padding: 30px; background: #f9fafb; }}
                            .button {{ background: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; }}
                            .footer {{ text-align: center; padding: 20px; color: #6b7280; font-size: 14px; }}
                        </style>
                    </head>
                    <body>
                        <div class="container">
                            <div class="header">
                                <h1>Welcome to Fritt Tracker! 🎉</h1>
                            </div>
                            <div class="content">
                                <p>Thanks for creating an account. Please verify your email address to get started.</p>
                                <p style="text-align: center; margin: 30px 0;">
                                    <a href="{verification_link}" class="button" style="background: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block;">Verify Email Address</a>
                                </p>
                                <p style="text-align: center; margin: 30px 0;">
                                    Or copy and paste this link into your browser:
                                    <br>
                                    <span style="word-break: break-all; color: #2563eb; font-size: 14px;">{verification_link}</span>
                                </p>
                                <p>This link expires in <strong>24 hours</strong>.</p>
                                <p style="color: #6b7280; font-size: 14px;">If you didn't create an account, you can safely ignore this email.</p>
                            </div>
                            <div class="footer">
                                <p>Fritt Tracker helps you keep track of your important documents and their expiry dates.</p>
                            </div>
                        </div>
                    </body>
                    </html>
                """,
                "text": f"""
                    Welcome to Fritt Tracker!
                    
                    Thanks for creating an account. Please verify your email address to get started.
                    
                    Verify your email by visiting this link:
                    {verification_link}
                    
                    This link expires in 24 hours.
                    
                    If you didn't create an account, you can safely ignore this email.
                    
                    ---
                    Fritt Tracker helps you keep track of your important documents and their expiry dates.
                    Visit us at: https://tracker.fritt.org
                """
            },
            timeout=10
        )
        
        if response.status_code >= 400:
            print(f"Warning: Resend error sending verification email: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"Warning: failed to send verification email: {e}")
        return False

def send_subscription_expiry_email(user_email, user_name=None):
    """Send email notification when subscription expires."""
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [user_email],
                "subject": "Your Fritt Tracker Subscription Has Expired",
                "html": f"""
                    <h2>Your Subscription Has Expired</h2>
                    <p>Your Fritt Tracker subscription has expired.</p>
                    <p>You've been moved back to the Free plan with a 20-document limit.</p>
                    <p>If you have more than 20 documents, we've kept your 20 most important ones.</p>
                    <p><a href="https://tracker.fritt.org/pricing">Renew your subscription →</a></p>
                """,
                "text": f"""
                    Your Fritt Tracker subscription has expired.
                    
                    You've been moved back to the Free plan with a 20-document limit.
                    
                    If you have more than 20 documents, we've kept your 20 most important ones.
                    
                    Renew your subscription: https://tracker.fritt.org/pricing
                """
            },
            timeout=10
        )
        return response.status_code < 400
    except Exception as e:
        print(f"Error sending expiry email: {e}")
        return False
    
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
        put_db(conn)
    return docs


def get_status(expiry_date_str):
    """Works out how many days are left until expiry, and the label/color/icon to show."""
    expiry_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    today = datetime.today().date()
    days_left = (expiry_date - today).days

    if days_left >= 120:
        return days_left, "Safe", "blue", "🟦"
    elif days_left >= 60:
        return days_left, "Good", "green", "🟢"
    elif days_left >= 15:
        return days_left, "Warning", "orange", "🟠"
    elif days_left >= 0:
        return days_left, "Urgent", "red", "🔴"
    else:
        return days_left, "Expired", "black", "⚫"


def require_verified():
    """
    Check if user is logged in AND their email is verified.
    """
    if not session.get("user_id"):
        return redirect(url_for("login"))
    
    user_id = session["user_id"]
    if not is_email_verified(user_id):
        flash("⚠️ Please verify your email address to access all features.", "warning")
        return redirect(url_for("resend_verification"))
    
    return None


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
        put_db(conn)

    if doc is None:
        return None

    return {"id": doc[0], "title": doc[1], "expiry_date": doc[2], "user_id": doc[3]}

# Get user region 
def get_user_region():
    """
    Detect user's region based on IP address.
    Uses a free IP geolocation API.
    """
    try:
        # Get user's IP address
        if request.headers.get('X-Forwarded-For'):
            ip = request.headers.get('X-Forwarded-For').split(',')[0]
        else:
            ip = request.remote_addr
        
        # Skip for localhost (testing)
        if ip in ['127.0.0.1', 'localhost']:
            return 'us'  # Default to US for testing
        
        # Use free API to get country
        response = requests.get(f'http://ip-api.com/json/{ip}', timeout=3)
        if response.status_code == 200:
            data = response.json()
            country_code = data.get('countryCode', '').upper()
            
            if country_code == 'NG':
                return 'ng'
            elif country_code == 'GB' or country_code == 'UK':
                return 'uk'
            else:
                return 'us'  # Default for everyone else
    except Exception as e:
        print(f"Warning: Could not detect region: {e}")
        return 'us'  # Default to US
    
    return 'us'  # Fallback

def validate_password_strength(password):
    """Check if password meets complexity requirements."""
    errors = []
    
    if len(password) < 8:
        errors.append("At least 8 characters")
    if not any(c.isupper() for c in password):
        errors.append("An uppercase letter")
    if not any(c.islower() for c in password):
        errors.append("A lowercase letter")
    if not any(c.isdigit() for c in password):
        errors.append("A number")
    if not any(c in "!@#$%^&*()_+-=[]{}|;:',.<>?/~`" for c in password):
        errors.append("A symbol")
    
    return errors

# Get user pricing
def get_pricing(region='us'):
    """Return pricing based on region."""
    pricing = {
        'ng': {
            'currency': '₦',
            'monthly': '2,500',
            'yearly': '25,000',
            'monthly_raw': 2500,
            'yearly_raw': 25000,
            'vip_monthly': '7,500',
            'vip_yearly': '75,000',
            'vip_monthly_raw': 7500,
            'vip_yearly_raw': 75000,
            'region_name': 'Nigeria'
        },
        'uk': {
            'currency': '£',
            'monthly': '3.99',
            'yearly': '39.99',
            'monthly_raw': 3.99,
            'yearly_raw': 39.99,
            'vip_monthly': '11.99',
            'vip_yearly': '119.99',
            'vip_monthly_raw': 11.99,
            'vip_yearly_raw': 119.99,
            'region_name': 'United Kingdom'
        },
        'us': {
            'currency': '$',
            'monthly': '4.99',
            'yearly': '49.99',
            'monthly_raw': 4.99,
            'yearly_raw': 49.99,
            'vip_monthly': '14.99',
            'vip_yearly': '149.99',
            'vip_monthly_raw': 14.99,
            'vip_yearly_raw': 149.99,
            'region_name': 'Worldwide'
        }
    }
    return pricing.get(region, pricing['us'])

def log_slow_query(query, params=None, threshold=0.1):
    """Log queries that take longer than threshold seconds."""
    start = time.time()
    # Execute query...
    duration = time.time() - start
    if duration > threshold:
        print(f"SLOW QUERY ({duration:.3f}s): {query[:100]}...")

# Get the plan ID based on plan type and currency
def get_plan_id(plan_type, currency):
    """Get the appropriate plan ID based on plan type and currency."""
    # Map plan type and currency to environment variable
    plan_map = {
        'pro_monthly': f"FLW_PRO_MONTHLY_{currency}_PLAN",
        'pro_yearly': f"FLW_PRO_YEARLY_{currency}_PLAN",
        'vip_monthly': f"FLW_VIP_MONTHLY_{currency}_PLAN",
        'vip_yearly': f"FLW_VIP_YEARLY_{currency}_PLAN",
    }
    
    var_name = plan_map.get(plan_type)
    if var_name:
        return os.getenv(var_name)
    return None

def get_currency_for_region(region):
    """Map region to currency code."""
    currency_map = {
        'ng': 'NGN',
        'uk': 'GBP',
        'us': 'USD'
    }
    return currency_map.get(region, 'USD')

def update_user_to_free(user_id):
    """
    Update a user's subscription to free tier.
    Called when subscription is cancelled or expires.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        
        # First, get the user's current subscription info
        cursor.execute(
            "SELECT subscription_tier, subscription_status FROM users WHERE id = %s",
            (user_id,)
        )
        result = cursor.fetchone()
        
        if not result:
            print(f"User {user_id} not found")
            return False
        
        current_tier, current_status = result
        
        # Only update if user is on a paid plan
        if current_tier in ['pro', 'vip']:
            # Check if user has more than 20 documents
            cursor.execute(
                "SELECT COUNT(*) FROM documents WHERE user_id = %s",
                (user_id,)
            )
            doc_count = cursor.fetchone()[0]
            
            if doc_count > 20:
                # User has more than 20 docs - trim them
                trim_documents_to_free_limit(user_id)
                print(f"Trimmed documents for user {user_id} (had {doc_count} docs)")
            
            # Update to free tier
            cursor.execute("""
                UPDATE users 
                SET subscription_tier = 'free',
                    subscription_status = 'expired',
                    subscription_expiry = NULL
                WHERE id = %s
            """, (user_id,))
            conn.commit()
            
            print(f"✅ User {user_id} downgraded to Free tier (was {current_tier})")
            return True
        else:
            print(f"User {user_id} is already on Free tier")
            return False
            
    except Exception as e:
        print(f"Error updating user {user_id} to free: {e}")
        conn.rollback()
        return False
    finally:
        put_db(conn)

def verify_flutterwave_webhook(data, signature):
    """
    Verify webhook signature for v3.
    """
    if not FLW_WEBHOOK_SECRET:
        print("⚠️ FLW_WEBHOOK_SECRET not set - webhook verification disabled")
        return True
    
    if not signature:
        print("❌ No signature provided")
        return False
    
    # Compute expected signature
    expected_signature = hmac.new(
        FLW_WEBHOOK_SECRET.encode('utf-8'),
        json.dumps(data).encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected_signature, signature)


# --- CUSTOM ERROR PAGES ---
# These replace Flask's default debug/error pages with branded versions
# that match the Fritt Tracker design system. Users see these instead of
# raw Flask output when something goes wrong.

@app.errorhandler(404)
def page_not_found(e):
    """Page not found - user followed a broken link or typed a wrong URL."""
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    """Internal server error - something unexpected broke in the app."""
    return render_template('errors/500.html'), 500

@app.errorhandler(403)
def forbidden(e):
    """Forbidden - user tried to access something they shouldn't (e.g., another user's document)."""
    return render_template('errors/403.html'), 403

@app.errorhandler(405)
def method_not_allowed(e):
    """Method not allowed - e.g., GET instead of POST."""
    return render_template('errors/405.html'), 405

@app.route("/")
def home():
    """Show landing page for non-logged-in users, dashboard for logged-in users."""
    # Check if user is logged in
    if not session.get("user_id"):
        # Not logged in → show landing page with region-specific pricing
        region = get_user_region()
        pricing = get_pricing(region)
        return render_template("landing.html", pricing=pricing)
        pass

    user_id = session["user_id"]

    # Check if Pro subscription expired
    sub_status = get_subscription_status(user_id)
    if sub_status['tier'] == 'pro' and not sub_status['is_active']:
        # Pro expired - trim documents and revert to free
        deleted_count = trim_documents_to_free_limit(user_id)
        
        # Update user to free tier
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET subscription_tier = 'free', 
                    subscription_status = 'expired',
                    subscription_expiry = NULL
                WHERE id = %s
            """, (user_id,))
            conn.commit()
            cursor.close()
        finally:
            put_db(conn)
        
        if deleted_count > 0:
            flash(
                f"⚠️ Your Pro trial has expired. We've kept your 20 most important documents "
                f"(farthest from expiry) and removed {deleted_count} documents. "
                f"Upgrade to Pro to track more than 20 documents.",
                "warning"
            )
        else:
            flash(
                "⚠️ Your Pro trial has expired. You're now on the Free plan with a 20-document limit. "
                "Upgrade to Pro to track more documents.",
                "warning"
            )
        
        # Refresh the page to show updated status
        return redirect(url_for("home"))
    
    # Continue with normal dashboard

    doc_count = get_document_count(user_id)
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

    # Get subscription info for the warning banner
    sub_status = get_subscription_status(user_id)

    return render_template(
        "index.html", 
        documents=documents, 
        doc_count=doc_count,  
        subscription_tier=sub_status['tier'],
        subscription_expiry=sub_status['expiry'],
        now=datetime.now(timezone.utc))

@app.route("/add", methods=["GET", "POST"])
def add_document():
    auth = require_verified()
    if auth:
        return auth

    # Check subscription limit
    if not can_add_document(session["user_id"]):
        flash("⚠️  You've reached the 20-document limit on the Free plan. Upgrade to Pro for unlimited documents.", "warning")
        return redirect(url_for("pricing"))  # You'll need a pricing page

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
        #             unique_name = f"{session['user_id']}_{int(datetime.now(timezone.utc).timestamp())}_{secure_filename(file.filename)}"
        #             file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
        #             file_path = unique_name
        #         else:
        #             error = "That file type isn't allowed"

        if not error:
            conn = get_db()
            try:
                cursor = conn.cursor()
                # Check for existing document
                cursor.execute(
                    "SELECT id FROM documents WHERE user_id = %s AND title = %s AND expiry_date = %s",
                    (session["user_id"], title, expiry_date)
                )
                existing = cursor.fetchone()
                
                if existing:
                    error = "You already have a document with this title and expiry date."
                else:
                    cursor.execute(
                        "INSERT INTO documents (title, expiry_date, user_id) VALUES (%s, %s, %s)",
                        (title, expiry_date, session["user_id"])
                    )
                    conn.commit()
                    flash("✅ Document added successfully!", "success")
                cursor.close()
            finally:
                put_db(conn)
            return redirect("/")

    return render_template("add.html", error=error)


@app.route("/delete/<int:doc_id>", methods=["POST"])
def delete_document(doc_id):
    auth = require_verified()
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
        put_db(conn)

    # --- FILE UPLOAD FEATURE: DISABLED FOR MVP (see note near the top) ---
    # if doc["file_path"]:
    #     filepath = os.path.join(app.config["UPLOAD_FOLDER"], doc["file_path"])
    #     if os.path.exists(filepath):
    #         os.remove(filepath)

    flash("✅ Document deleted successfully!", "success")
    return redirect("/")


@app.route("/edit/<int:doc_id>", methods=["GET", "POST"])
def edit_document(doc_id):
    auth = require_verified()
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
                    put_db(conn)
                flash("✅ Document updated successfully!", "success")
                return redirect("/")

    return render_template("edit.html", doc=doc, error=error)


# --- FILE UPLOAD FEATURE: DISABLED FOR MVP (see note near the top) ---
# @app.route("/view/<int:doc_id>")
# def view_file(doc_id):
#     auth = require_verified()
#     if auth:
#         return auth
#
#     doc = get_owned_document(doc_id, session["user_id"])
#     if not doc or not doc["file_path"]:
#         abort(404)
#
#     return send_from_directory(app.config["UPLOAD_FOLDER"], doc["file_path"])


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def register():
    error = None
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        password_errors = validate_password_strength(password)
        if password_errors:
            error = f"Password must contain: {', '.join(password_errors)}"
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
                        "INSERT INTO users (email, password_hash, email_verified) VALUES (%s, %s, %s) RETURNING id",
                        (email, password_hash, False)
                    )
                    new_user_id = cursor.fetchone()[0]
                    conn.commit()

                    # Send verification email
                    send_verification_email(email, new_user_id)

                    # session["user_id"] = new_user_id
                    # session["email"] = email
                    cursor.close()
                conn.commit()
            finally:
                put_db(conn)

            if not error:
                flash("✅ Account created! Please check your email to verify your address.", "success")
                return redirect(url_for("login"))

    return render_template("register.html", error=error)

@app.route("/verify-email/<token>")
def verify_email(token):
    """Verify a user's email address."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, email FROM users WHERE verification_token = %s AND verification_token_expiry > %s AND email_verified = FALSE",
            (token, datetime.now(timezone.utc))
        )
        user = cursor.fetchone()
        
        if user:
            user_id, email = user
            cursor.execute(
                "UPDATE users SET email_verified = TRUE, verification_token = NULL, verification_token_expiry = NULL WHERE id = %s",
                (user_id,)
            )
            conn.commit()

            # Send welcome email
            # send_welcome_email(email, user_id)
            
            # Log them in automatically
            session["user_id"] = user_id
            session["email"] = email
            
            flash("✅ Email verified successfully! Welcome to Fritt Tracker.", "success")
            return redirect("/")
        else:
            # Check if token expired or already used
            cursor.execute(
                "SELECT id, email_verified FROM users WHERE verification_token = %s",
                (token,)
            )
            expired_user = cursor.fetchone()
            
            if expired_user and expired_user[1]:
                flash("ℹ️ This email is already verified. Please log in.", "info")
                return redirect(url_for("login"))
            else:
                flash("❌ This verification link has expired or is invalid. Please request a new one.", "error")
                return render_template("verify_email.html", invalid=True)
        cursor.close()
    finally:
        put_db(conn)

@app.route("/resend-verification", methods=["GET", "POST"])
@limiter.limit("3 per hour")
def resend_verification():
    error = None
    success = None
    
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        
        if not email:
            error = "Please enter your email address."
        else:
            user = get_user_by_email(email)
            
            if user and len(user) >= 4:  # Check we have all 4 elements
                user_id = user[0] # id
                email_verified = user[3] # email_verified

                if not email_verified:
                    if send_verification_email(email, user_id):
                        success = "A new verification email has been sent. Please check your inbox."
                    else:
                        error = "Could not send verification email. Please try again later."
                else:
                    error = "This email is already verified. Please log in."
            else:
                error = "No unverified account found with that email address."
    
    return render_template("resend_verification.html", error=error, success=success)

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
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
            put_db(conn)

        if user and check_password_hash(user[2], password):
            session["user_id"] = user[0]
            session["email"] = user[1]
            return redirect("/")
        else:
            error = "Invalid email or password"

    return render_template("login.html", error=error)


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def forgot_password():
    message = None

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        
        if not email:
            message = "Please enter your email address."
        else:
            user = get_user_by_email(email)

            if user:
                user_id = user[0]
                token = secrets.token_urlsafe(32)
                # IMPORTANT: Store as timezone-aware UTC
                expiry = datetime.now(timezone.utc) + RESET_TOKEN_LIFETIME

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
                    put_db(conn)

                reset_link = url_for("reset_password", token=token, _external=True)
                # Force HTTPS
                if reset_link.startswith('http://'):
                    reset_link = reset_link.replace('http://', 'https://')
                send_password_reset_email(email, reset_link)

            # Same message whether or not the email is registered
            message = "If an account exists for that email, we've sent a password reset link."

    return render_template("forgot_password.html", message=message)

@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("15 per hour")
def reset_password(token):
    user = get_user_by_reset_token(token)

    if not user:
        # Token is invalid or expired
        flash("❌ This password reset link has expired or is invalid. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    # Ensure user has enough elements
    if len(user) < 3:
        flash("❌ An error occurred. Please try again.", "error")
        return redirect(url_for("forgot_password"))

    user_id = user[0]
    error = None

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

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
                put_db(conn)

            flash("✅ Password reset successfully! Please log in with your new password.", "success")
            return redirect(url_for("login"))

    return render_template("reset_password.html", invalid=False, error=error)


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    auth = require_verified()
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
            put_db(conn)

    return render_template("change_password.html", error=error, success=success)


@app.route("/delete-account", methods=["GET", "POST"])
def delete_account():
    auth = require_verified()
    if auth:
        return auth

    error = None

    if request.method == "POST":
        password = request.form["password"]

        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT password_hash FROM users WHERE id = %s", (session["user_id"],))
            current_hash = cursor.fetchone()[0]

            if not check_password_hash(current_hash, password):
                error = "Incorrect password"
            else:
                # The documents table doesn't automatically cascade-delete
                # when a user is removed (no ON DELETE CASCADE on the
                # foreign key), so documents have to be deleted first -
                # otherwise this would fail with a foreign key error.
                cursor.execute("DELETE FROM documents WHERE user_id = %s", (session["user_id"],))
                cursor.execute("DELETE FROM users WHERE id = %s", (session["user_id"],))
                conn.commit()

            cursor.close()
        finally:
            put_db(conn)

        if not error:
            session.clear()
            return redirect(url_for("login"))

    return render_template("delete_account.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/feedback")
def feedback():
    return render_template("feedback.html")

@app.route("/pricing")
def pricing():
    """Pricing page showing subscription tiers."""
    region = get_user_region()
    pricing = get_pricing(region)
    
    # If user is logged in, get their subscription info
    subscription_tier = 'free'
    subscription_expiry = None
    doc_count = 0
    
    if session.get('user_id'):
        user_id = session['user_id']
        sub_status = get_subscription_status(user_id)
        subscription_tier = sub_status['tier']
        subscription_expiry = sub_status['expiry']
        doc_count = get_document_count(user_id)
    
    return render_template(
        "pricing.html",
        pricing=pricing,
        subscription_tier=subscription_tier,
        subscription_expiry=subscription_expiry,
        doc_count=doc_count,
        now=datetime.now(timezone.utc)
    )

# --- LEGAL PAGES ---
@app.route("/terms")
def terms():
    return render_template("legal/terms.html")

@app.route("/privacy")
def privacy():
    return render_template("legal/privacy.html")

@app.route("/renewed/<int:doc_id>", methods=["GET", "POST"])
def mark_renewed(doc_id):
    """Mark a document as renewed and set a new expiry date."""
    auth = require_verified()
    if auth:
        return auth

    user_id = session["user_id"]
    doc = get_owned_document(doc_id, user_id)
    
    if not doc:
        abort(404)

    error = None
    success = None

    if request.method == "POST":
        new_expiry = request.form.get("expiry_date")
        
        if not new_expiry:
            error = "Please select a new expiry date."
        else:
            try:
                datetime.strptime(new_expiry, "%Y-%m-%d")
            except ValueError:
                error = "Invalid date format."
            else:
                conn = get_db()
                try:
                    cursor = conn.cursor()
                    # Update expiry date and reset reminder tracking
                    cursor.execute("""
                        UPDATE documents 
                        SET expiry_date = %s, 
                            last_reminder_sent = NULL, 
                            reminder_state = NULL, 
                            snoozed_until = NULL 
                        WHERE id = %s AND user_id = %s
                    """, (new_expiry, doc_id, user_id))
                    conn.commit()
                    cursor.close()
                finally:
                    put_db(conn)
                
                flash("✅ Document renewed successfully! New expiry date set.", "success")
                return redirect("/")

    return render_template("renewed.html", doc=doc, error=error, success=success)

@app.route("/cron/reminders")
def run_reminders():
    """Endpoint triggered by cron-job.org to send daily reminders."""
    # Check for the secret token to authenticate the request
    provided_token = request.args.get("token") or request.headers.get("X-Trigger-Token")
    
    if provided_token != TRIGGER_SECRET:
        abort(401, "Unauthorized: Invalid token")
    
    try:
        import reminders
        reminders.check_and_send_reminders()
        return "✅ Reminders sent successfully.", 200
    except Exception as e:
        return f"❌ Reminders failed: {str(e)}", 500

@app.route("/import-csv", methods=["GET", "POST"])
def import_csv():
    auth = require_verified()
    if auth:
        return auth

    error = None

    if request.method == "POST":
        if 'csv_file' not in request.files:
            error = "Please upload a CSV file."
        else:
            file = request.files['csv_file']
            
            if file.filename == '':
                error = "No file selected."
            elif not file.filename.lower().endswith('.csv'):
                error = "Please upload a CSV file (.csv)."
            else:
                try:
                    stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
                    csv_input = csv.reader(stream)
                    
                    headers = [h.strip().lower().replace(' ', '_') for h in next(csv_input)]
                    
                    title_match = None
                    expiry_match = None
                    
                    for i, h in enumerate(headers):
                        if h in ['title', 'document', 'document_title', 'doc_title', 'name']:
                            title_match = i
                        if h in ['expiry_date', 'expiry', 'expiration', 'expiration_date']:
                            expiry_match = i
                    
                    if title_match is None:
                        error = f"CSV must have a 'title' column. Found: {', '.join(headers)}"
                    elif expiry_match is None:
                        error = f"CSV must have an 'expiry_date' column. Found: {', '.join(headers)}"
                    else:
                        conn = get_db()
                        cursor = conn.cursor()
                        added = 0
                        failed = 0
                        
                        for row in csv_input:
                            if not row or all(cell.strip() == '' for cell in row):
                                continue
                            
                            title = row[title_match].strip() if len(row) > title_match else ""
                            expiry_date = row[expiry_match].strip() if len(row) > expiry_match else ""
                            
                            if not title or not expiry_date:
                                failed += 1
                                continue
                            
                            try:
                                datetime.strptime(expiry_date, "%Y-%m-%d")
                            except ValueError:
                                try:
                                    parsed = datetime.strptime(expiry_date, "%d/%m/%Y")
                                    expiry_date = parsed.strftime("%Y-%m-%d")
                                except ValueError:
                                    try:
                                        parsed = datetime.strptime(expiry_date, "%m/%d/%Y")
                                        expiry_date = parsed.strftime("%Y-%m-%d")
                                    except ValueError:
                                        failed += 1
                                        continue
                            
                            try:
                                cursor.execute(
                                    "INSERT INTO documents (title, expiry_date, user_id) VALUES (%s, %s, %s)",
                                    (title, expiry_date, session["user_id"])
                                )
                                added += 1
                            except Exception:
                                failed += 1
                        
                        conn.commit()
                        cursor.close()
                        put_db(conn)
                        
                        if added > 0:
                            flash(f"✅ Successfully imported {added} documents! {failed} failed.", "success")
                        else:
                            flash(f"⚠️ No documents imported. {failed} rows had errors.", "error")
                            
                        return redirect("/")
                        
                except csv.Error as e:
                    error = f"CSV error: {str(e)}"
                except Exception as e:
                    error = f"Error reading file: {str(e)}"

    return render_template("import_csv.html", error=error)

@app.route("/newsletter/subscribe", methods=["POST"])  # Changed URL
@limiter.limit("5 per hour")
def subscribe_newsletter():
    email = request.form.get("email", "").strip().lower()
    
    if not email:
        flash("Please enter your email address.", "error")
        return redirect(url_for("home"))
    
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO newsletter_subscribers (email, subscribed_at) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING",
            (email, datetime.now(timezone.utc))
        )
        conn.commit()
        cursor.close()
    finally:
        put_db(conn)  # Use put_db, not conn.close()
    
    flash("✅ Thanks for subscribing! You'll hear from us soon.", "success")
    return redirect(url_for("home"))


@app.route("/subscribe/<plan_type>")
def subscribe(plan_type):
    """Show payment page for subscription."""
    # Use require_verified instead of @login_required
    auth = require_verified()
    if auth:
        return auth
    
    user_id = session['user_id']
    user_email = session['email']
    
    # Get user's region and currency
    region = get_user_region()
    currency = get_currency_for_region(region)
    
    # Get the plan ID for this type and currency
    plan_id = get_plan_id(plan_type, currency)
    
    if not plan_id:
        flash(f"Payment plan not available for your region ({region}). Please contact support.", "error")
        return redirect(url_for("pricing"))
    
    # Get region-specific pricing
    pricing = get_pricing(region)
    
    # Get plan details for display
    plan_details = {
        'pro_monthly': {'name': 'Pro Monthly', 'price': pricing['monthly'], 'tier': 'Pro'},
        'pro_yearly': {'name': 'Pro Yearly', 'price': pricing['yearly'], 'tier': 'Pro'},
        'vip_monthly': {'name': 'VIP Monthly', 'price': pricing['vip_monthly'], 'tier': 'VIP'},
        'vip_yearly': {'name': 'VIP Yearly', 'price': pricing['vip_yearly'], 'tier': 'VIP'},
    }
    
    if plan_type not in plan_details:
        flash("Invalid plan selected.", "error")
        return redirect(url_for("pricing"))
    
    return render_template(
        "subscribe.html",
        plan_type=plan_type,
        plan_id=plan_id,
        plan_details=plan_details[plan_type],
        pricing=pricing,
        currency=currency,
        user_email=user_email
    )

@app.route("/payment/initiate", methods=["POST"])
def initiate_payment():
    """Initialize Flutterwave payment."""
    # Use require_verified instead of @login_required
    auth = require_verified()
    if auth:
        return auth
    
    plan_id = request.form.get("plan_id")
    plan_type = request.form.get("plan_type")
    
    if not plan_id or not plan_type:
        flash("Invalid payment request.", "error")
        return redirect(url_for("pricing"))
    
    # Get user's region and currency
    region = get_user_region()
    currency = get_currency_for_region(region)
    pricing = get_pricing(region)
    
    # Map plan type to amount based on region
    amount_map = {
        'pro_monthly': pricing['monthly_raw'],
        'pro_yearly': pricing['yearly_raw'],
        'vip_monthly': pricing['vip_monthly_raw'],
        'vip_yearly': pricing['vip_yearly_raw'],
    }
    
    amount = amount_map.get(plan_type, 0)
    if amount <= 0:
        flash("Invalid payment amount.", "error")
        return redirect(url_for("pricing"))
    
    try:
        # Initialize payment with Flutterwave
        response = rave.Payment.initialize({
            'amount': amount,
            'email': session['email'],
            'currency': currency,
            'tx_ref': f"fritt_{session['user_id']}_{int(time.time())}",
            'payment_plan': int(plan_id),  # Enables subscription
            'redirect_url': url_for('payment_callback', _external=True),
            'meta': {
                'user_id': session['user_id'],
                'plan_type': plan_type,
                'region': region
            }
        })
        
        # Store transaction reference in session
        session['tx_ref'] = response['data']['tx_ref']
        
        # Redirect to Flutterwave checkout
        return redirect(response['data']['link'])
        
    except Exception as e:
        print(f"Payment error: {e}")
        flash("Payment initialization failed. Please try again.", "error")
        return redirect(url_for("pricing"))

@app.route("/payment/callback")
def payment_callback():
    """Handle payment callback from Flutterwave."""
    # Use require_verified instead of @login_required
    auth = require_verified()
    if auth:
        return auth
    
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')
    
    if not tx_ref or not transaction_id:
        flash("Invalid payment callback.", "error")
        return redirect(url_for("pricing"))
    
    try:
        # Verify payment status
        response = rave.Transaction.verify(transaction_id)
        
        if response['data']['status'] == 'successful':
            # Payment successful - update user subscription
            user_id = session['user_id']
            
            # Get plan_type from meta
            plan_type = response['data'].get('meta', {}).get('plan_type', '')
            
            if not plan_type:
                # Try to get from payment plan
                payment_plan = response['data'].get('payment_plan', {})
                plan_name = payment_plan.get('name', '')
                if 'Pro' in plan_name:
                    plan_type = 'pro_monthly' if 'Monthly' in plan_name else 'pro_yearly'
                elif 'VIP' in plan_name:
                    plan_type = 'vip_monthly' if 'Monthly' in plan_name else 'vip_yearly'
            
            # Extract tier from plan_type (pro_monthly → pro)
            tier = plan_type.split('_')[0] if plan_type else 'pro'
            
            # Update user subscription in database
            conn = get_db()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE users 
                    SET subscription_tier = %s,
                        subscription_status = 'active',
                        subscription_expiry = NULL
                    WHERE id = %s
                """, (tier, user_id))
                conn.commit()
                cursor.close()
            finally:
                put_db(conn)
            
            flash("✅ Payment successful! Your subscription is now active.", "success")
            return redirect(url_for("home"))
        else:
            flash("Payment failed. Please try again.", "error")
            return redirect(url_for("pricing"))
            
    except Exception as e:
        print(f"Callback error: {e}")
        flash("Error verifying payment. Please contact support.", "error")
        return redirect(url_for("pricing"))

@app.route("/payment/cancel")
def payment_cancel():
    """Handle payment cancellation."""
    # Use require_verified instead of @login_required
    auth = require_verified()
    if auth:
        return auth

    flash("Payment cancelled. You can try again anytime.", "info")
    return redirect(url_for("pricing"))

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/business")
def business():
    return render_template("business.html")

@app.route("/admin/newsletter")
def newsletter_admin():
    if not session.get("is_admin"):  # Add is_admin flag to users table
        abort(403)
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT email, subscribed_at FROM newsletter_subscribers ORDER BY subscribed_at DESC")
    subscribers = cursor.fetchall()
    cursor.close()
    put_db(conn)
    
    return render_template("admin/newsletter.html", subscribers=subscribers)

@app.route("/webhook/flutterwave", methods=["POST"])
def flutterwave_webhook():
    """Handle Flutterwave webhook for subscription events."""
    try:
        data = request.json
        
        # Log the webhook for debugging
        print(f"📨 Webhook received: {data.get('event', 'unknown')}")
        
        # Verify webhook signature
        signature = request.headers.get('verif-hash')
        if not verify_flutterwave_webhook(data, signature):
            print("❌ Webhook signature verification failed")
            return "Unauthorized", 401
        
        event = data.get('event')
        
        if event == 'charge.completed':
            # Handle successful payment
            print("✅ Payment completed")
            
            webhook_data = data.get('data', {})
            meta = webhook_data.get('meta', {})
            user_id = meta.get('user_id')
            
            if user_id:
                plan_type = meta.get('plan_type', 'pro_monthly')
                tier = plan_type.split('_')[0] if plan_type else 'pro'
                
                conn = get_db()
                try:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE users 
                        SET subscription_tier = %s,
                            subscription_status = 'active',
                            subscription_expiry = NULL
                        WHERE id = %s
                    """, (tier, user_id))
                    conn.commit()
                    cursor.close()
                    print(f"✅ User {user_id} upgraded to {tier}")
                finally:
                    put_db(conn)
        
        elif event == 'subscription.cancelled':
            print("❌ Subscription cancelled")
            webhook_data = data.get('data', {})
            meta = webhook_data.get('meta', {})
            user_id = meta.get('user_id')
            
            if user_id:
                update_user_to_free(user_id)
                print(f"✅ User {user_id} downgraded to Free due to cancellation")
        
        elif event == 'subscription.expired':
            print("⏰ Subscription expired")
            webhook_data = data.get('data', {})
            meta = webhook_data.get('meta', {})
            user_id = meta.get('user_id')
            
            if user_id:
                update_user_to_free(user_id)
                print(f"✅ User {user_id} downgraded to Free due to expiry")
        
        elif event == 'charge.failed':
            print("❌ Payment failed")
            webhook_data = data.get('data', {})
            meta = webhook_data.get('meta', {})
            user_id = meta.get('user_id')
            if user_id:
                print(f"⚠️ Payment failed for user {user_id}")
                # Optionally send email notification
        
        else:
            print(f"ℹ️ Unhandled webhook event: {event}")
        
        return "OK", 200
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return "Internal Server Error", 500

# Health check endpoint that bypasses rate limiting
@app.route("/health")
@limiter.exempt
def health_check():
    """Health check endpoint for UptimeRobot — no rate limit."""
    return "OK", 200

if __name__ == "__main__":
    # In production (Render), debug should be False so users see your
    # custom error pages instead of the interactive debugger.
    # Render sets the FLASK_DEBUG environment variable automatically
    # based on your environment (usually False on production).
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    app.run(debug=debug_mode)
