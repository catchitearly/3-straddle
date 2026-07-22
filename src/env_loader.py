"""
Minimal .env file loader - no python-dotenv dependency needed for one simple
KEY=VALUE file.

Only used for LOCAL runs (live_notifier_local.py, run_backtest.py if you run
it locally). On GitHub Actions there's no .env file, so this silently does
nothing there and the real secrets-derived environment variables (already
set by the workflow) are used untouched.

Existing environment variables always win - this never overwrites something
you've already exported in your shell.
"""

import os


def load_dotenv_if_present(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
