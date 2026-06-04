import { put, list, get } from "@vercel/blob";

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
  const pathname = `events/${day}/${ev.received_at}-${safeId}.json`;
  await put(pathname, JSON.stringify(ev), {
    access: "private",
    addRandomSuffix: false,
    contentType: "application/json",
  });
}

export type ReactionSummary = {
  received_at: string;
  type: string;              // reaction_added / reaction_removed / ...
  reaction: string | null;   // e.g. "+1", "thumbsdown"
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
 * Read the most recent Slack-reaction events straight from Vercel Blob (the
 * same store the events webhook writes to). This is the immediate-visibility
 * path: it doesn't depend on the agent's daily cron, so a reaction shows up
 * here within seconds of being added in Slack.
 */
export async function listRecentFeedback(
  limit = 50,
): Promise<{ total: number; recent: ReactionSummary[] }> {
  // 1) Enumerate every event blob (paginating through the store).
  const all: { pathname: string; uploadedAt: Date }[] = [];
  let cursor: string | undefined;
  do {
    const res = await list({ prefix: "events/", cursor, limit: 1000 });
    for (const b of res.blobs) {
      all.push({ pathname: b.pathname, uploadedAt: b.uploadedAt });
    }
    cursor = res.hasMore ? res.cursor : undefined;
  } while (cursor);

  // 2) Newest first, then read the content of just the top `limit`.
  all.sort((a, b) => b.uploadedAt.getTime() - a.uploadedAt.getTime());
  const recent: ReactionSummary[] = [];
  for (const b of all.slice(0, limit)) {
    try {
      const g = await get(b.pathname, { access: "private" });
      if (!g || g.statusCode !== 200) continue;
      const ev: any = await new Response(g.stream).json();
      const inner = ev?.event || {};
      const item = inner?.item || {};
      recent.push({
        received_at: ev?.received_at || b.uploadedAt.toISOString(),
        type: ev?.type || inner?.type || "unknown",
        reaction: inner?.reaction ?? null,
        user: inner?.user ?? null,
        slack_ts: item?.ts ?? inner?.ts ?? null,
        slack_channel: item?.channel ?? inner?.channel ?? null,
      });
    } catch {
      // Skip a blob we can't read rather than failing the whole list.
    }
  }
  return { total: all.length, recent };
}
