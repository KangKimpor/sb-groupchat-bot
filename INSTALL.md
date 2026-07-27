# Installing SingBuildGroupChatBot — step-by-step

## How to use this file

Hand this whole file to an AI assistant and ask it to walk you through the
install one step at a time. It contains everything needed; the assistant should
not have to guess or search.

**Assistant: please read this first.**

- Take the steps **in order**, one at a time. After each one, ask me to paste
  back the result described under **Check it worked** before moving on.
- Every step is done in a browser or in Telegram except step 5a and the optional
  local test. There is no server to configure and no command line required for a
  normal install.
- I am on **Windows / PowerShell**. Use PowerShell syntax for any command, and
  use `;` rather than `&&` to join commands.
- Nine values get collected along the way. Keep the running list visible in your
  replies so I don't lose track, and **never echo the full token or key back to
  me in plain text once it's stored** — refer to them by name.
- If something doesn't match what this file predicts, stop and diagnose rather
  than continuing. The **Troubleshooting** section at the end maps symptoms to
  causes.
- Budget about 45 minutes.

## What you're deploying

A Telegram bot that sits in Singbuild's project group chats. One deployment
serves every group. It logs group messages for 10 days, answers `/ask` questions,
and privately DMs recaps (`/summary`) and full text exports (`/export`),
including voice notes transcribed and translated to English.

Three services, all on free plans, no payment method anywhere:

| Service | Role | Plan |
| --- | --- | --- |
| Render | runs the bot, receives Telegram webhooks | Free web service |
| Firebase Firestore | stores messages, group registry, daily counters | Spark (no cost) |
| Gemini API | answers, summaries, voice transcription | Free tier |
| cron-job.org | triggers the daily 10-day cleanup | Free |

**This cannot bill you.** With no payment method attached, each service refuses
work instead of charging: Render suspends free services, Firestore returns errors
once a daily quota is spent, Gemini returns rate-limit errors. Two things would
change that, so don't do either — enable **Cloud Storage for Firebase**, or
attach a billing account to the Firebase project (which would also drop the
Gemini API off its free tier).

One privacy tradeoff to decide on before you start: on the Gemini API **free
tier**, Google states content may be used to improve their products. If a
particular project's chat can't be handled that way, don't put that group on
this bot.

## Values you'll collect

Keep these in a note as you go. Steps that produce them are shown.

```
TELEGRAM_BOT_TOKEN    = ?   step 1
BOT_USERNAME          = ?   step 1
ADMIN_USER_IDS        = ?   step 2
GEMINI_API_KEY        = ?   step 4
WEBHOOK_SECRET        = ?   step 5 (you generate this)
CLEANUP_KEY           = ?   step 5 (you generate this, different again)
WEBHOOK_HEADER_SECRET = ?   step 5 (you generate this, different again)
RENDER_URL            = ?   step 5
```

Generate the three secrets now, in PowerShell. Run this three times and keep
each result separately:

```powershell
-join ((48..57) + (65..90) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
```

On macOS or Linux:

```bash
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40; echo
```

They must be different from each other, and letters and digits only — no slashes
or spaces, because two of them go inside URLs.

---

## Step 1 — Create the Telegram bot

1. In Telegram, search for **@BotFather** and open the chat. Press **Start**.
2. Send `/newbot`.
3. For the display name, send: `SingBuildGroupChatBot`
4. For the username, send: `SingBuildGroupChatBot`
   - Usernames are globally unique. If it's taken, try
     `SingBuildGroupChat_bot` or `SingBuildGroupChatBot_bot`. **Write down
     whichever you actually get as `BOT_USERNAME`** — you need it later to open a
     private chat and to add the bot to groups.
5. BotFather replies with a token like `8123456789:AAF...`. That is your
   **`TELEGRAM_BOT_TOKEN`**. Treat it like a password.
6. **Turn off privacy mode.** By default a bot cannot see normal group messages,
   only commands, so summaries and exports would come back empty. Send
   `/setprivacy` to BotFather, pick your bot, then choose **Disable**.

**Check it worked:** BotFather has confirmed *"Privacy mode is disabled"* for
your bot, and you have a token and a username written down.

---

## Step 2 — Get your own Telegram user ID

1. Search for **@userinfobot** in Telegram and press Start.
2. It replies with your numeric ID, e.g. `123456789`. That is your
   **`ADMIN_USER_IDS`**.
3. For several admins, separate with commas and no spaces: `123456789,987654321`.

Admins bypass every daily limit, so keep this list short.

**Check it worked:** you have a number, not a username.

---

## Step 3 — Create the Firebase project

1. Go to <https://console.firebase.google.com> and sign in with a Google account.
2. Click **Create a project**. Name it `sb-groupchat-bot`. Google Analytics is
   not needed — turn it off. Click **Create project**.
3. **Firestore:** in the left menu open **Build → Firestore Database** →
   **Create database**. Choose **Production mode**. Pick a location near
   Cambodia, e.g. `asia-southeast1`. Click **Enable**.
   - Production mode blocks direct access from browsers and phones. The bot uses
     a service account, so it is unaffected. Leave the security rules alone.
4. **Ignore Firebase Storage.** You do not need it, and you should not enable
   it. Since September 2024 it requires the paid Blaze plan, and linking a
   billing account to this project would also drop it off the Gemini API free
   tier. Stay on the **Spark (no cost)** plan.
   - This bot stores no audio. Voice notes already live on Telegram's servers,
     and the bot keeps only a reference to them.
5. **Service account key:** click the gear icon (top left) → **Project
   settings** → **Service accounts** tab → **Generate new private key** →
   **Generate key**. A `.json` file downloads.
   - This file is a master key to your database. Never commit it to GitHub,
     never send it over chat, never paste its contents into a chat with an AI
     assistant. Rename it to `gcp-service-account.json`.

**Check it worked:** Firestore shows an empty database, the project header says
**Spark** (not Blaze), and you have `gcp-service-account.json` saved locally.

---

## Step 4 — Get a Gemini API key

1. Go to <https://aistudio.google.com/apikey> and sign in.
2. Click **Create API key**, and choose the Firebase project from step 3.
3. Copy the key. That is your **`GEMINI_API_KEY`**.

**Check it worked:** AI Studio shows the key attached to the `sb-groupchat-bot`
project, and the project is on the free tier (no billing prompt appeared).

---

## Step 5 — Put the code on GitHub, then deploy to Render

### 5a. GitHub

The repository already exists locally with git history. Push it:

```powershell
cd C:\Users\Por\Documents\GitHub\sb-groupchat-bot
git status
git push -u origin main
```

If there is no remote yet, create an **empty private** repository at
<https://github.com/new> named `sb-groupchat-bot` — no README, no .gitignore —
then:

```powershell
git remote add origin https://github.com/<your-username>/sb-groupchat-bot.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `gcp-service-account.json` and anything
matching `*-service-account*.json`, so the key cannot be pushed by accident.
**Do not add `gcp-service-account.json` to the repo** — it goes to Render
directly in step 5c.

**Check it worked:** the GitHub repo is **Private** and lists `bot.py`,
`firestore_db.py`, `gemini_client.py`, `test_bot.py`, `requirements.txt`,
`render.yaml`, `README.md`, `INSTALL.md`, `.env.example`, `.gitignore` — and
does **not** contain any `*-service-account*.json`.

### 5b. Render

1. Go to <https://render.com> and sign up with your GitHub account. **Do not add
   a payment method.**
2. Click **New → Blueprint**, connect GitHub, and pick `sb-groupchat-bot`.
   - Use **Blueprint**, not "Web Service". Blueprint is the option that reads
     `render.yaml`, so the plan, build command and start command configure
     themselves.
3. Render prompts for the values marked `sync: false` in `render.yaml`. Fill them
   in now, or add them afterwards under the service's **Environment** tab:

   | Key | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | from step 1 |
   | `GEMINI_API_KEY` | from step 4 |
   | `WEBHOOK_SECRET` | your first generated secret |
   | `CLEANUP_KEY` | your second generated secret |
   | `WEBHOOK_HEADER_SECRET` | your third generated secret |
   | `ADMIN_USER_IDS` | from step 2 |

   The remaining variables already have correct values from `render.yaml` —
   `GEMINI_MODEL`, `GEMINI_MIN_INTERVAL_SECONDS`, `GEMINI_MAX_ATTEMPTS`,
   `MAX_MESSAGES_PER_REQUEST`, `LOCAL_UTC_OFFSET_HOURS` and
   `GOOGLE_APPLICATION_CREDENTIALS`. Leave them alone.
4. Confirm and let it deploy (**Apply**). The first build takes a few minutes and
   **is expected to fail to reach Firestore** — the credentials arrive next.

### 5c. The service account key

1. Open the service → **Environment** → **Secret Files** → **Add Secret File**:
   - Filename: `gcp-service-account.json` (exactly this — `render.yaml` points
     `GOOGLE_APPLICATION_CREDENTIALS` at `/etc/secrets/gcp-service-account.json`)
   - Contents: open the JSON file from step 3 in a text editor, copy everything,
     paste it in. Save.
2. Saving a secret file redeploys the service. That's expected.
3. Wait for the status to read **Live**, then copy the URL at the top, e.g.
   `https://sb-groupchat-bot.onrender.com`. That is your **`RENDER_URL`**.

**Check it worked:** open `RENDER_URL` in a browser. It shows
`sb-groupchat-bot ok`. If not, open **Logs** and read the first few lines — a
missing environment variable appears as a `KeyError`.

You should also see one warning in the logs *only if* you skipped `CLEANUP_KEY`:
`CLEANUP_KEY is unset, so /cleanup reuses WEBHOOK_SECRET`. If you see it, go back
and set `CLEANUP_KEY`.

---

## Step 6 — Point Telegram at Render

Telegram needs to know where to deliver messages. This is a one-time step, and
the parameters matter — read the notes below before pasting.

Paste this into your browser's address bar, replacing the four `<...>` parts,
then press Enter:

```
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=<RENDER_URL>/webhook/<WEBHOOK_SECRET>&secret_token=<WEBHOOK_HEADER_SECRET>&allowed_updates=["message","edited_message","chat_member"]
```

Worked example (fake values):

```
https://api.telegram.org/bot8123456789:AAFxxxx/setWebhook?url=https://sb-groupchat-bot.onrender.com/webhook/aB3xY9kLmQ7rT2vW&secret_token=zP5nH8jF4dS6gK1c&allowed_updates=["message","edited_message","chat_member"]
```

Three things to get right:

- There is no `<` or `>` in the final URL, and the token keeps its colon.
- **`secret_token` must equal the `WEBHOOK_HEADER_SECRET` you set in Render.**
  If they don't match, the bot rejects every update with 403 and looks dead. If
  you left `WEBHOOK_HEADER_SECRET` blank in Render, drop `&secret_token=...`
  from this URL entirely.
- **`allowed_updates` must include `chat_member`.** Telegram does not send those
  by default, and they're how the bot learns that someone left a group so it can
  revoke their access to that group's history.

**Check it worked:** you see `{"ok":true,"result":true,"description":"Webhook was
set"}`. Then open
`https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo` and confirm
`url` matches, and `allowed_updates` lists `chat_member`.

---

## Step 7 — Set up the daily cleanup

Render's free plan has no scheduler, so use a free external one. This is what
enforces the 10-day retention.

1. Go to <https://cron-job.org> and create a free account.
2. Click **Create cronjob**.
3. URL: `<RENDER_URL>/cleanup?key=<CLEANUP_KEY>`
   - Note this uses **`CLEANUP_KEY`**, not `WEBHOOK_SECRET`. They are separate on
     purpose: this URL is stored in cron-job.org's dashboard and appears in
     Render's request logs, which is no place for the webhook path.
4. Schedule: every day at a quiet hour, e.g. 19:00 UTC (02:00 in Cambodia).
5. Save and press **Test run** once.

**Check it worked:** the test run returns HTTP 200 with a body like `deleted 0`.
If it returns 403, the key in the URL doesn't match `CLEANUP_KEY` in Render.

---

## Step 8 — Add the bot to each project group

Do this for every Singbuild group.

1. Open the group in Telegram → group name → **Add members** → search
   `BOT_USERNAME` → add it.
2. **Make it an admin.** Group name → **Administrators** → **Add
   administrator** → pick the bot. It doesn't need any special powers; admin
   status makes message visibility reliable and lets it see membership changes.
   Leave the defaults.
3. In the group, give it a short name:

   ```
   /setalias uvp2
   ```

   The bot confirms and shows you the DM commands that alias enables.
4. Repeat per group with a different alias each time — e.g. `uvp2`, `kfk2`,
   `norea`. Aliases are 2–20 characters, letters, numbers, dashes and
   underscores only. `today` and `week` are reserved.

**Check it worked:** send a normal message in the group, then send `/summary`.
The group gets "Working on it" and the recap arrives in your private chat.

---

## Step 9 — Tell the team one thing

For a private recap to reach someone, **they must have started a private chat
with the bot at least once**. Telegram forbids bots from messaging people first.

Ask everyone to search `BOT_USERNAME`, open it, and press **Start**. One time,
and that's it.

If someone forgets, `/summary` tells them so in the group and refunds the quota
unit, so nothing is wasted.

**Check it worked:** at least one colleague can run `/summary` in a group and
receive the DM.

---

## Step 10 — Smoke test

Run through all of it once:

1. In a group, send a few normal messages.
2. `/ask what is the standard curing time for concrete` → answers in the group.
3. Send a short voice note (under 3 minutes). **Nothing visible happens.** That's
   correct — the bot only records a reference to it.
4. `/summary` → group gets "Working on it", recap arrives privately with the
   voice note included as English text.
5. `/export week` → a `.txt` file arrives privately.
6. `/limits` → shows your remaining quota. If you're in `ADMIN_USER_IDS` it says
   you have no limits, which is also correct.
7. From a **private chat** with the bot: `/summary uvp2 week` → works.
8. Ask a colleague who is *not* in that group to try `/summary uvp2` in a DM →
   they get "I don't know a group called 'uvp2' that you're a member of."

**Check it worked:** all eight behave as described. Step 8 is the access-control
check and is worth actually doing.

---

# Reference

## Environment variables

| Variable | Set by | Notes |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | you | From BotFather. Required. |
| `GEMINI_API_KEY` | you | From AI Studio. Required. |
| `WEBHOOK_SECRET` | you | Goes in the webhook URL path. Required. |
| `CLEANUP_KEY` | you | `?key=` on `/cleanup`. Keep different from `WEBHOOK_SECRET`. |
| `WEBHOOK_HEADER_SECRET` | you | Optional. If set, must match `secret_token` on setWebhook. |
| `ADMIN_USER_IDS` | you | Comma-separated numeric IDs that bypass all limits. |
| `GEMINI_MODEL` | render.yaml | `gemini-3.5-flash-lite`. Change here when Google retires it. |
| `GEMINI_MIN_INTERVAL_SECONDS` | render.yaml | `4.0` ≈ 15 requests/minute, the free tier's ballpark. |
| `GEMINI_MAX_ATTEMPTS` | render.yaml | `3`. Retries on rate limits and 5xx. |
| `MAX_MESSAGES_PER_REQUEST` | render.yaml | `2000` cap per `/summary` or `/export`. |
| `LOCAL_UTC_OFFSET_HOURS` | render.yaml | `7` for Cambodia. Decides when "today" starts. |
| `GOOGLE_APPLICATION_CREDENTIALS` | render.yaml | `/etc/secrets/gcp-service-account.json`. |

## Daily limits

Per person, per day, across all groups, resetting at midnight UTC.

| | Limit |
| --- | --- |
| `/ask` | 20 |
| `/summary` | 5 |
| `/export` | 3 |
| Voice transcriptions, per person | 15 |
| Voice transcriptions, whole group | 200 |

## Free-tier ceilings, for reference

- **Render:** 750 free instance hours per workspace per month. Sleeps after 15
  minutes idle and takes about a minute to wake; sleeping doesn't consume hours.
  Don't add a keep-alive ping — a 31-day month is 744 hours, so staying awake
  eats the entire allowance.
- **Firestore Spark:** 1 GiB stored, 20,000 writes, 50,000 reads and 20,000
  deletes per day. Roughly one write per chat message; reads are dominated by
  `/summary` and `/export`, which is why `MAX_MESSAGES_PER_REQUEST` exists.
- **Gemini free tier:** limits requests per minute as well as per day, which is
  what `GEMINI_MIN_INTERVAL_SECONDS` respects.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Bot does nothing, `getWebhookInfo` shows 403 errors | `secret_token` on setWebhook doesn't match `WEBHOOK_HEADER_SECRET` | Re-run step 6, or clear `WEBHOOK_HEADER_SECRET` in Render |
| Bot does nothing, webhook URL looks wrong | Typo in `RENDER_URL` or `WEBHOOK_SECRET` | Re-run step 6 |
| First message after a quiet spell is slow | Render free tier was asleep | Normal. Telegram retries and the bot ignores duplicates |
| `/summary` and `/export` are empty | Privacy mode still on | Redo step 1.6, then send new messages. Only messages after the fix are recorded |
| "I couldn't message you privately" | That person never pressed Start | Step 9. Their quota is refunded |
| "I don't know a group called 'x'" | Alias doesn't exist, or caller isn't in that group | Same message for both, on purpose. Check `/setalias` and membership |
| `[voice transcription failed]` | Usually the sender deleted the voice message | No fix; the audio is gone. Otherwise check `GEMINI_API_KEY` and the Render logs |
| Logs full of `Gemini call failed ... retrying` | Brushing the free tier's per-minute limit | Raise `GEMINI_MIN_INTERVAL_SECONDS` |
| `model not found` in logs | Google retired the model | Look up the current Gemini flash model and update `GEMINI_MODEL` in Render. No code change |
| `[voice transcription limit reached for today]` | Past 15 per person or 200 per group | Expected. Resets at midnight UTC |
| A long voice note was ignored | Over 3 minutes | Deliberate. Raise `MAX_VOICE_SECONDS` in `bot.py` if you accept the extra quota cost |
| "only the most recent 2000 messages" | `MAX_MESSAGES_PER_REQUEST` capped it | Raise it in Render, or accept it |
| `KeyError` on startup in Render logs | A required env var is missing | Compare the Environment tab against the table above |
| `deleted 0` forever from cleanup | Nothing is 10 days old yet | Correct behaviour |
| `/cleanup` returns 403 | Cron URL uses the wrong key | It must be `CLEANUP_KEY`, not `WEBHOOK_SECRET` |

## Optional: run the tests before deploying

Not required for a normal install, but it verifies the code on your machine
without any credentials — Firestore and Gemini are stubbed.

```powershell
cd C:\Users\Por\Documents\GitHub\sb-groupchat-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest test_bot -v
```

Expect `Ran 50 tests` and `OK`. Nothing here touches the network or Telegram.
