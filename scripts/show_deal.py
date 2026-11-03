"""Read a deal back from HubSpot with its pipeline stage, contact and note.

Usage:
    python scripts/show_deal.py <deal_id>
"""

from __future__ import annotations

import os
import re
import sys
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hubspot import HubSpotClient, HubSpotError  # noqa: E402
from scripts._env import load_dotenv  # noqa: E402


def _ids(obj: dict, kind: str) -> list[str]:
    return [r["id"] for r in obj.get("associations", {}).get(kind, {}).get("results", [])]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    load_dotenv()
    token = os.environ.get("HUBSPOT_TOKEN", "").strip()
    if not token:
        print("Set HUBSPOT_TOKEN first.", file=sys.stderr)
        return 2
    client = HubSpotClient(token, os.environ.get("HUBSPOT_BASE_URL") or "https://api.hubapi.com")
    try:
        deal = client.get_object(
            "deals",
            sys.argv[1],
            ["dealname", "pipeline", "dealstage", "hs_priority", "intake_idempotency_key"],
            ["contacts", "notes"],
        )
        stages = {
            s["id"]: (p["label"], s["label"])
            for p in client.list_deal_pipelines()
            for s in p.get("stages", [])
        }
        props = deal["properties"]
        pipeline, stage = stages.get(props["dealstage"], (props["pipeline"], props["dealstage"]))
        print(f"Deal {deal['id']}: {props['dealname']}")
        print(f"  pipeline / stage : {pipeline} / {stage}")
        print(f"  priority         : {props.get('hs_priority')}")
        key = props.get("intake_idempotency_key") or ""
        print(f"  idempotency key  : {key[:16]}…")

        for contact_id in _ids(deal, "contacts"):
            c = client.get_object(
                "contacts", contact_id, ["firstname", "lastname", "email", "company"]
            )["properties"]
            name = f"{c.get('firstname', '')} {c.get('lastname', '')}".strip()
            print(f"  contact {contact_id}: {name} <{c.get('email')}>, {c.get('company')}")

        for note_id in _ids(deal, "notes"):
            body = client.get_object("notes", note_id, ["hs_note_body"])["properties"]
            text = unescape(re.sub(r"<br>|</p>", "\n", body.get("hs_note_body") or ""))
            text = re.sub(r"<[^>]+>", "", text).strip()
            print(f"  note {note_id}:")
            for line in text.splitlines():
                if line.strip():
                    print(f"    {line.strip()}")
    except HubSpotError as exc:
        print(f"HubSpot error: {exc} {exc.body}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
