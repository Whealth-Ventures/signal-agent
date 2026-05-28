import { Resend } from "resend";

export async function sendMagicLink(email: string, link: string): Promise<void> {
  const apiKey = process.env.RESEND_API_KEY;
  const from = process.env.RESEND_FROM;
  if (!apiKey || !from) {
    // Bootstrap fallback: when Resend isn't configured yet, log the link so
    // an operator with access to server logs (`vercel logs`) can still sign
    // in. Once RESEND_API_KEY + RESEND_FROM are set the real email path
    // takes over and this branch goes away.
    console.warn(
      `[magic-link] Resend not configured. Sign-in link for ${email}:\n${link}`,
    );
    return;
  }
  const resend = new Resend(apiKey);
  const { error } = await resend.emails.send({
    from,
    to: email,
    subject: "Sign in to Signal Agent admin",
    text: [
      "Click the link below to sign in. It's valid for 15 minutes.",
      "",
      link,
      "",
      "If you didn't request this, you can ignore this email.",
    ].join("\n"),
    html: `
      <p>Click the link below to sign in. It's valid for 15 minutes.</p>
      <p><a href="${link}" style="display:inline-block;padding:10px 16px;background:#111;color:#fff;border-radius:6px;text-decoration:none">Sign in</a></p>
      <p style="color:#666;font-size:13px">If the button doesn't work, paste this URL into your browser:<br>${link}</p>
      <p style="color:#999;font-size:12px">If you didn't request this, you can ignore this email.</p>
    `,
  });
  if (error) {
    throw new Error(`Resend send failed: ${error.message}`);
  }
}
