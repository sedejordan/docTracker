#!/usr/bin/env python3
"""
One-time migration script to update user subscriptions.
Run this once after deploying the new code.
"""

import os
import psycopg2
from datetime import datetime, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL")

def run_migration():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not set")
        return
    
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cursor = conn.cursor()
        
        # Check if columns exist first
        cursor.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'users' 
            AND column_name IN ('subscription_tier', 'subscription_status', 'subscription_expiry')
        """)
        existing_columns = [row[0] for row in cursor.fetchall()]
        
        # Add columns if they don't exist
        if 'subscription_tier' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN subscription_tier VARCHAR(50) DEFAULT 'free'")
            print("✅ Added subscription_tier column")
        
        if 'subscription_status' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN subscription_status VARCHAR(50) DEFAULT 'active'")
            print("✅ Added subscription_status column")
        
        if 'subscription_expiry' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN subscription_expiry TIMESTAMP")
            print("✅ Added subscription_expiry column")
        
        # Users with >20 documents get 1 month of Pro
        cursor.execute("""
            UPDATE users 
            SET subscription_tier = 'pro',
                subscription_status = 'active',
                subscription_expiry = CURRENT_TIMESTAMP + INTERVAL '1 month'
            WHERE id IN (
                SELECT user_id 
                FROM documents 
                GROUP BY user_id 
                HAVING COUNT(*) > 20
            )
            AND subscription_tier = 'free'
        """)
        updated_count = cursor.rowcount
        conn.commit()
        
        print(f"✅ Updated {updated_count} users to Pro tier (1 month free)")
        print(f"   They'll have 1 month to manage their documents")
        print(f"   After expiry, the 20 farthest-from-expiry documents will be kept")
        print(f"   and the rest will be removed")
        
        # Show summary
        cursor.execute("""
            SELECT 
                subscription_tier,
                COUNT(*) as count,
                COUNT(CASE WHEN subscription_expiry IS NOT NULL THEN 1 END) as with_expiry
            FROM users 
            GROUP BY subscription_tier
        """)
        summary = cursor.fetchall()
        print("\n📊 Subscription Summary:")
        for tier, count, with_expiry in summary:
            expiry_text = f" ({with_expiry} with expiry)" if with_expiry > 0 else ""
            print(f"  {tier}: {count} users{expiry_text}")
        
        cursor.close()
    finally:
        conn.close()

if __name__ == "__main__":
    run_migration()