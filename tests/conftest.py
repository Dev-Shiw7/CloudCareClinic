"""Test bootstrap.

Loads .env before any app module imports, since the tests import the data
layer directly rather than going through app.main.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Voice endpoints fail closed without a secret; give tests a known one.
os.environ.setdefault("VAPI_SERVER_SECRET", "test-secret")

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "@db:", "@postgres:")


def pytest_configure(config):
    """Refuse to run against a remote database.

    This suite calls ``Base.metadata.drop_all()``. Pointed at the deployed
    Postgres that is a production wipe, so require an explicitly local or
    throwaway database rather than trusting whatever ``.env`` happens to
    hold. Set TEST_DATABASE_URL to opt in.
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not any(h in url for h in _LOCAL_HOSTS):
        raise SystemExit(
            "\nREFUSING TO RUN: these tests drop every table, and the "
            "database is remote:\n    {}\n\n"
            "Point them at a throwaway database first:\n"
            "    TEST_DATABASE_URL=postgresql://postgres:pw@localhost:5432/intake_test"
            "\n".format(url.split("@")[-1] or "<unset>")
        )
    os.environ["DATABASE_URL"] = url
