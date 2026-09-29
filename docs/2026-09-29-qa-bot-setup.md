# Q&A bot (Phase B): setup you do

**About 15 minutes, all in the Slack app settings and AWS. After this, the Signal Agent bot can answer questions from the news archive in Slack.**

The bot is the existing Signal Agent app (`@signal_agent`, workspace 2070Health), not a new one. It keeps posting the digests exactly as today. The code (`src/bot.py`, its systemd service, the deploy change) is built separately on `feat/subhanu-qa-bot`.

What it will do once live:

- Answer `@signal_agent <question>` in any channel it's in, in a thread.
- Answer direct messages.
- Search the labelled archive, and reply with a short answer plus the links it actually used.

## 1. Slack app settings

Open [api.slack.com/apps](https://api.slack.com/apps) and pick the **Signal Agent** app.

### 1a. Turn on Socket Mode

Socket Mode lets the bot receive messages over an outbound connection, so the server needs no public URL.

1. **Socket Mode** (left menu): switch **Enable Socket Mode** on.
2. When asked for an app-level token, name it `socket`, add the scope **`connections:write`**, then **Generate**.
3. Copy the token. It starts with `xapp-`. This is `SLACK_APP_TOKEN` for step 2.

You can find it again later under **Basic Information → App-Level Tokens**.

### 1b. Subscribe to the two events

1. **Event Subscriptions**: switch **Enable Events** on. There's no Request URL to fill in with Socket Mode.
2. Under **Subscribe to bot events**, add:
   - `app_mention`, for `@signal_agent` in a channel
   - `message.im`, for a direct message to the bot
3. **Delete the three older events** (trash icon): `message.channels`, `reaction_added` and `reaction_removed`.
   - They fed the Slack reaction-feedback loop, which was removed in commit `1a13b2b` together with its receiver (`admin/app/api/slack/events/route.ts`).
   - Once Socket Mode is on, they would push every message and every reaction in the bot's channels to the bot, all for nothing.
   - Deleting an event doesn't remove its scope. Keep `channels:history`, which the bot uses to read thread context.
4. **Save Changes**.

### 1c. Allow direct messages

1. **App Home** → **Show Tabs**: turn on **Messages Tab**.
2. Tick **Allow users to send Slash commands and messages from the messages tab**.

Without this, a DM to the bot shows "Sending messages to this app has been turned off."

### 1d. Add four bot scopes

**OAuth & Permissions** → **Scopes** → **Bot Token Scopes**. The app already has `chat:write`, `channels:history`, `reactions:read`, `users:read` and `incoming-webhook`, so leave those. Add:

| Scope | Why |
|---|---|
| `app_mentions:read` | See `@signal_agent` mentions |
| `im:read` | Receive direct messages |
| `im:history` | Read the earlier messages in a DM thread |
| `groups:history` | Read the earlier messages in a thread in a private channel |

### 1e. Reinstall the app

Slack shows a yellow banner after scope changes. Click **Reinstall to Workspace**, then **Allow**.

The bot token (`xoxb-…`) normally stays the same. If Slack shows a new one on **OAuth & Permissions**, the secret has to be updated too (step 2).

### 1f. Make a test channel

Create a private channel, for example `#signal-agent-bot-test`. Invite the bot:

```
/invite @signal_agent
```

The first tests happen there rather than in the digest channels. The bot is already a member of the three digest channels, because it posts there.

## 2. Secrets

Add the app token everywhere the agent reads secrets. **Paste the bare value, with no quote marks.** On 29 September, `DATABASE_URL` went in with quotes and the box couldn't connect.

1. **AWS Secrets Manager** (region ap-south-1) → `signal-agent/prod/agent-env` → **Retrieve secret value** → **Edit** → **Add row**:
   - key `SLACK_APP_TOKEN`
   - value `xapp-…`
2. **Local `.env.production`**: add `SLACK_APP_TOKEN=xapp-…`, so the bot can be tested locally before deploy.
3. If the reinstall issued a new bot token, update `SLACK_BOT_TOKEN` in both places too.

Don't paste either token into chat.

## 3. Tell Claude "done"

Claude then checks, without printing any token:

- the bot token still works (`auth.test`) and now has the new scopes
- the app token can open a Socket Mode connection (`apps.connections.open`)
- the box has `SLACK_APP_TOKEN` after the next deploy

## Worth knowing

- **One Socket Mode connection per app is safest.** While the bot runs on the box, a local test copy running at the same time would get about half the messages. Local testing therefore happens before the first deploy, or with the box service stopped.
- **Cost:** an answer uses OpenAI, the same key as labelling, at about 3 to 4 cents a question.
- **Scope of answers:** the labelled archive, currently 14 September onwards, growing daily. Older stories can be backfilled for about $3.
