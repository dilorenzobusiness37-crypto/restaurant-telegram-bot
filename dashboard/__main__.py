"""Start the owner dashboard: python -m dashboard"""

import os
import sys

import uvicorn

from . import app as dashboard
from . import settings_store as store
import database as db


def main() -> None:
    # A password set from the dashboard's settings page replaces DASHBOARD_PASSWORD.
    custom = store.has_custom_password()
    if not dashboard.PASSWORD and not custom:
        sys.exit("DASHBOARD_PASSWORD is missing. Set it in your .env file (see .env.example).")
    if dashboard.PASSWORD == "change-me" and not custom:
        sys.exit("DASHBOARD_PASSWORD is still the example value 'change-me'. Choose your own password in .env.")
    db.init_db()

    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    print(f"Dashboard running on http://{'localhost' if host == '127.0.0.1' else host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(dashboard.app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
