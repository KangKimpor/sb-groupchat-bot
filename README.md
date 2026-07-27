# SingBuildGroupChatBot

A Telegram bot for Singbuild Construction project group chats. One deployment
serves every project group (Urban Village, KFK KMall 2, Norea SuperVilla, ...).

It quietly logs group messages for 10 days, answers questions, and on request
sends you a private recap or a full text export — including voice notes, which
it transcribes and translates to English.

Everything runs on free tiers: Render + Firebase (Firestore + Storage) + the
Gemini API.

## Commands

| Command | What it does |
| --- | --- |
| `/ask <question>` | Answers in the chat. Works in a group or in a private chat. |
| `/summary [group] [today\|week]` | Chat recap. **Always sent to you privately.** |
| `/export [group] [today\|week]` | Full chat log as a `.txt`. **Always sent privately.** |
| `/limits` | How much of your daily quota is left. |
| `/setalias <name>` | Group admins only. Gives the group a short name so you can use `/summary` and `/export` from a private chat. |
| `/help`, `/start` | Command list. |

Defaults to `today` if you don't say `today` or `week`. Inside a group you don't
need to name the group. From a private chat you do: `/summary uvp2 week`.

## Daily limits

Per person, per day, resetting at midnight UTC. Anyone listed in
`ADMIN_USER_IDS` has no limits.

| | Limit |
| --- | --- |
| `/ask` | 20 |
| `/summary` | 5 |
| `/export` | 3 |
| Voice transcriptions | 15 |
| Voice transcriptions, whole group | 200 |

These exist to stay inside the Gemini free tier. Voice notes are only ever
transcribed once — the text is saved, so the same note never costs quota twice.

## How voice notes work

1. Someone sends a voice note. The bot silently saves the audio file. **No AI is
   used at this point** and nothing is posted in the chat.
2. Notes longer than **3 minutes** are ignored completely.
3. The first time a `/summary` or `/export` covers that note, it is transcribed,
   translated to English if needed, and the text is saved for next time.

## Data retention

Messages, transcripts and audio files are deleted after **10 days**. A daily
cleanup job does this (step 7 below).

---

# Setup guide

Assumes no prior experience. Follow in order. Budget about 45 minutes.

You will collect six values along the way. Keep them in a note as you go:

```
TELEGRAM_BOT_TOKEN = ?
GEMINI_API_KEY     = ?
GCS_BUCKET         = ?
WEBHOOK_SECRET     = ?   (you invent this one)
ADMIN_USER_IDS     = ?
RENDER_URL         = ?   (you get this in step 5)
```

## Step 1 — Create the Telegram bot

1. In Telegram, search for **@BotFather** and open the chat. Press **Start**.
2. Send `/newbot`.
3. For the display name, send: `SingBuildGroupChatBot`
4. For the username, send: `SingBuildGroupChatBot`
   - Usernames are globally unique. If it's taken, try
     `SingBuildGroupChat_bot` or `SingBuildGroupChatBot_bot`. Note down whatever
     you actually get — you'll need it later to open a private chat.
5. BotFather replies with a token like `8123456789:AAF...`. That is your
   **`TELEGRAM_BOT_TOKEN`**. Treat it like a password.
6. **Important — turn off privacy mode.** By default a bot cannot see normal
   group messages, only commands, so summaries and exports would come back
   empty. Send `/setprivacy` to BotFather, pick your bot, then choose
   **Disable**. It should confirm privacy mode is disabled.

## Step 2 — Get your own Telegram user ID

1. Search for **@userinfobot** in Telegram and press Start.
2. It replies with your numeric ID, e.g. `123456789`. That is your
   **`ADMIN_USER_IDS`**.
3. For several admins, separate with commas and no spaces: `123456789,987654321`.

## Step 3 — Create the Firebase project

1. Go to <https://console.firebase.google.com> and sign in with a Google account.
2. Click **Create a project**. Name it `sb-groupchat-bot`. Google Analytics is
   not needed — turn it off. Click **Create project**.
3. **Firestore:** in the left menu open **Build → Firestore Database** →
   **Create database**. Choose **Production mode**. Pick a location near
   Cambodia, e.g. `asia-southeast1`. Click **Enable**.
   - Production mode blocks direct access from browsers and phones. The bot uses
     a service account, so it is unaffected. Leave the security rules alone.
4. **Storage:** open **Build → Storage** → **Get started**. Accept the defaults
   and the same location.
   - It shows a bucket name like `sb-groupchat-bot.firebasestorage.app`. That is
     your **`GCS_BUCKET`**. Copy it exactly, with no `gs://` prefix.
5. **Service account key:** click the gear icon (top left) → **Project
   settings** → **Service accounts** tab → **Generate new private key** →
   **Generate key**. A `.json` file downloads.
   - This file is a master key to your database. Never commit it to GitHub,
     never send it over chat. Rename it to `gcp-service-account.json`.

## Step 4 — Get a Gemini API key

1. Go to <https://aistudio.google.com/apikey> and sign in.
2. Click **Create API key**, and choose the Firebase project from step 3.
3. Copy the key. That is your **`GEMINI_API_KEY`**.

## Step 5 — Put the code on GitHub, then deploy to Render

### 5a. GitHub

1. Go to <https://github.com/new>. Repository name: `sb-groupchat-bot`. Set it to
   **Private**. Create it.
2. Upload these files (drag and drop works, via **Add file → Upload files**):
   `bot.py`, `firestore_db.py`, `gemini_client.py`, `requirements.txt`,
   `render.yaml`, `.gitignore`, `.env.example`, `README.md`, `test_bot.py`.
3. **Do not upload `gcp-service-account.json`.** It goes to Render directly in
   step 5b.

### 5b. Render

1. Go to <https://render.com> and sign up with your GitHub account.
2. Click **New → Blueprint**, connect GitHub, and pick `sb-groupchat-bot`.
   - Use **Blueprint**, not "Web Service". Blueprint is the option that actually
     reads `render.yaml`, so the plan, build command and start command configure
     themselves.
   - If you'd rather create it as **New → Web Service** by hand, set
     **Build Command** to `pip install -r requirements.txt`, **Start Command** to
     `python bot.py`, instance type **Free**, and then also add the three
     variables `GEMINI_MODEL=gemini-3.5-flash-lite`,
     `LOCAL_UTC_OFFSET_HOURS=7` and
     `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/gcp-service-account.json`
     alongside the table below.
3. Render prompts for the values marked `sync: false` in `render.yaml`. Fill them
   in now, or add them afterwards under the service's **Environment** tab:

   | Key | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | from step 1 |
   | `GEMINI_API_KEY` | from step 4 |
   | `GCS_BUCKET` | from step 3 |
   | `WEBHOOK_SECRET` | invent a long random string, e.g. 30 mixed characters, no spaces or `/` |
   | `ADMIN_USER_IDS` | from step 2 |

   `GEMINI_MODEL`, `LOCAL_UTC_OFFSET_HOURS` and
   `GOOGLE_APPLICATION_CREDENTIALS` already have correct values from
   `render.yaml`. Leave them.

4. Confirm and let it deploy (**Apply**, or **Create Web Service** if you went the
   manual route). The first build takes a few minutes.
5. Now add the service account key. Open the service → **Environment** →
   **Secret Files** → **Add Secret File**:
   - Filename: `gcp-service-account.json` (exactly this — `render.yaml` points
     `GOOGLE_APPLICATION_CREDENTIALS` at `/etc/secrets/gcp-service-account.json`)
   - Contents: open the JSON file from step 3 in a text editor, copy everything,
     paste it in. Save.
   - Saving a secret file redeploys the service. That's expected — the first
     deploy will have failed to reach Firestore without this, which is fine.
6. Wait for the status to read **Live**, then copy the URL at the top, e.g.
   `https://sb-groupchat-bot.onrender.com`. That is your **`RENDER_URL`**.
7. Check it: open `RENDER_URL` in a browser. It should show
   `sb-groupchat-bot ok`. If it doesn't, open **Logs** and look at the first few
   lines for a missing variable.

## Step 6 — Point Telegram at Render

Telegram needs to know where to deliver messages. This is a one-time step.

Paste this into your browser's address bar, replacing the three parts, then
press Enter:

```
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=<RENDER_URL>/webhook/<WEBHOOK_SECRET>
```

Worked example (with fake values):

```
https://api.telegram.org/bot8123456789:AAFxxxx/setWebhook?url=https://sb-groupchat-bot.onrender.com/webhook/my-long-random-secret
```

You should see `{"ok":true,"result":true,"description":"Webhook was set"}`.

Note there is no `<` or `>` in the final URL, and the token keeps its colon.

## Step 7 — Set up the daily cleanup

Render's free plan has no scheduler, so use a free external one.

1. Go to <https://cron-job.org> and create a free account.
2. Click **Create cronjob**.
3. URL: `<RENDER_URL>/cleanup?key=<WEBHOOK_SECRET>`
4. Schedule: every day at a quiet hour, e.g. 19:00 (that's 02:00 in Cambodia).
5. Save and press **Test run** once. It should return `deleted 0`.

## Step 8 — Add the bot to each project group

Do this for every Singbuild group.

1. Open the group in Telegram → group name → **Add members** → search your bot's
   username → add it.
2. **Make it an admin.** Group name → **Administrators** → **Add
   administrator** → pick the bot. It doesn't need any special powers; admin
   status just makes message visibility reliable. Leave the defaults.
3. In the group, send a short name for it:

   ```
   /setalias uvp2
   ```

   The bot confirms. Now anyone in that group can message the bot privately and
   run `/summary uvp2 week`.
4. Repeat per group with a different alias each time — e.g. `uvp2`, `kfk2`,
   `norea`.

## Step 9 — Tell the team one thing

For a private recap to reach someone, **they must have started a private chat
with the bot at least once**. Telegram forbids bots from messaging people first.

Ask everyone to search the bot's username, open it, and press **Start**. One
time, and that's it.

## Step 10 — Check it works

1. In a group, send a few normal messages.
2. Send `/ask what is the standard curing time for concrete` — it answers in the
   group.
3. Send a short voice note. Nothing visible happens. That's correct.
4. Send `/summary`. The group gets "Working on it", and the recap arrives in your
   private chat with the voice note included as text.
5. Send `/export week` — a `.txt` file arrives privately.
6. Send `/limits` — shows your remaining quota.

---

# Notes and troubleshooting

## The first message after a quiet spell is slow

Render's free tier puts the service to sleep after 15 minutes of no traffic. The
next Telegram message wakes it, which takes a few seconds. Nothing is lost, it's
just briefly slow. Normal.

## The bot does nothing at all

1. Check the webhook. Open in a browser:
   `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
   - `"url"` must exactly match your Render URL plus `/webhook/<secret>`.
   - Look at `last_error_message`. If it mentions a timeout, hit your Render URL
     once in a browser to wake the service, then send another message.
2. Check Render → your service → **Logs** for errors on startup. A missing env
   var shows up as `KeyError` in the first few lines.
3. Confirm the service is **Live**, not **Failed** or **Suspended**.

## Commands work but `/summary` and `/export` come back empty

Privacy mode is still on, so the bot never saw the normal messages. Redo step
1.6 (`/setprivacy` → Disable), then send some new messages. Only messages sent
*after* the fix are recorded.

## "I couldn't message you privately"

That person hasn't started a private chat with the bot. See step 9.

## Voice notes show `[voice transcription failed]`

- Check `GEMINI_API_KEY` in Render.
- Check the Render logs for the real error.
- If it says the model was not found, Google has retired it. Search for the
  current Gemini flash model name and update the `GEMINI_MODEL` env var in
  Render. No code change needed — that's why it's a variable.

## Voice notes show `[voice transcription limit reached for today]`

Expected once someone passes 15 transcriptions, or the group passes 200, in one
day. Resets at midnight UTC. Add yourself to `ADMIN_USER_IDS` to bypass.

## A long voice note was ignored

Anything over 3 minutes is skipped on purpose, to stay inside the free tiers.
Ask for shorter notes, or raise `MAX_VOICE_SECONDS` in `bot.py` knowing it costs
more quota.

## "today" looks like it covers the wrong hours

`LOCAL_UTC_OFFSET_HOURS` in Render decides when "today" starts. It ships as `7`
for Cambodia. Daily quotas always reset at midnight UTC regardless.

## Running the tests

```bash
pip install -r requirements.txt
python3 -m unittest test_bot.py -v
```

No credentials needed — Firestore and Gemini are stubbed. The important test
pushes several webhook requests through in one process, which is what catches
event-loop regressions.

## Running locally

```bash
cp .env.example .env          # then fill in the real values
pip install -r requirements.txt
set -a && source .env && set +a
python3 bot.py
```

Telegram needs a public HTTPS URL, so local runs can't receive real messages
unless you tunnel (e.g. ngrok) and point the webhook at the tunnel. Easiest is
to test on Render.

## Design notes for whoever edits this next

- Three code files on purpose. `bot.py` is the only file that touches Telegram
  or Flask, `firestore_db.py` is the only file that touches Google storage,
  `gemini_client.py` is the only file that calls the AI. Don't add a
  handlers/services/repository split for one caller.
- **Never** replace the shared event loop in `bot.py` with `asyncio.run()` inside
  the webhook route. `asyncio.run()` destroys its loop on return, and
  python-telegram-bot binds internals to the loop it was initialised on. It
  fails on the *second* message the bot receives, not the first, so it looks
  fine in casual testing.
- The Flask server is single-threaded deliberately, because that shared loop is
  not thread-safe. Adding gunicorn or `threaded=True` means switching to
  `asyncio.run_coroutine_threadsafe`.
- Every Firestore query here is single-field, and the only range query orders by
  the same field it filters on, so no composite indexes are required. Filtering
  on one field while ordering by another would need an index created by hand.
- Use the `google-genai` package. `google-generativeai` is dead (end of life
  2025-11-30).
