"""Mirror the SharePoint inputs folder down into inputs/ before a run.

SharePoint is the source of truth for every file under inputs/ — the four
workbooks, portfolio_context.md, and the content/ corpus. Editing a workbook in
SharePoint is all it takes; the next scheduled run picks it up. The copies in
git are the seed for a fresh clone and the fallback when SharePoint is
unreachable, not the thing you edit.

This runs as its OWN process, before python src/main.py — NOT as a call inside
main(). config.py parses inputs/tuning.xlsx at *import* time, so a sync that ran
after `import config` would apply one run late. See deploy/run-digest.sh.

Deliberately does not import config: config would parse the very workbook this
script is about to replace, and would blow up on a box that has no inputs yet.
Env is read directly instead.

Auth is app-only Microsoft Graph (client credentials) against an Azure AD app
registration holding Sites.Selected on the one site. Set:

    SHAREPOINT_TENANT_ID
    SHAREPOINT_CLIENT_ID
    SHAREPOINT_CLIENT_SECRET
    SHAREPOINT_SITE         e.g. contoso.sharepoint.com:/sites/SignalAgent
    SHAREPOINT_INPUTS_PATH  drive-relative folder, e.g. Signal Agent/inputs

Leave any of them unset and the sync is a no-op — local dev and CI keep running
on the files in git.

Usage:
    python src/sharepoint_sync.py              # mirror down
    python src/sharepoint_sync.py --dry-run    # list what it would fetch
    python src/sharepoint_sync.py --browse     # show the folder tree (setup aid)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
INPUTS_DIR = ROOT / "inputs"
load_dotenv(ROOT / ".env")

GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = 60.0


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


_AUTH_VARS = (
    "SHAREPOINT_TENANT_ID",
    "SHAREPOINT_CLIENT_ID",
    "SHAREPOINT_CLIENT_SECRET",
    "SHAREPOINT_SITE",
)


def can_authenticate() -> bool:
    """Enough to talk to Graph. --browse needs only this: it's the tool you use
    to *find* SHAREPOINT_INPUTS_PATH, so it can't require it."""
    return all(_env(k) for k in _AUTH_VARS)


def is_configured() -> bool:
    return can_authenticate() and bool(_env("SHAREPOINT_INPUTS_PATH"))


def _token(client: httpx.Client) -> str:
    r = client.post(
        f"https://login.microsoftonline.com/{_env('SHAREPOINT_TENANT_ID')}"
        "/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _env("SHAREPOINT_CLIENT_ID"),
            "client_secret": _env("SHAREPOINT_CLIENT_SECRET"),
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _site_id(client: httpx.Client) -> str:
    """Resolve `host:/sites/Name` to the opaque site id Graph wants."""
    r = client.get(f"{GRAPH}/sites/{_env('SHAREPOINT_SITE')}")
    r.raise_for_status()
    return r.json()["id"]


def _children(client: httpx.Client, site_id: str, path: str) -> list[dict]:
    """List one folder. `path` is drive-relative; empty string means drive root."""
    loc = f"root:/{quote(path)}:" if path else "root"
    items: list[dict] = []
    url = f"{GRAPH}/sites/{site_id}/drive/{loc}/children?$top=200"
    while url:
        r = client.get(url)
        r.raise_for_status()
        body = r.json()
        items.extend(body.get("value", []))
        url = body.get("@odata.nextLink")
    return items


def _walk(client: httpx.Client, site_id: str, path: str, rel: str = ""):
    """Yield (relative_path, download_url) for every file under `path`."""
    for item in _children(client, site_id, path):
        name = item["name"]
        child_rel = f"{rel}/{name}" if rel else name
        if "folder" in item:
            yield from _walk(client, site_id, f"{path}/{name}", child_rel)
        elif url := item.get("@microsoft.graph.downloadUrl"):
            yield child_rel, url


def browse(path: str = "", depth: int = 2) -> None:
    """Print the folder tree under `path` so you can find SHAREPOINT_INPUTS_PATH.

    A SharePoint "Copy link" gives you an opaque sharing token, not a path — this
    is the quickest way to see what the drive-relative path actually is. Also the
    thing to reach for when a sync reports zero files.
    """
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        client.headers["Authorization"] = f"Bearer {_token(client)}"
        site_id = _site_id(client)

        def walk(p: str, level: int) -> None:
            for item in _children(client, site_id, p):
                is_folder = "folder" in item
                child = f"{p}/{item['name']}" if p else item["name"]
                print(f"{'  ' * level}{item['name']}{'/' if is_folder else ''}")
                if is_folder and level < depth:
                    walk(child, level + 1)

        print(f"{_env('SHAREPOINT_SITE')}  (default document library)")
        walk(path, 1)
        print("\nSHAREPOINT_INPUTS_PATH is the path to the inputs folder above,")
        print("slash-separated, with real spaces — e.g. 'Signal Agent/inputs'.")


def sync(dry_run: bool = False) -> int:
    """Mirror SharePoint → inputs/. Returns the number of files written."""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        client.headers["Authorization"] = f"Bearer {_token(client)}"
        site_id = _site_id(client)
        remote = dict(_walk(client, site_id, _env("SHAREPOINT_INPUTS_PATH")))
        if not remote:
            raise RuntimeError(
                f"SHAREPOINT_INPUTS_PATH '{_env('SHAREPOINT_INPUTS_PATH')}' "
                "listed zero files — wrong folder path?"
            )

        if dry_run:
            for rel in sorted(remote):
                print(f"  would fetch {rel}")
            return len(remote)

        # Download everything BEFORE deleting anything: a failure part-way
        # through leaves the previous inputs/ fully intact rather than half a
        # mirror. Each file lands via a .tmp + os.replace so a torn download can
        # never be read as a corrupt workbook.
        # downloadUrl is a short-lived pre-authenticated CDN link — it must be
        # fetched WITHOUT the Graph bearer token, hence a second bare client.
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as dl:
            for rel, url in sorted(remote.items()):
                dest = INPUTS_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                with dl.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_bytes():
                            fh.write(chunk)
                os.replace(tmp, dest)

    # Mirror semantics: a file deleted in SharePoint disappears locally too.
    # Only reached once every download above succeeded.
    for local in INPUTS_DIR.rglob("*"):
        if local.is_file() and str(local.relative_to(INPUTS_DIR)) not in remote:
            local.unlink()
            print(f"  removed {local.relative_to(INPUTS_DIR)} (gone from SharePoint)")

    return len(remote)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    # --browse doesn't touch inputs/ and is run by hand during setup, so let it
    # fail loudly rather than swallowing the error the way a scheduled run does.
    if "--browse" in args:
        if not can_authenticate():
            print(f"set these first: {', '.join(_AUTH_VARS)}")
            return 1
        browse()
        return 0

    if not is_configured():
        print("sharepoint sync: not configured — using inputs/ as committed")
        return 0

    try:
        n = sync(dry_run="--dry-run" in args)
    except Exception as e:  # noqa: BLE001 — any failure must be non-fatal
        # A SharePoint blip must never take out the 08:00 digest. Fall back to
        # whatever is already on disk; only refuse if there is nothing to fall
        # back to.
        print(f"WARN: sharepoint sync failed ({type(e).__name__}: {e})")
        if not (INPUTS_DIR / "tuning.xlsx").exists():
            print("ERROR: no local inputs to fall back on")
            return 1
        print("sharepoint sync: continuing on last-known-good inputs/")
        return 0

    print(f"sharepoint sync: {n} file(s) up to date in {INPUTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
