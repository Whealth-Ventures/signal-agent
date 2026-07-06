// Feedback event store, backed by S3 (replaces the former Vercel Blob store).
// Objects live under events/YYYY-MM-DD/ in the bucket named by FEEDBACK_S3_BUCKET.
// Credentials come from the ambient AWS role (EC2 instance profile in prod).
import {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
  ListObjectsV2Command,
} from "@aws-sdk/client-s3";

const REGION = process.env.AWS_REGION || "ap-south-1";
const BUCKET = process.env.FEEDBACK_S3_BUCKET || "";

let _s3: S3Client | null = null;
function s3(): S3Client {
  if (!BUCKET) throw new Error("FEEDBACK_S3_BUCKET env var not set");
  if (!_s3) _s3 = new S3Client({ region: REGION });
  return _s3;
}

export type FeedbackEvent = {
  received_at: string;
  event_id: string;
  team_id?: string;
  api_app_id?: string;
  type: string;
  event: unknown;
};

export async function appendFeedbackEvent(ev: FeedbackEvent): Promise<void> {
  const day = ev.received_at.slice(0, 10);
  const safeId = ev.event_id.replace(/[^A-Za-z0-9_-]/g, "_");
  const key = `events/${day}/${ev.received_at}-${safeId}.json`;
  await s3().send(
    new PutObjectCommand({
      Bucket: BUCKET,
      Key: key,
      Body: JSON.stringify(ev),
      ContentType: "application/json",
    }),
  );
}

export type ReactionSummary = {
  received_at: string;
  type: string; // reaction_added / reaction_removed / ...
  reaction: string | null; // e.g. "+1", "thumbsdown"
  user: string | null;
  slack_ts: string | null;
  slack_channel: string | null;
};

// Slack emoji names we treat as a thumbs-up / thumbs-down (mirrors the agent's
// feedback_aggregator polarity sets so the admin shows the same signal).
const POSITIVE = new Set(["+1", "thumbsup", "heart", "fire", "white_check_mark", "100"]);
const NEGATIVE = new Set(["-1", "thumbsdown", "x", "no_entry_sign"]);

export function reactionPolarity(reaction: string | null): "up" | "down" | "other" {
  if (!reaction) return "other";
  if (POSITIVE.has(reaction)) return "up";
  if (NEGATIVE.has(reaction)) return "down";
  return "other";
}

/**
 * Read the most recent Slack-reaction events straight from S3 (the same store
 * the events webhook writes to). This is the immediate-visibility path: it
 * doesn't depend on the agent's daily cron, so a reaction shows up here within
 * seconds of being added in Slack.
 */
export async function listRecentFeedback(
  limit = 50,
): Promise<{ total: number; recent: ReactionSummary[] }> {
  const client = s3();

  // 1) Enumerate every event object (paginating through the store).
  const all: { key: string; lastModified: Date }[] = [];
  let token: string | undefined;
  do {
    const res = await client.send(
      new ListObjectsV2Command({
        Bucket: BUCKET,
        Prefix: "events/",
        ContinuationToken: token,
      }),
    );
    for (const o of res.Contents || []) {
      if (o.Key) all.push({ key: o.Key, lastModified: o.LastModified || new Date(0) });
    }
    token = res.IsTruncated ? res.NextContinuationToken : undefined;
  } while (token);

  // 2) Newest first, then read the content of just the top `limit`.
  all.sort((a, b) => b.lastModified.getTime() - a.lastModified.getTime());
  const recent: ReactionSummary[] = [];
  for (const b of all.slice(0, limit)) {
    try {
      const g = await client.send(
        new GetObjectCommand({ Bucket: BUCKET, Key: b.key }),
      );
      const text = await g.Body?.transformToString();
      if (!text) continue;
      const ev: any = JSON.parse(text);
      const inner = ev?.event || {};
      const item = inner?.item || {};
      recent.push({
        received_at: ev?.received_at || b.lastModified.toISOString(),
        type: ev?.type || inner?.type || "unknown",
        reaction: inner?.reaction ?? null,
        user: inner?.user ?? null,
        slack_ts: item?.ts ?? inner?.ts ?? null,
        slack_channel: item?.channel ?? inner?.channel ?? null,
      });
    } catch {
      // Skip an object we can't read rather than failing the whole list.
    }
  }
  return { total: all.length, recent };
}
