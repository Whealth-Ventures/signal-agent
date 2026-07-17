// Trigger a deploy after an admin save so edits reach the box.
//
// The box runs the inputs/prompts that shipped in the last deploy and never
// talks to GitHub (push model). So a commit alone doesn't go live — a deploy
// (Jenkins pipeline: package the new commit -> S3 -> SSM to the box) must run.
// After a successful commit we ping Jenkins to kick that pipeline.
//
// Config (set in the admin secret; deploy.sh materializes it into
// .env.production automatically):
//   DEPLOY_TRIGGER_URL   Jenkins build URL, e.g.
//                        https://<jenkins>/job/signal-agent/job/main/build
//                        or a buildByToken URL. Unset => auto-deploy disabled
//                        (Save still commits; you deploy manually).
//   DEPLOY_TRIGGER_TOKEN optional — appended as ?token=… (Jenkins "trigger
//                        builds remotely" token / buildByToken).
//   DEPLOY_TRIGGER_AUTH  optional — "user:apiToken" for Basic auth (Jenkins
//                        API-token auth is CSRF-crumb exempt).

export type TriggerRequest = { target: string; headers: Record<string, string> };

// Pure assembly so it can be unit-checked without network. Exported for the
// check in deploy.check.mjs.
export function buildTriggerRequest(
  url: string,
  token?: string,
  auth?: string,
): TriggerRequest {
  const target = token
    ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token)
    : url;
  const headers: Record<string, string> = {};
  if (auth) headers["Authorization"] = "Basic " + Buffer.from(auth).toString("base64");
  return { target, headers };
}

export type DeployResult = { triggered: boolean; detail: string };

export async function triggerDeploy(): Promise<DeployResult> {
  const url = process.env.DEPLOY_TRIGGER_URL;
  if (!url) return { triggered: false, detail: "auto-deploy not configured" };

  const { target, headers } = buildTriggerRequest(
    url,
    process.env.DEPLOY_TRIGGER_TOKEN,
    process.env.DEPLOY_TRIGGER_AUTH,
  );
  const res = await fetch(target, { method: "POST", headers });
  // Jenkins returns 201 (queued); accept any 2xx to be tolerant of proxies.
  if (!res.ok && res.status !== 201) {
    const body = (await res.text().catch(() => "")).slice(0, 200);
    throw new Error(`deploy trigger failed: ${res.status} ${body}`.trim());
  }
  return { triggered: true, detail: `deploy queued (HTTP ${res.status})` };
}
