# SharePoint sync — one-time Azure setup

`src/sharepoint_sync.py` mirrors the SharePoint inputs folder into `inputs/` at
the top of every run. To do that it needs to read one SharePoint site with no
human present, which means an **Azure AD (Microsoft Entra) app registration**
holding **app-only** Microsoft Graph permission.

There are five steps. Steps 3 and 4 need someone with admin rights — if that
isn't you, steps 1, 2 and 5 are still yours and you hand 3–4 to IT.

Budget ~20 minutes.

---

## Before you start

You need:

- Access to <https://entra.microsoft.com> with your work account.
- A **Global Administrator** or **Privileged Role Administrator** for step 3
  (granting tenant consent). Check who that is in your org before you begin —
  it's the one thing that can stall this for days.
- **Site owner / SharePoint admin** rights on the site for step 4.

---

## Step 1 — Create the app registration

1. Go to <https://entra.microsoft.com> and sign in.
2. Left nav: **Applications → App registrations**.
3. Click **+ New registration**.
4. Fill in:
   - **Name**: `signal-agent-sharepoint`
   - **Supported account types**: *Accounts in this organizational directory
     only (Single tenant)* — the first option.
   - **Redirect URI**: **leave blank.** There is no browser login here; the
     agent authenticates as itself with a secret.
5. Click **Register**.

You land on the app's **Overview** page. Copy these two values now — you'll need
them at the end:

| Overview field | Env var |
|---|---|
| **Application (client) ID** | `SHAREPOINT_CLIENT_ID` |
| **Directory (tenant) ID** | `SHAREPOINT_TENANT_ID` |

Both are GUIDs. Neither is secret — they're identifiers.

---

## Step 2 — Create a client secret

This is the app's password.

1. In the app, left nav: **Certificates & secrets**.
2. **Client secrets** tab → **+ New client secret**.
3. **Description**: `signal-agent` · **Expires**: pick the longest offered
   (usually 24 months).
4. Click **Add**.

**Copy the `Value` column immediately.** Not `Secret ID` — `Value`. It is shown
exactly once; navigate away and it's gone forever and you make a new one.

That value is `SHAREPOINT_CLIENT_SECRET`.

> **Put a calendar reminder on the expiry date now.** When the secret expires the
> sync starts failing — and because failure is deliberately non-fatal, the digest
> keeps shipping on stale inputs rather than erroring loudly. You'd notice only
> by spotting `WARN: sharepoint sync failed` in the logs, or by wondering why a
> SharePoint edit never showed up.

---

## Step 3 — Add the Graph permission (needs a tenant admin)

1. In the app, left nav: **API permissions**.
2. **+ Add a permission** → **Microsoft Graph**.
3. Choose **Application permissions**. ← *This is the step people get wrong.*
   Delegated permissions act on behalf of a signed-in user; there is no user
   here. It must be **Application**.
4. Search `Sites.Selected`, tick it, click **Add permissions**.
5. Optionally remove the `User.Read` delegated permission Azure adds by default
   — the agent never uses it.
6. Click **Grant admin consent for \<your tenant\>** → **Yes**.

Check the **Status** column now reads a green ✅ *Granted for \<tenant\>*. If it
still says "Not granted", consent didn't happen and nothing downstream will work.

**What `Sites.Selected` means:** by itself it grants access to *nothing*. It only
makes the app *eligible* to be given access to individual sites, one at a time,
in step 4. That's why it's the safe choice — compare `Sites.Read.All`, which
would let this app read every SharePoint site in the company.

---

## Step 4 — Grant the app read on your one site

`Sites.Selected` has to be pointed at a specific site, and **this can only be
done over the Graph API — there is no button for it in the Azure or SharePoint
portal.** Hence the helper script.

From a machine where an admin is signed into the Azure CLI:

```bash
az login          # sign in as a site admin / SharePoint admin
.venv/bin/python scripts/grant_sharepoint_access.py \
    --site 2070health.sharepoint.com:/sites/SignalAgent \
    --client-id <Application (client) ID from step 1>
```

It prints the resolved site id, performs the grant, and lists every app that now
has access to that site so you can confirm yours is there with `['read']`.

Re-run any time with `--list` (and no `--client-id`) to just inspect:

```bash
.venv/bin/python scripts/grant_sharepoint_access.py --site <site> --list
```

**If `az login` isn't available**, the equivalent in PnP PowerShell, run by a
SharePoint admin:

```powershell
Connect-PnPOnline -Url https://2070health.sharepoint.com/sites/SignalAgent -Interactive
Grant-PnPAzureADAppSitePermission -AppId <client-id> -DisplayName "signal-agent-sharepoint" -Permissions Read
```

---

## Step 5 — Find the site and folder values

### `SHAREPOINT_SITE`

A SharePoint **site** is a container — one team's or project's workspace, with
its own document libraries and membership. You don't create one for this; the
inputs already live in an existing site. Find its name in any URL pointing at
the folder:

```
https://2070health.sharepoint.com/sites/NewVentures/Shared%20Documents/...
        └──────── host ─────────┘└──── site ──────┘
```

→ `SHAREPOINT_SITE=2070health.sharepoint.com:/sites/NewVentures`

Host, then a **colon**, then the `/sites/...` part. No `https://`.

> A **"Copy link" sharing URL** looks different — `https://host/:f:/s/NewVentures/IgAdiCkK...?e=X`.
> The `:f:` means "folder link" and `/s/` is shorthand for `/sites/`, so the site
> name is still readable. The long token after it is opaque and tells you nothing
> about the path — see below.

### `SHAREPOINT_INPUTS_PATH`

This is the path **inside the site's default document library**, slash-separated,
with real spaces (not `%20`), and *without* the library name (`Shared Documents`
/ `Documents`) at the front. If the files sit in the library root, use the folder
name alone.

Once steps 1–4 are done, the reliable way to get it is to ask:

```bash
.venv/bin/python src/sharepoint_sync.py --browse
```

That prints the folder tree; read the path straight off it.

Before you have credentials, get it from the browser instead: open the folder,
then look at the address bar — it will contain
`...Forms/AllItems.aspx?id=%2Fsites%2FNewVentures%2FShared%20Documents%2FSignal%20Agent%2Finputs`.
URL-decode the `id=` value (`%2F` → `/`, `%20` → space), then drop the
`/sites/<Name>/<Library>/` prefix. The breadcrumb above the file list shows the
same thing in readable form.

> **Caveat:** the sync reads the site's **default** document library. If your
> inputs live in a *second*, separately-created library, say so — it's a one-line
> change to target a named library, but it isn't handled today.

---

## Step 6 — Wire the values in

Five values total. Put them in two places.

**Local `.env`** (for running the sync from your laptop):

```
SHAREPOINT_TENANT_ID=<Directory (tenant) ID>
SHAREPOINT_CLIENT_ID=<Application (client) ID>
SHAREPOINT_CLIENT_SECRET=<the Value from step 2>
SHAREPOINT_SITE=2070health.sharepoint.com:/sites/SignalAgent
SHAREPOINT_INPUTS_PATH=Signal Agent/inputs
```

**AWS Secrets Manager**, so the production box gets them — same five keys, added
to the existing agent secret. `deploy/deploy.sh` materializes every key in that
secret into the box's env automatically, so nothing else needs changing.

---

## Verify

```bash
.venv/bin/python src/sharepoint_sync.py --dry-run
```

Expected: a list of every file it would fetch. Then a real run:

```bash
.venv/bin/python src/sharepoint_sync.py
```

Expected: `sharepoint sync: N file(s) up to date in .../inputs`.

Now prove the loop end to end: change a cell in `tuning.xlsx` **in SharePoint**,
save, re-run the sync, and confirm the change is in your local copy.

### When it doesn't work

| Symptom | Cause |
|---|---|
| `401` / `invalid_client` on the token call | Wrong secret, or you copied `Secret ID` instead of `Value`. |
| `403` listing the folder | Step 3 consent missing, or step 4 site grant missing. Run the `--list` check. |
| `listed zero files — wrong folder path?` | `SHAREPOINT_INPUTS_PATH` is off, or the files are in a non-default document library. |
| `could not resolve site` | `SHAREPOINT_SITE` format — needs the `host:/sites/Name` colon form, no `https://`. |
| `not configured` | One of the five vars is empty. All five are required. |

---

## Rotating the secret later

Repeat **step 2 only** — new secret, copy the `Value`, update `.env` and Secrets
Manager. The app registration, the consent, and the site grant all stay as they
are. Old secrets can be deleted from the same screen once the new one works.
