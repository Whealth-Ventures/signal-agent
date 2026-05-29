import { put } from "@vercel/blob";

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
