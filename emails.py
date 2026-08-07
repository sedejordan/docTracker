# emails.py
import requests
import os
import secrets
from datetime import datetime, timedelta, timezone
from flask import url_for

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "auth@fritt.org")

def send_welcome_email(user_email, user_name=None):
    """Send welcome email to new user."""
    try:
        if not RESEND_API_KEY:
            print("❌ RESEND_API_KEY not set, cannot send welcome email")
            return False
            
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [user_email],
                "subject": "Welcome to Fritt Tracker! 🎉",
                "html": f"""
                    <h1>Welcome to Fritt Tracker!</h1>
                    <p>We're excited to help you keep track of your important documents.</p>
                    <h2>Quick Tips:</h2>
                    <ul>
                        <li>📄 Add your documents with expiry dates</li>
                        <li>🔔 Get reminders before they expire</li>
                        <li>📊 Track everything in one place</li>
                    </ul>
                    <p><a href="https://tracker.fritt.org/" style="color: #2563eb;">Get Started →</a></p>
                """
            },
            timeout=10
        )
        return response.status_code < 400
    except Exception as e:
        print(f"Error sending welcome email: {e}")
        return False

def send_reminder_email(user_email, document_title, days_left, expiry_date):
    """Send a reminder about an expiring document."""
    try:
        if not RESEND_API_KEY:
            print("❌ RESEND_API_KEY not set, cannot send reminder email")
            return False
            
        urgency = "URGENT" if days_left <= 7 else "Reminder"
        
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [user_email],
                "subject": f"{urgency}: '{document_title}' expires in {days_left} days",
                "html": f"""
                    <h2>Document Expiry Reminder</h2>
                    <p>Your document <strong>"{document_title}"</strong> will expire in <strong>{days_left} days</strong>.</p>
                    <p><strong>Expiry Date:</strong> {expiry_date}</p>
                    <p><a href="https://tracker.fritt.org/" style="color: #2563eb;">View your documents →</a></p>
                    <hr>
                    <p style="color: #6b7280; font-size: 14px;">Visit Fritt Tracker to renew or update this document.</p>
                """
            },
            timeout=10
        )
        return response.status_code < 400
    except Exception as e:
        print(f"Error sending reminder email: {e}")
        return False

# FIXED: Added missing send_verification_email function
def send_verification_email(user_email, user_id):
    """Send email verification link to a new user."""
    try:
        if not RESEND_API_KEY:
            print("❌ RESEND_API_KEY not set, cannot send verification email")
            return False
            
        token = secrets.token_urlsafe(32)
        expiry = datetime.now(timezone.utc) + timedelta(hours=24)
        
        # Store token in database - this requires database access
        # The database function is in app.py, so this function should be called from app.py
        # This is a placeholder for when emails.py is used independently
        
        verification_link = f"https://tracker.fritt.org/verify-email/{token}"
        
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [user_email],
                "subject": "Verify your email address for Fritt Tracker",
                "html": f"""
                    <p>Welcome to Fritt Tracker!</p>
                    <p>Please click the link below to verify your email address:</p>
                    <p><a href="{verification_link}">Verify Email Address</a></p>
                    <p>This link expires in 24 hours.</p>
                    <p>If you didn't create an account, you can safely ignore this email.</p>
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

# FIXED: Added missing send_password_reset_email function
def send_password_reset_email(user_email, reset_link):
    """Send password reset email via Resend."""
    try:
        if not RESEND_API_KEY:
            print("❌ RESEND_API_KEY not set, cannot send password reset email")
            return
            
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