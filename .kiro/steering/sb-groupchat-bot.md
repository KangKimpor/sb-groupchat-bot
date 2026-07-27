# SB Groupchat Bot — project knowledge

Telegram bot for Singbuild Construction project group chats. One deployment
serves every project group (Urban Village, KFK KMall 2, Norea SuperVilla, ...).

Telegram bot display name and username: **SingBuildGroupChatBot** (the username
may carry a `_bot` suffix if the exact string was taken — check what is actually
registered in BotFather). The GitHub repo name, the Render service name
(`sb-groupchat-bot`) and the Telegram bot's own name are independent
identifiers; they are not required to match.

Read this before touching `bot.py`, `firestore_db.py`, `gemini_client.py` or the
deploy config, so settled decisions don't get rebuilt or re-litigated.

## Locked command spec

- `/ask <question>` — general Q&A, public reply in the group, or standalone in a
  DM. No topic restriction beyond Gemini's own safety filters.
- `/summary [group_alias] [today|week]` — recap, **always DM'd privately**
  regardless of where it was called. Defaults to `today`.
- `/export [group_alias] [today|week]` — `.txt` of all messages in range,
  **always DM'd privately**. Voice notes appear inline as transcribed text.
- `/limits` — caller's remaining daily quota per command.
- `/setalias <name>` — group admins only, run inside a group, so that group can
  be referenced from a DM.
- `/help`, `/start` — command list.

### Out of scope — do not add back without Por asking

- No `/translate` command. Cut deliberately; `/ask`, `/summary` and `/export`
  cover the real need.
- No automatic transcription on receipt. Voice notes are downloaded and stored
  silently, nothing more.
- No gamification (streaks, points, leaderboards). Rejected early — it doesn't
  serve a work group.
- No image generation.

## Architecture — three files, deliberately not more

Built under a "ponytail" constraint (lazy senior engineer: minimum files, reuse
what's already there, no abstraction for a future that hasn't arrived). Keep
applying that lens unless Por says otherwise. Each module has exactly one
caller, so do **not** split these into `handlers/`, `services/` or a repository
layer.

- **`bot.py`** — the only file importing `flask` or `telegram`. Flask routes,
  PTB `Application`, every command handler, the silent logging middleware
  (`log_all_messages`), and transcription orchestration.
- **`firestore_db.py`** — the only file touching Firestore or Firebase Storage.
  Message logging, group registry, rate limit counters, retention sweep, voice
  blob upload/download.
- **`gemini_client.py`** — the only file calling the AI. `ask()`, `summarize()`,
  `transcribe_and_translate()`.
- **`test_bot.py`** — stdlib `unittest`, Firestore and Gemini stubbed. No pytest
  unless the project starts trending that way.

## Hard invariants — breaking these causes real outages

1. **One event loop, created once at module load.** `bot.py` calls
   `asyncio.new_event_loop()` once, runs `tg_app.initialize()` on it, and reuses
   it via `_loop.run_until_complete(...)` for every webhook request. Never
   replace this with `asyncio.run()` inside the route: `asyncio.run()` destroys
   its loop on return, but PTB's `Application`/`ExtBot` bind internals (queues
   etc.) to the loop they were initialised on. This fails on the **second**
   update the bot receives, not the first, so it survives casual testing. It was
   the original build's critical bug.
2. **The Flask server stays single-threaded.** That shared loop is not
   thread-safe. Adding gunicorn or `threaded=True` requires switching to
   `asyncio.run_coroutine_threadsafe` from worker threads. One free Render
   instance serving construction groups does not need real concurrency.
3. **Use the `google-genai` SDK.** `google-generativeai` (imported as
   `google.generativeai`) is dead — end of life 2025-11-30.
4. **The Gemini model name lives in the `GEMINI_MODEL` env var.** Google retires
   models on a schedule: this bot originally used `gemini-2.0-flash`, which was
   **shut down 2026-06-01**. Current default is `gemini-3.5-flash-lite` (GA,
   supports text/image/video/audio/PDF). When it's retired, change the env var
   in Render — not the code. Re-verify the current model name in any future
   session rather than trusting this line.
5. **No composite Firestore indexes required.** Every query is single-field, and
   the only range query orders by the same field it filters on (`ts`). Adding a
   query that filters on one field and orders by another needs an index created
   by hand first — avoid it.
6. **Voice notes go inline, not through the Files API.** The 3-minute cap keeps
   every request far below the 20MB inline ceiling, so `types.Part.from_bytes`
   avoids an upload plus cleanup round trip.

## Voice pipeline

1. **On receipt:** downloaded from Telegram, uploaded to Firebase Storage at
   `{group_id}/{message_id}.ogg`, logged in Firestore with `transcript: null`.
   No AI call, no chat message.
2. **Over 3 minutes** (`MAX_VOICE_SECONDS = 180`): skipped entirely, not stored
   and not logged.
3. **Transcription is lazy** — only when a `/summary` or `/export` range
   actually contains the note. Gemini auto-detects the language and translates
   to English. The result is cached to the Firestore `transcript` field, so a
   note is never sent to Gemini twice.
4. Gemini is prompted for a fixed two-line reply (`LANGUAGE:` / `TEXT:`), parsed
   by `_extract_transcript_text` in `bot.py`. **If transcripts start coming back
   malformed, that regex is the first thing to check.** It falls back to the
   whole reply rather than dropping content.

## Retention and rate limits

**Retention:** 10 days rolling, for Firestore docs, cached transcripts, Storage
audio blobs and stale usage counters. Enforced by
`firestore_db.cleanup_old_messages()` via the `/cleanup` route, called daily by
cron-job.org. Render's own cron needs a paid plan, hence the external trigger.

**Limits** (per user, per day, reset at UTC midnight; `ADMIN_USER_IDS` bypasses
everything):

| | Limit |
| --- | --- |
| `/ask` | 20 |
| `/summary` | 5 |
| `/export` | 3 |
| voice transcription, per user | 15 |
| voice transcription, whole group | 200 |

Both voice caps are enforced inside `_transcribe_voice_msg`, before any Gemini
call. The group cap is checked first, then the per-user quota is consumed. The
group counter only increments **after** a successful call, so a Gemini outage
doesn't burn the group's day. These caps exist specifically to stay inside the
Gemini free tier — the `/summary` and `/export` limits alone would not stop one
call whose range happens to contain fifty voice notes.

## Multi-group design and access control

Each group is a Firestore doc at `groups/{group_id}` holding `name`, `alias` and
`member_ids`. Messages are namespaced at `messages/{group_id}/log/{message_id}`.

DM-based group commands require an alias (set by `/setalias` inside the target
group) and check `is_member(group_id, user_id)` before returning anything.
**That membership check is the access control boundary** preventing someone from
DM-querying a project group they aren't in. A failed alias lookup and a failed
membership check return the same message, so aliases can't be probed.

Convenience behaviour: from a DM with no alias, if the caller belongs to exactly
one aliased group, that group is used. Still safe — `list_user_groups` only ever
returns groups the caller is a member of.

## Configuration

Set in the Render dashboard; see `.env.example` and `render.yaml`.

`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `GCS_BUCKET`, `WEBHOOK_SECRET`,
`ADMIN_USER_IDS`, `GEMINI_MODEL`, `LOCAL_UTC_OFFSET_HOURS`,
`GOOGLE_APPLICATION_CREDENTIALS`.

- `WEBHOOK_SECRET` is embedded in the webhook URL path (`/webhook/<secret>`) and
  required as `?key=` on `/cleanup`, so random traffic can't trigger either.
- `LOCAL_UTC_OFFSET_HOURS` (default `7`) decides when "today" starts for
  `/summary` and `/export`. A UTC-midnight boundary would silently drop every
  message sent before 7am on a Cambodian site. Rate limits still reset at UTC
  midnight regardless.
- Bot must be **admin** in each group for reliable message visibility, and
  BotFather `/setprivacy` must be **Disabled** or the bot never sees ordinary
  group messages and summaries come back empty.
- Render free tier sleeps after 15 minutes idle. Webhook-based, so it wakes on
  the next update — a few seconds of latency, acceptable here.

## Verification status

Run and passing: `py_compile` on all four Python files, plus assertions covering
argument parsing, the transcript regex and its fallback, the local-midnight
boundary, message chunking, every voice-limit branch, registry write deduping,
and route registration.

**Not yet run: `test_bot.py` itself**, including
`test_sequential_updates_share_one_loop` — the test that actually proves
invariant 1 above. It was written in a sandbox with no PyPI access, so
python-telegram-bot and Flask could not be installed. Run it before trusting a
production deploy:

```bash
pip install -r requirements.txt && python3 -m unittest test_bot.py -v
```

`google-genai==2.14.0` in `requirements.txt` is also an unverified pin (PyPI was
unreachable). The other four pins were confirmed current as of 2026-07-27. If a
Render build fails on a dependency line, start there.

## Minor things left alone on purpose

Group registry writes are deduped by an in-process `_registry_seen` cache, so
each member costs one registry write per container lifetime rather than one per
message. The cache resets whenever Render's free instance sleeps, which is
harmless — `register_group` and `add_member` are both idempotent.
