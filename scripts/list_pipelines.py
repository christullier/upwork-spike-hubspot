"""Print your HubSpot deal pipelines and stage IDs, for filling in .env.

Usage:
    HUBSPOT_TOKEN=pat-... python scripts/list_pipelines.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hubspot import HubSpotClient, HubSpotError  # noqa: E402
from scripts._env import load_dotenv  # noqa: E402


def main() -> int:
    load_dotenv()
    token = os.environ.get("HUBSPOT_TOKEN", "").strip()
    if not token:
        print("Set HUBSPOT_TOKEN (in the environment or .env) first.", file=sys.stderr)
        return 2
    base = os.environ.get("HUBSPOT_BASE_URL", "").strip() or "https://api.hubapi.com"
    client = HubSpotClient(token, base)
    try:
        pipelines = client.list_deal_pipelines()
    except HubSpotError as exc:
        print(f"HubSpot error: {exc} {exc.body}", file=sys.stderr)
        return 1
    finally:
        client.close()

    for pipeline in pipelines:
        print(f"\nPipeline: {pipeline['label']}\n  HUBSPOT_PIPELINE_ID={pipeline['id']}")
        for stage in sorted(pipeline.get("stages", []), key=lambda s: s.get("displayOrder", 0)):
            print(f"    {stage['id']:<24} {stage['label']}")
    print(
        "\nCopy the stage IDs into HUBSPOT_STAGE_QUOTE, _COI, _POLICY_CHANGE, _RENEWAL and _REVIEW."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
