"""
tests/test_isolation.py

The most important test file in this project. DocTracker's entire value
proposition is that your documents are private to your account - these
tests prove that's actually true, not just assumed.

Each test uses a single Flask test client, which carries cookies/session
like a real browser would - so "logout, then register/login as a
different user" genuinely simulates two separate people using the app,
one after another, in the same browser session slot.
"""

import os
import psycopg2


def get_db_connection():
    # conftest.py sets DATABASE_URL to the test database before any test
    # runs, so this always points at the test database, never production.
    return psycopg2.connect(os.environ["DATABASE_URL"])


def register(client, email, password="password123"):
    return client.post(
        "/register",
        data={"email": email, "password": password},
        follow_redirects=True
    )


def add_document(client, title="Passport", expiry_date="2099-01-01"):
    return client.post(
        "/add",
        data={"title": title, "expiry_date": expiry_date},
        follow_redirects=True
    )


def get_document_id_by_title(title):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM documents WHERE title = %s", (title,))
        row = cursor.fetchone()
        assert row is not None, f"Expected to find a document titled {title!r} in the database, found none"
        cursor.close()
        return row[0]
    finally:
        conn.close()


def test_users_homepage_does_not_show_other_users_documents(client):
    register(client, "alice@example.com")
    add_document(client, title="Alice Passport")
    client.get("/logout")

    register(client, "bob@example.com")
    add_document(client, title="Bob License")

    response = client.get("/")
    assert b"Bob License" in response.data
    assert b"Alice Passport" not in response.data


def test_users_search_does_not_return_other_users_documents(client):
    register(client, "alice@example.com")
    add_document(client, title="Alice Passport")
    client.get("/logout")

    register(client, "bob@example.com")
    add_document(client, title="Bob Passport")

    # Bob searches for "Passport" - a title that matches both his own
    # document and Alice's. He should only see his own.
    response = client.get("/?q=Passport")
    assert b"Bob Passport" in response.data
    assert b"Alice Passport" not in response.data


def test_user_cannot_view_another_users_edit_page_by_guessing_the_id(client):
    register(client, "alice@example.com")
    add_document(client, title="Alice's Passport")
    alice_doc_id = get_document_id_by_title("Alice's Passport")
    client.get("/logout")

    register(client, "bob@example.com")
    response = client.get(f"/edit/{alice_doc_id}")

    # Not a redirect to somewhere friendly, not a blank success page -
    # a real 404, same as if the document didn't exist at all. This
    # matters: the response shouldn't hint that a document with this ID
    # exists but just belongs to someone else.
    assert response.status_code == 404


def test_user_cannot_edit_another_users_document_by_guessing_the_id(client):
    register(client, "alice@example.com")
    add_document(client, title="Alice's Passport", expiry_date="2099-01-01")
    alice_doc_id = get_document_id_by_title("Alice's Passport")
    client.get("/logout")

    register(client, "bob@example.com")
    client.post(
        f"/edit/{alice_doc_id}",
        data={"title": "Hacked Title", "expiry_date": "2050-01-01"},
        follow_redirects=True
    )

    # Go straight to the database - not back through the app - to check
    # Alice's document is genuinely untouched, not just hidden from Bob's
    # view.
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT title FROM documents WHERE id = %s", (alice_doc_id,))
    title = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    assert title == "Alice's Passport"


def test_user_cannot_delete_another_users_document_by_guessing_the_id(client):
    register(client, "alice@example.com")
    add_document(client, title="Alice's Passport")
    alice_doc_id = get_document_id_by_title("Alice's Passport")
    client.get("/logout")

    register(client, "bob@example.com")
    response = client.post(f"/delete/{alice_doc_id}")
    assert response.status_code == 404

    # Confirm the document is genuinely still in the database.
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM documents WHERE id = %s", (alice_doc_id,))
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    assert count == 1


def test_two_users_can_have_documents_with_the_same_title(client):
    """
    Not a security test, but a related correctness check: two different
    users independently naming a document "Passport" shouldn't clash or
    overwrite each other, since they're only ever compared within the
    same user_id.
    """
    register(client, "alice@example.com")
    add_document(client, title="Passport", expiry_date="2099-01-01")
    client.get("/logout")

    register(client, "bob@example.com")
    response = add_document(client, title="Passport", expiry_date="2099-06-01")

    assert response.status_code == 200
    assert b"Passport" in response.data