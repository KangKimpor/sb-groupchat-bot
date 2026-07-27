# SingBuildGroupChatBot

A Telegram bot for Singbuild Construction project group chats. One deployment
serves every project group (Urban Village, KFK KMall 2, Norea SuperVilla, ...).

It quietly logs group messages for 10 days, answers questions, and on request
sends you a private recap or a full text export — including voice notes, which
it transcribes and translates to English.

Everything runs on genuinely free tiers, with no billing account anywhere:
Render + Firebase Firestore + the Gemini API.

**Installing it for the first time? Follow [INSTALL.md](INSTALL.md).** It is a
self-contained, step-by-step guide written for someone who has never deployed
anything. This file is the reference: what the bot does, what the limits are,
and what to check when something misbehaves.

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

Per person, per day, **across all groups**, resetting at midnight UTC. Anyone
listed in `ADMIN_USER_IDS` has no limits.

| | Limit |
| --- | --- |
| `/ask` | 20 |
| `/summary` | 5 |
| `/export` | 3 |
| Voice transcriptions | 15 |
| Voice transcriptions, whole group | 200 |

These exist to stay inside the Gemini free tier. Voice notes are only ever
transcribed once — the text is saved, so the same note never costs quota twice.

If a request fails in a way that never reached you — Gemini erroring on `/ask`,
or a `/summary` that couldn't be delivered because you never pressed Start — the
quota unit is handed back.

## How voice notes work

1. Someone sends a voice note. The bot notes down a reference to it and nothing
   else. **No AI is used, no audio is copied**, and nothing is posted in the chat.
2. Notes longer than **3 minutes** are ignored completely.
3. The first time a `/summary` or `/export` covers that note, the bot fetches the
   audio back from Telegram, transcribes it, translates it to English if needed,
   and saves the text for next time.

The audio itself is never stored by this bot — it stays where your team already
sent it, on Telegram's servers. That keeps the whole project on free plans and
means there's no second copy of your site conversations sitting in a cloud
bucket.

One consequence: if someone **deletes** their voice message in Telegram, a later
summary can't transcribe it and will show `[voice transcription failed]`. Notes
already transcribed are unaffected, since the text is saved.

## Who can read what

Recaps and exports are only ever sent to the person who asked, and only for a
group they are currently in. Membership is confirmed against Telegram at the
moment of the request, so **removing someone from a project group immediately
removes their access to that group's history**, including from a private chat.

For that revocation to be recorded promptly, the webhook must be registered with
`chat_member` in its `allowed_updates` list — INSTALL.md step 6 does this. Even
without it, the live check still refuses the request.

## Data retention

Messages and transcripts are deleted after **10 days** by a daily cleanup job
(INSTALL.md step 7). After that the bot has no record of the conversation at all.

One thing to know before putting a client-confidential group on this: on the
Gemini API **free tier**, Google states that content may be used to improve
their products. If that isn't acceptable for a particular project, that project
shouldn't use this bot on the free tier.

---

# Notes and troubleshooting

## The first message after a quiet spell is slow

Render's free tier puts the service to sleep after 15 minutes of no traffic, and
waking it takes about a minute. The next Telegram message wakes it. Telegram
re-sends anything it thinks failed and the bot ignores duplicates, so nothing is
lost or doubled — it's just briefly slow. Normal.

Don't "fix" this with a keep-alive ping. Render grants 750 free instance hours a
month per workspace and a 31-day month is 744 hours, so keeping the service
permanently awake consumes essentially the entire allowance and risks suspending
everything until the next month.

## The bot does nothing at all

1. Check the webhook. Open in a browser:
   `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
   - `"url"` must exactly match your Render URL plus `/webhook/<WEBHOOK_SECRET>`.
   - Look at `last_error_message`. If it mentions a timeout, hit your Render URL
     once in a browser to wake the service, then send another message.
2. **If you set `WEBHOOK_HEADER_SECRET`,** the `setWebhook` call must have
   included `&secret_token=<that same value>`. If it didn't, every update is
   rejected with 403 and the bot looks dead. Either re-run setWebhook with the
   token, or clear `WEBHOOK_HEADER_SECRET` in Render.
3. Check Render → your service → **Logs** for errors on startup. A missing env
   var shows up as `KeyError` in the first few lines.
4. Confirm the service is **Live**, not **Failed** or **Suspended**.

## Commands work but `/summary` and `/export` come back empty

Privacy mode is still on, so the bot never saw the normal messages. Redo
INSTALL.md step 1.6 (`/setprivacy` → Disable), then send some new messages. Only
messages sent *after* the fix are recorded.

## "I couldn't message you privately"

That person hasn't started a private chat with the bot. See INSTALL.md step 9.
Their quota is refunded, so they can retry once that's sorted.

## "I don't know a group called 'x' that you're a member of"

Either the alias doesn't exist, or the caller isn't in that group any more. The
message is deliberately identical for both so aliases can't be guessed. Check
with `/setalias` inside the group, and check the person is still a member.

## Voice notes show `[voice transcription failed]`

- **Was the voice message deleted from the chat?** The bot fetches the audio from
  Telegram on demand, so a deleted note can no longer be transcribed. This is the
  most common cause and there's no fix — the audio is gone.
- Check `GEMINI_API_KEY` in Render.
- Check the Render logs. Rate limits and 5xx errors are retried automatically
  three times with backoff, and each attempt is logged, so a burst of
  `Gemini call failed ... retrying` lines means you're brushing the free tier's
  per-minute ceiling. Raise `GEMINI_MIN_INTERVAL_SECONDS`.
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

## A summary says "only the most recent 2000 messages"

`MAX_MESSAGES_PER_REQUEST` capped it. The cap keeps the newest messages and
exists so one busy week can't exhaust the 512MB instance, the Gemini token
budget, or Firestore's 50,000 free reads a day. Raise or lower it in Render.

## "today" looks like it covers the wrong hours

`LOCAL_UTC_OFFSET_HOURS` in Render decides when "today" starts. It ships as `7`
for Cambodia. Daily quotas always reset at midnight UTC regardless.

## Could this ever cost money?

Not without you choosing to make it so. No payment method is attached to Render,
Firebase or the Gemini API, so every limit in this stack fails by refusing work,
not by billing:

- **Render** suspends free services rather than charging.
- **Firestore on Spark** returns errors once a daily quota is spent, and resets.
- **Gemini free tier** returns 429s, which the bot retries and then reports.

Two things would break that, and both are called out in the code comments:
enabling **Cloud Storage for Firebase** (needs the paid Blaze plan) or attaching
a billing account to the Firebase project (which also drops the Gemini API off
the free tier).

## Running the tests

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest test_bot -v
```

On macOS or Linux:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m unittest test_bot -v
```

50 tests, no credentials needed — Firestore and Gemini are stubbed. The
important ones push webhook requests through in a single process, which is what
catches event-loop regressions and slow-ACK regressions.

## Running locally

PowerShell:

```powershell
Copy-Item .env.example .env      # then fill in the real values
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
}
.\.venv\Scripts\python.exe bot.py
```

bash:

```bash
cp .env.example .env             # then fill in the real values
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
set -a && source .env && set +a
./.venv/bin/python bot.py
```

Telegram needs a public HTTPS URL, so local runs can't receive real messages
unless you tunnel (e.g. ngrok) and point the webhook at the tunnel. Easiest is
to test on Render.

## Design notes for whoever edits this next

- Three code files on purpose. `bot.py` is the only file that touches Telegram
  or Flask, `firestore_db.py` is the only file that touches Firestore,
  `gemini_client.py` is the only file that calls the AI. Don't add a
  handlers/services/repository split for one caller.
- **Never** replace the shared event loop in `bot.py` with `asyncio.run()` inside
  the webhook route. `asyncio.run()` destroys its loop on return, and
  python-telegram-bot binds internals to the loop it was initialised on. It
  fails on the *second* message the bot receives, not the first, so it looks
  fine in casual testing.
- **The webhook must ACK before doing the work.** Telegram gives a webhook about
  a minute and re-delivers anything slower. Processing a twenty-voice-note
  summary inline got it replayed: quota charged twice, DM sent twice, and every
  other group blocked meanwhile. The route parses, dedupes on `update_id`, hands
  the coroutine to the loop thread and returns 200.
- Flask is single-threaded and must only ever reach the loop through
  `asyncio.run_coroutine_threadsafe`. Update processing already happens off the
  request thread, so gunicorn or `threaded=True` buys nothing.
- Wrap blocking Firestore and Gemini calls in `_off_loop`. They're synchronous;
  awaiting them directly pins the loop and one slow summary stalls every group.
- Membership is confirmed against Telegram, not against `member_ids`. That list
  only ever grew, so it could not revoke anything on its own.
- `log_message` uses `merge=True` and never writes `transcript`. The same
  `message_id` can arrive twice, and a plain `.set()` wiped cached transcriptions.
- Every Firestore query here is single-field, and the only range query orders by
  the same field it filters on, so no composite indexes are required. Filtering
  on one field while ordering by another would need an index created by hand.
- Use the `google-genai` package. `google-generativeai` is dead (end of life
  2025-11-30).
- The free tier limits requests per *minute*, not just per day. `gemini_client`
  serialises calls through a pace gate and retries 429s and 5xx with backoff.
