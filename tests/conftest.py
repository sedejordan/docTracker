"""
conftest.py

Shared setup for all tests. pytest automatically finds and uses this file -
you don't import it anywhere yourself.

IMPORTANT: tests run against TEST_DATABASE_URL, a completely separate
database from your real production one. Every test wipes all data from
the tables it uses, so running these against your real database would
delete all real users' documents. To make that mistake hard to make by
accident, we refuse to start at all if TEST_DATABASE_URL isn't set -
there's no silent fallback to DATABASE_URL.
"""

import os
import sys
from pathlib import Path
import pytest

# Make sure the project root (one level up from this tests/ folder, where
# app.py and database.py live) is on Python's import search path. Without
# this, whether "import app" and "from database import ..." work depends
# on exactly how pytest was invoked and from where - fragile and
# inconsistent across machines. This makes it reliable everywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

if not TEST_DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL is not set. Tests must run against a database "
        "that is NOT your production database, since tests wipe all data "
        "between runs. Set TEST_DATABASE_URL to a separate Postgres "
        "database before running pytest."
    )

# app.py and database.py both read DATABASE_URL from the environment at
# import time, as a plain module-level variable (not something we can
# override after the fact). So we point DATABASE_URL at the test database
# BEFORE importing app - this line has to stay above "import app".
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-used-in-production")
# Flask-Limiter locks in whether it's enabled at construction time inside
# app.py, not per-request - so this has to be set before "import app" runs,
# not afterwards via app.config. See the matching comment in app.py.
os.environ["DISABLE_RATE_LIMITING"] = "true"

import psycopg2
from database import init_db  # noqa: E402  (import after env vars are set, on purpose)
import app as app_module  # noqa: E402

# Explicitly create the tables in the test database, right here, where a
# failure will actually stop the test run with a clear error - rather than
# relying only on app.py's own startup init, which deliberately swallows
# connection errors (so a slow-starting production database doesn't crash
# the live app). That's the right behavior for a running server, but the
# wrong behavior for tests: if this fails (e.g. Postgres isn't accepting
# connections yet - common right after "docker run", which needs a couple
# of seconds to finish starting up), we want to know immediately, not see
# a confusing "table does not exist" error somewhere else later.
init_db()


@pytest.fixture
def app():
    """
    The Flask app, configured for testing. WTF_CSRF_ENABLED off lets tests
    POST form data directly without needing to fetch and resubmit a real
    CSRF token first - CSRF protection itself is simple enough (a
    Flask-WTF built-in) that we're trusting the library rather than
    re-testing it here. Rate limiting is disabled separately, via the
    DISABLE_RATE_LIMITING env var set above (app.config can't turn it off
    at this point - see the comment on that line).
    """
    app_module.app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
    )
    yield app_module.app


@pytest.fixture
def client(app):
    """A Flask test client - lets tests make fake GET/POST requests without a real server."""
    return app.test_client()


@pytest.fixture(autouse=True)
def clean_database():
    """
    Runs before every single test automatically (autouse=True). Empties
    the users and documents tables so each test starts from a blank
    slate, regardless of what earlier tests did.
    RESTART IDENTITY resets auto-incrementing IDs back to 1 each time too,
    so tests can rely on predictable IDs if needed. CASCADE handles the
    users -> documents foreign key relationship automatically.
    """
    conn = psycopg2.connect(TEST_DATABASE_URL)
    cursor = conn.cursor()
    # If some earlier test left a connection open with an uncommitted
    # transaction (e.g. it crashed before closing its connection), TRUNCATE
    # would normally wait forever for that lock to clear. This makes it
    # fail loudly after 5 seconds instead, with a clear error, rather than
    # hanging the whole test run with no explanation.
    cursor.execute("SET lock_timeout = '5s';")
    cursor.execute("TRUNCATE TABLE documents, users RESTART IDENTITY CASCADE;")
    conn.commit()
    cursor.close()
    conn.close()
    yield