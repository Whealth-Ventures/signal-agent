"""One-time Azure setup: grant the agent's app registration read on ONE site.

`Sites.Selected` grants nothing by itself — after a tenant admin consents to it,
somebody with site-admin rights has to name the app on the specific site. That
grant is only available over Graph, not in the Azure or SharePoint portal UI,
which is why this script exists.

Run it once, by hand, from a machine where an admin is logged into the Azure CLI:

    az login
    python scripts/grant_sharepoint_access.py \
        --site contoso.sharepoint.com:/sites/SignalAgent \
        --client-id <the app registration's Application (client) ID>

The admin token comes from `az account get-access-token`; pass --token to use
one from elsewhere instead. Add --list to only show the site id and the grants
already in place (handy for verifying afterwards, or debugging a 403 from
src/sharepoint_sync.py).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"


def _az_token() -> str:
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", "https://graph.microsoft.com"],
            capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        sys.exit(
            f"could not get a token from the Azure CLI ({e}). "
            "Run `az login` as a tenant/site admin, or pass --token."
        )
    return json.loads(out.stdout)["accessToken"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--site", required=True, help="host:/sites/Name")
    p.add_argument("--client-id", help="app registration Application (client) ID")
    p.add_argument("--name", default="signal-agent-sharepoint", help="app display name")
    p.add_argument("--token", help="Graph access token (default: from `az`)")
    p.add_argument("--list", action="store_true", help="only show site id + existing grants")
    args = p.parse_args()

    if not args.list and not args.client_id:
        p.error("--client-id is required unless --list is given")

    client = httpx.Client(
        timeout=30.0,
        headers={"Authorization": f"Bearer {args.token or _az_token()}"},
    )

    r = client.get(f"{GRAPH}/sites/{args.site}")
    if r.status_code != 200:
        sys.exit(f"could not resolve site '{args.site}': {r.status_code} {r.text}")
    site_id = r.json()["id"]
    print(f"site id: {site_id}")

    if not args.list:
        r = client.post(
            f"{GRAPH}/sites/{site_id}/permissions",
            json={
                "roles": ["read"],
                "grantedToIdentities": [
                    {"application": {"id": args.client_id, "displayName": args.name}}
                ],
            },
        )
        if r.status_code >= 300:
            sys.exit(f"grant failed: {r.status_code} {r.text}")
        print(f"granted read to {args.name} ({args.client_id})")

    r = client.get(f"{GRAPH}/sites/{site_id}/permissions")
    r.raise_for_status()
    print("\ncurrent grants on this site:")
    for perm in r.json().get("value", []):
        apps = [
            i.get("application", {}).get("displayName", "?")
            for i in perm.get("grantedToIdentities", [])
        ]
        print(f"  {perm.get('roles')} -> {', '.join(apps) or '(non-app identity)'}")

    print(f"\nSet SHAREPOINT_SITE={args.site} in the agent env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
