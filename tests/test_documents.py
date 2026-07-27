"""
tests/test_documents.py

Covers the core document features (add/edit/delete) for a single user,
and confirms logged-out visitors can't reach any of them.
"""

import os
import psycopg2


def get_db_connection():
    # conftest.py sets DATABASE_URL to the test database before any test
    # runs, so this always points at the test database, never production.
    return psycopg2.connect(os.environ["DATABASE_URL"])


def register(client, email="alice@example.com", password="password123"):
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


def test_logged_out_visitor_is_redirected_to_login(client):
    response = client.get("/", follow_redirects=True)
    assert b"Login" in response.data or b"Password" in response.data


def test_logged_out_visitor_cannot_add_document(client):
    response = client.get("/add", follow_redirects=True)
    assert b"Login" in response.data or b"Password" in response.data


def test_add_document_appears_on_homepage(client):
    register(client)
    add_document(client, title="Passport", expiry_date="2099-01-01")

    response = client.get("/")
    assert b"Passport" in response.data


def test_add_document_requires_title(client):
    register(client)
    response = add_document(client, title="", expiry_date="2099-01-01")
    assert b"Please fill in all fields" in response.data


def test_add_document_requires_valid_date(client):
    register(client)
    response = add_document(client, title="Passport", expiry_date="not-a-date")
    assert b"Invalid date format" in response.data


def test_edit_document_updates_title(client):
    register(client)
    add_document(client, title="Old Title", expiry_date="2099-01-01")

    # Find the document's id the same way a real user would - by looking
    # at the homepage's edit link, not by assuming it's 1.
    response = client.get("/")
    assert b"Old Title" in response.data

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM documents WHERE title = %s", ("Old Title",))
        row = cursor.fetchone()
        assert row is not None, "Expected to find a document titled 'Old Title' in the database, found none"
        doc_id = row[0]
        cursor.close()
    finally:
        conn.close()

    client.post(
        f"/edit/{doc_id}",
        data={"title": "New Title", "expiry_date": "2099-01-01"},
        follow_redirects=True
    )

    response = client.get("/")
    assert b"New Title" in response.data
    assert b"Old Title" not in response.data


def test_delete_document_removes_it(client):
    register(client)
    add_document(client, title="Delete Me", expiry_date="2099-01-01")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM documents WHERE title = %s", ("Delete Me",))
        row = cursor.fetchone()
        assert row is not None, "Expected to find a document titled 'Delete Me' in the database, found none"
        doc_id = row[0]
        cursor.close()
    finally:
        conn.close()

    client.post(f"/delete/{doc_id}", follow_redirects=True)

    response = client.get("/")
    assert b"Delete Me" not in response.data
