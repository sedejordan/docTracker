"""
tests/test_auth.py

Covers account creation and login/logout - the basic building blocks
everything else depends on.
"""


def register(client, email="alice@example.com", password="password123"):
    return client.post(
        "/register",
        data={"email": email, "password": password},
        follow_redirects=True
    )


def login(client, email="alice@example.com", password="password123"):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=True
    )


def test_register_creates_account_and_logs_user_in(client):
    response = register(client)
    assert response.status_code == 200
    # A successful register redirects to the homepage and logs the user in
    # automatically, so the homepage (not the login page) should show.
    assert b"Add Document" in response.data


def test_register_rejects_duplicate_email(client):
    register(client, email="alice@example.com")
    client.get("/logout")

    response = register(client, email="alice@example.com")
    assert b"already exists" in response.data


def test_register_rejects_short_password(client):
    response = register(client, email="alice@example.com", password="short")
    assert b"at least 8 characters" in response.data


def test_login_with_correct_password_succeeds(client):
    register(client, email="alice@example.com", password="password123")
    client.get("/logout")

    response = login(client, email="alice@example.com", password="password123")
    assert b"Add Document" in response.data


def test_login_with_wrong_password_fails(client):
    register(client, email="alice@example.com", password="password123")
    client.get("/logout")

    response = login(client, email="alice@example.com", password="wrongpassword")
    assert b"Invalid email or password" in response.data


def test_login_with_unknown_email_fails(client):
    response = login(client, email="nobody@example.com", password="password123")
    assert b"Invalid email or password" in response.data


def test_logout_requires_login_again_to_see_documents(client):
    register(client, email="alice@example.com")
    client.get("/logout")

    # After logging out, the homepage should redirect to the login page
    # rather than show anything.
    response = client.get("/", follow_redirects=True)
    assert b"Login" in response.data or b"Password" in response.data


def test_delete_account_with_wrong_password_fails(client):
    register(client, email="alice@example.com", password="password123")
    response = client.post(
        "/delete-account",
        data={"password": "wrongpassword"},
        follow_redirects=True
    )
    assert b"Incorrect password" in response.data


def test_delete_account_removes_account_and_logs_out(client):
    register(client, email="alice@example.com", password="password123")
    client.post(
        "/delete-account",
        data={"password": "password123"},
        follow_redirects=True
    )

    # Session should be cleared - homepage should redirect to login now.
    response = client.get("/", follow_redirects=True)
    assert b"Login" in response.data or b"Password" in response.data

    # And the email should be free to register again, since the account
    # is genuinely gone, not just hidden.
    response = register(client, email="alice@example.com", password="password123")
    assert b"already exists" not in response.data


def test_delete_account_also_removes_documents(client):
    import os
    import psycopg2

    register(client, email="alice@example.com", password="password123")
    client.post(
        "/add",
        data={"title": "Passport", "expiry_date": "2099-01-01"},
        follow_redirects=True
    )
    client.post(
        "/delete-account",
        data={"password": "password123"},
        follow_redirects=True
    )

    # Go straight to the database to confirm no orphaned documents were
    # left behind for a user_id that no longer has a matching user.
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM documents")
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    assert count == 0


def test_logged_out_visitor_cannot_delete_account(client):
    response = client.get("/delete-account", follow_redirects=True)
    assert b"Login" in response.data or b"Password" in response.data
