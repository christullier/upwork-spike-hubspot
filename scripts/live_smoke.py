"""Live end-to-end smoke test against a real HubSpot test account and the Claude API.

This creates REAL records (a contact, a deal and a note) in whatever HubSpot
account HUBSPOT_TOKEN belongs to. Use a developer test account.

What it does:
  1. Signs a sample payload with WEBHOOK_SECRET and POSTs it to the app.
  2. Prints the classification and the HubSpot record IDs/links.
  3. POSTs the identical payload again and checks it is reported as a duplicate.

By default the app runs in-process (no server needed). Pass --url to target a
running server instead, e.g. --url http://localhost:8000/webhooks/intake.

Usage:
    cp .env.example .env   # fill in real values
    python scripts/live_smoke.py
    python scripts/live_smoke.py --fixture tests/fixtures/intake_quote.json
    python scripts/live_smoke.py --skip-llm   # HubSpot only, no Anthropic key needed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.classify import fallback  # noqa: E402
from app.config import ConfigError, Settings  # noqa: E402
from app.hubspot import HubSpotClient, HubSpotError  # noqa: E402
from app.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, compute_signature  # noqa: E402
from scripts._env import load_dotenv  # noqa: E402


def _post(target, url: str, body: bytes, secret: str):
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: compute_signature(secret, ts, body),
    }
    return target.post(url, content=body, headers=headers)


class _SkipLLM:
    """Stand-in classifier for --skip-llm: always the safe fallback."""

    def classify(self, payload):
        return fallback("llm_skipped")


def _print_links(settings: Settings, data: dict) -> None:
    client = HubSpotClient(settings.hubspot_token, settings.hubspot_base_url)
    try:
        info = client.account_details()
    except HubSpotError:
        return
    finally:
        client.close()
    base = f"https://{info.get('uiDomain', 'app.hubspot.com')}/contacts/{info['portalId']}/record"
    print(f"   contact: {base}/0-1/{data['contact_id']}")
    print(f"   deal:    {base}/0-3/{data['deal_id']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", default=str(ROOT / "tests/fixtures/intake_coi.json"))
    parser.add_argument("--url", help="POST to a running server instead of in-process")
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="don't call Claude; every request takes the needs-review fallback path",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    needed = ["HUBSPOT_TOKEN"] + ([] if args.skip_llm else ["ANTHROPIC_API_KEY"])
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        print(f"Live mode needs {', '.join(missing)}. Skipping.", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    payload = json.loads(Path(args.fixture).read_text())
    # Unique email per run so repeated smoke tests don't collapse into one contact.
    tag = uuid.uuid4().hex[:8]
    local, _, domain = payload["email"].partition("@")
    payload["email"] = f"{local}+smoke{tag}@{domain}"
    body = json.dumps(payload).encode()

    if args.url:
        target, url, close = httpx.Client(timeout=60), args.url, None
    else:
        from fastapi.testclient import TestClient

        from app.main import create_app

        classifier = _SkipLLM() if args.skip_llm else None
        target = TestClient(create_app(settings, classifier=classifier))
        target.__enter__()
        url, close = "/webhooks/intake", target

    try:
        print(f"-> POST {url}  ({payload['email']})")
        first = _post(target, url, body, settings.webhook_secret)
        print(f"<- {first.status_code} {json.dumps(first.json(), indent=2)}")
        if first.status_code != 201:
            return 1

        data = first.json()
        _print_links(settings, data)

        print("-> POST same payload again (should be a duplicate)")
        second = _post(target, url, body, settings.webhook_secret)
        print(f"<- {second.status_code} {second.json()}")
        ok = second.status_code == 200 and second.json().get("deal_id") == data["deal_id"]
        print("PASS" if ok else "FAIL: duplicate was not detected")
        return 0 if ok else 1
    finally:
        if close is not None:
            close.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
