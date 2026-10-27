"""Vercel entrypoint: the website as a serverless function.

Vercel's Python runtime imports this file and serves the ASGI ``app``. What runs
here is the landing page, onboarding, sign-in and the family page. What cannot
run here is a phone call: Twilio's Conversation Relay needs a long-lived
WebSocket, which a serverless function does not keep, so the Twilio routes
exist but no call will be connected on this host. Use Render or any server for
real calls; use this for a free demo link.

Storage: a function's filesystem is read-only except ``/tmp``, which is wiped
whenever the instance is recycled. The database lives there and the demo
family is re-seeded on every cold start, so the demo page is always populated.
Real families should not be created on this host; their data would not last.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUNDAY_PROVIDER", "fake")
os.environ.setdefault("SUNDAY_DB", "/tmp/sunday.sqlite")
os.environ.setdefault("SUNDAY_SEED_DEMO", "true")
os.environ.setdefault("SUNDAY_DEMO_LOGIN", "true")
os.environ.setdefault("SUNDAY_TWILIO_VERIFY_SIGNATURES", "true")

from sunday.app import Services  # noqa: E402
from sunday.config import load_settings  # noqa: E402
from sunday.web import create_app  # noqa: E402

_settings = load_settings()
_services = Services.from_settings(_settings)
if _settings.seed_demo:
    from sunday.demo import seed_demo

    seed_demo(_services.repo)

app = create_app(_services)
