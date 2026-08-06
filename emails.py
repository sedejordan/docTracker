# emails.py
import requests
import os
from flask import url_for

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "auth@fritt.org")

def send_welcome_email(user_email, user_name=None):
    """Send welcome email to new user."""
    try:
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