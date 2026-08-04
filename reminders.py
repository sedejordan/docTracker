import os
import psycopg2
from datetime import datetime, timedelta
import requests

APP_URL = os.environ.get("APP_URL", "https://doctracker-bxxw.onrender.com")
DATABASE_URL = os.environ.get("DATABASE_URL")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

def get_db():
    return psycopg2.connect(DATABASE_URL)

def send_reminder_email(email, documents, urgency):
    """Send a batch email with all expiring documents."""
    subject = f"⚠️ {urgency}: Your Fritt Tracker documents need attention"
    
    html = f"""
    <h2>Your Fritt Tracker Documents</h2>
    <p>The following documents need your attention:</p>
    <ul>
    """
    for doc in documents:
        html += f"<li><strong>{doc['title']}</strong> — expires {doc['expiry_date']} ({doc['days_left']} days left)</li>"
    
    html += f"""
    </ul>
    <p><a href="{APP_URL}">View your dashboard →</a></p>
    <p style="font-size: 12px; color: #666;">Update reminders in your account settings.</p>
    """
    
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": "Fritt Tracker <onboarding@resend.dev>",  # Change later
            "to": [email],
            "subject": subject,
            "html": html
        }
    )

def check_and_send_reminders():
    """Find documents that need reminders and send emails."""
    conn = get_db()
    cursor = conn.cursor()
    
    today = datetime.today().date()
    
    # Find all users with expiring documents
    cursor.execute("""
        SELECT DISTINCT u.id, u.email 
        FROM users u
        JOIN documents d ON u.id = d.user_id
        WHERE d.expiry_date::date <= %s + INTERVAL '6 months'
    """, (today,))
    
    users = cursor.fetchall()
    
    for user_id, email in users:
        # Get all documents expiring within 120 days (matches your "Safe" threshold)
        cursor.execute("""
            SELECT id, title, expiry_date::date,
                   (expiry_date::date - %s) as days_left
            FROM documents
            WHERE user_id = %s
            AND expiry_date::date <= %s + INTERVAL '120 days'
            ORDER BY expiry_date ASC
        """, (today, user_id, today))
        
        docs = cursor.fetchall()
        
        if not docs:
            continue
        
        # Categorize based on your new thresholds
        critical = []      # 0-7 days (matches your "Urgent" threshold change)
        urgent = []        # 8-60 days (matches your "Warning" → "Urgent" transition)
        warning = []       # 61-120 days (matches your "Good" → "Warning" transition)
        expired = []  # <0 days

        for doc_id, title, expiry_date, days_left in docs:
            doc = {
                'id': doc_id,
                'title': title,
                'expiry_date': expiry_date,
                'days_left': days_left
            }
            
            if days_left < 0:
                expired.append(doc)
            elif days_left <= 7:
                critical.append(doc)
            elif days_left <= 60:
                urgent.append(doc)
            else:
                warning.append(doc)

        # Send expired emails daily (same as critical)
        if expired:
            send_reminder_email(email, expired, "EXPIRED")
        elif critical:
            send_reminder_email(email, critical, "CRITICAL")
        elif urgent:
            # Check if it's been at least 7 days since last reminder
            cursor.execute("""
                SELECT MAX(last_reminder_sent) FROM documents 
                WHERE user_id = %s AND expiry_date::date <= %s + INTERVAL '90 days'
            """, (user_id, today))
            last_reminder = cursor.fetchone()[0]
            
            if not last_reminder or (today - last_reminder).days >= 7:
                send_reminder_email(email, urgent, "URGENT")
                # Update last_reminder_sent for these documents
                for doc in urgent:
                    cursor.execute(
                        "UPDATE documents SET last_reminder_sent = %s WHERE id = %s",
                        (today, doc['id'])
                    )
        elif warning:
            # Monthly reminder
            cursor.execute("""
                SELECT MAX(last_reminder_sent) FROM documents 
                WHERE user_id = %s AND expiry_date::date <= %s + INTERVAL '180 days'
            """, (user_id, today))
            last_reminder = cursor.fetchone()[0]
            
            if not last_reminder or (today - last_reminder).days >= 30:
                send_reminder_email(email, warning, "WARNING")
                for doc in warning:
                    cursor.execute(
                        "UPDATE documents SET last_reminder_sent = %s WHERE id = %s",
                        (today, doc['id'])
                    )
        
        conn.commit()
    
    cursor.close()
    conn.close()

if __name__ == "__main__":
    check_and_send_reminders()