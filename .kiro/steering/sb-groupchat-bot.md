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
  (`log_all_messages`), membership revocation, and transcription orchestration.
- **`firestore_db.py`** — the only file touching Firestore. Message logging,
  group registry, rate limit counters, retention sweep. No blob storage; see
  invariant 8.
- **`gemini_client.py`** — the only file calling the AI. `ask()`, `summarize()`,
  `transcribe_and_translate()`, plus the free-tier pace gate and retry.
- **`test_bot.py`** — stdlib `unittest`, Firestore and Gemini stubbed. No pytest
  unless the project starts trending that way.

## Hard invariants — breaking these causes real outages

1. **One event loop, created once at module load, owned by one thread.**
   `bot.py` calls `asyncio.new_event_loop()` once, runs `tg_app.initialize()` on
   it, then hands it to a single daemon thread running `run_forever()`. Never
   replace this with `asyncio.run()` inside the route: `asyncio.run()` destroys
   its loop on return, but PTB's `Application`/`ExtBot` bind internals (queues
   etc.) to the loop they were initialised on. That fails on the **second**
   update the bot receives, not the first, so it survives casual testing. It was
   the original build's critical bug.
2. **The webhook route ACKs before doing the work.** It parses, dedupes, calls
   `_submit()` (which is `asyncio.run_coroutine_threadsafe`) and returns 200
   immediately. Telegram allows a webhook roughly a minute and re-delivers
   anything slower, so a `/summary` that transcribes twenty voice notes must
   never be processed inline — it used to get replayed, charging quota twice and
   DM'ing twice, while stalling every other group.
3. **Flask stays single-threaded (`threaded=False`) and never touches the loop
   directly.** The only legal way in from a request thread is
   `run_coroutine_threadsafe`. Update processing already happens off the request
   thread, so there is nothing to gain from gunicorn or `threaded=True`.
4. **Blocking calls go through `_off_loop`** (`asyncio.to_thread`). Everything
   in `firestore_db` and `gemini_client` is synchronous; calling it straight from
   a handler pins the shared loop and one slow summary stalls every other group.
5. **Use the `google-genai` SDK.** `google-generativeai` (imported as
   `google.generativeai`) is dead — end of life 2025-11-30.
6. **The Gemini model name lives in the `GEMINI_MODEL` env var.** Google retires
   models on a schedule: this bot originally used `gemini-2.0-flash`, which was
   **shut down 2026-06-01**. Current default is `gemini-3.5-flash-lite` —
   verified 2026-07-27 as GA, audio-capable, 1M context, and on the Gemini API
   free tier. When it's retired, change the env var in Render, not the code.
   Re-verify the current model name in any future session rather than trusting
   this line.
7. **No composite Firestore indexes required.** Every query is single-field, and
   the only range query orders by the same field it filters on (`ts`) — now
   descending plus `.limit()`, which is still the same field. Adding a query that
   filters on one field and orders by another needs an index created by hand
   first — avoid it.
8. **Never store audio, and never add Cloud Storage for Firebase.** Since
   September 2024 it requires the paid **Blaze** plan, and linking a billing
   account to the project would also drop it off the **Gemini API free tier** —
   two metered services where there were none. Instead we persist Telegram's
   `file_id` and fetch the audio back with `bot.get_file(file_id)` at
   transcription time. Telegram's download URLs expire after an hour, but
   calling `get_file` again mints a fresh one, so a `file_id` stays usable well
   past the 10-day retention window. Consequence to keep in mind: if the sender
   deletes the voice message, transcription fails and returns
   `[voice transcription failed]`. Already-cached transcripts are unaffected.
9. **`log_message` writes with `merge=True` and never writes `transcript`.**
   The same `message_id` legitimately arrives twice (an edit, or a Telegram
   re-delivery). A plain `.set()` carrying `transcript: None` wiped the cached
   transcription and sent the note to Gemini again.

## Voice pipeline

1. **On receipt:** only the Telegram `file_id` is logged in Firestore. No
   download, no upload, no AI call, no chat message.
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

**Retention:** 10 days rolling, for Firestore docs, cached transcripts and stale
usage counters. There is no audio to purge — we never stored any, and dropping
the doc drops the `file_id` with it. Enforced by
`firestore_db.cleanup_old_messages()` via the `/cleanup` route, called daily by
cron-job.org. Render's own cron needs a paid plan, hence the external trigger.
Deletes are batched (400 per commit) and the sweep walks the `messages`
collection rather than `groups`, so an orphaned log still gets collected.

**Limits** — per user, per day, **across every group**, reset at UTC midnight.
`ADMIN_USER_IDS` bypasses everything.

| | Limit |
| --- | --- |
| `/ask` | 20 |
| `/summary` | 5 |
| `/export` | 3 |
| voice transcription, per user | 15 |
| voice transcription, whole group | 200 |

The usage doc id is `usage/u{user_id}_{date}`. It used to carry the group id as
well, which silently multiplied everyone's allowance by the number of groups
they were in — and since the entire point of these caps is staying inside the
Gemini free tier, that mattered.

Both voice caps are enforced inside `_transcribe_voice_msg`, before any Gemini
call. The group cap is checked first, then the per-user quota is consumed. The
group counter only increments **after** a successful call, so a Gemini outage
doesn't burn the group's day.

**Refunds:** `/ask` that Gemini fails, and `/summary`/`/export` that cannot be
DM'd, are handed back via `db.refund_usage`. The usual cause of an undeliverable
DM is simply never having pressed Start, which should not cost a quota unit.

**Free-tier pacing lives in `gemini_client`,** not in the daily caps. The free
tier limits requests per *minute* too, so `_pace()` enforces a process-wide
minimum gap (`GEMINI_MIN_INTERVAL_SECONDS`, default 4s ≈ 15 req/min) and
`_generate` retries rate limits and 5xx with jittered backoff. Without that, a
summary containing thirty voice notes collected 429s after the first handful.

## Multi-group design and access control

Each group is a Firestore doc at `groups/{group_id}` holding `name`, `alias` and
`member_ids`. Messages are namespaced at `messages/{group_id}/log/{message_id}`.

**Telegram is the authority on membership, not Firestore.** `member_ids` is a
cheap first gate, but `bot._still_in_group()` confirms with
`bot.get_chat_member` before any history is handed over, and **fails closed** if
that check errors. This matters because `member_ids` only ever grew — ArrayUnion
on every message with nothing to undo it — so someone removed from a project
group kept DM access to its recaps indefinitely.

Firestore is kept tidy from both directions: `ChatMemberHandler` and the
`left_chat_member` service message both call `db.remove_member`, and
`new_chat_members` records joins so membership no longer depends on posting
first. The `ChatMemberHandler` path needs `chat_member` in the setWebhook
`allowed_updates` list — see INSTALL.md.

A failed alias lookup and a failed membership check return the same message, so
aliases can't be probed.

Convenience behaviour: from a DM with no alias, if the caller belongs to exactly
one aliased group, that group is used — still membership-checked.

## Configuration

Set in the Render dashboard; see `.env.example` and `render.yaml`.

`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `WEBHOOK_SECRET`, `CLEANUP_KEY`,
`WEBHOOK_HEADER_SECRET`, `ADMIN_USER_IDS`, `GEMINI_MODEL`,
`GEMINI_MIN_INTERVAL_SECONDS`, `GEMINI_MAX_ATTEMPTS`,
`MAX_MESSAGES_PER_REQUEST`, `LOCAL_UTC_OFFSET_HOURS`,
`GOOGLE_APPLICATION_CREDENTIALS`.

There is deliberately no `GCS_BUCKET`. The Firebase project must stay on the
no-cost **Spark** plan; see invariant 8.

- `WEBHOOK_SECRET` is embedded in the webhook URL path (`/webhook/<secret>`).
- **`CLEANUP_KEY` is a separate secret** for `/cleanup?key=`. Keep them
  different: a query string lands in cron-job.org's dashboard and Render's
  request logs, and whoever holds the webhook path can POST forged updates —
  including one claiming an `ADMIN_USER_IDS` sender, which bypasses every quota.
  If `CLEANUP_KEY` is unset the code falls back to `WEBHOOK_SECRET` and logs a
  warning at startup.
- `WEBHOOK_HEADER_SECRET` is optional hardening. Set it and pass the same value
  as `secret_token` to setWebhook, and forged POSTs are rejected even if the
  path leaks. Left empty the check is skipped.
- `MAX_MESSAGES_PER_REQUEST` (default 2000) caps one `/summary` or `/export`.
  It protects the 512MB free instance, the Gemini token budget and the Firestore
  free read quota. The query is descending so the cap keeps the *newest*
  messages; results are reversed before rendering.
- `LOCAL_UTC_OFFSET_HOURS` (default `7`) decides when "today" starts for
  `/summary` and `/export`. A UTC-midnight boundary would silently drop every
  message sent before 7am on a Cambodian site. Rate limits still reset at UTC
  midnight regardless.
- Bot must be **admin** in each group for reliable message visibility, and
  BotFather `/setprivacy` must be **Disabled** or the bot never sees ordinary
  group messages and summaries come back empty.
- Render free tier sleeps after 15 minutes idle and takes about a minute to wake.
  Telegram retries the update it timed out on, and the `update_id` dedup ring in
  `bot.py` makes a replay a no-op.

## Staying free — verified 2026-07-27

Nothing in this stack has a payment method attached, so **the failure mode of
every limit is refusal, not a bill.** Keep it that way.

- **Render free web service.** 750 free instance hours per workspace per month;
  spun-down services don't consume them. With no card on file Render *suspends*
  free services rather than billing when bandwidth or build minutes run out.
  Do **not** add a keep-alive cron to stop it sleeping: a 31-day month is 744
  hours, so one always-on free service consumes essentially the whole 750-hour
  allowance and any second service tips the workspace into suspension.
- **Firebase Spark / Firestore:** 1 GiB stored, 10 GiB/month egress, 20K writes,
  50K reads and 20K deletes per day. Message logging is roughly one write per
  message. Reads are the binding constraint, bounded by
  `MAX_MESSAGES_PER_REQUEST` × the daily `/summary` and `/export` caps × users;
  lower the cap if the quota is ever hit.
- **Gemini API free tier**, which `gemini-3.5-flash-lite` is on. Note the
  tradeoff Google states plainly: on the free tier **content may be used to
  improve their products**. Flag this to Por before any client-confidential
  group goes on it.
- **cron-job.org free** for the daily `/cleanup` trigger.

## Verification status

Everything below was run on 2026-07-27 against Python 3.14 on Windows.

- `py_compile` clean on all four Python files.
- All four `requirements.txt` pins install and import cleanly, **including
  `google-genai==2.14.0`**, which was previously an unverified guess.
- **`python -m unittest test_bot` — 50 tests, all passing**, including
  `test_sequential_updates_share_one_loop` (invariant 1) and
  `test_webhook_acks_before_processing` (invariant 2). This was the outstanding
  item from the first build; it is no longer outstanding.
- `google-genai` API surface checked against the installed package:
  `types.Part.from_bytes(data=, mime_type=)` and
  `models.generate_content(model=, contents=)` match how `gemini_client` calls
  them.

`test_bot.py` also loads the real `firestore_db` and `gemini_client` against
recording fakes (`FirestoreShapeTest`, `GeminiPacingTest`), so the doc shapes and
pacing behaviour the fixes depend on are pinned rather than assumed.

## Minor things left alone on purpose

Group registry writes are deduped by an in-process `_registry_seen` cache, so
each member costs one registry write per container lifetime rather than one per
message. The cache resets whenever Render's free instance sleeps, which is
harmless — `register_group` and `add_member` are both idempotent. It is now
lock-guarded, because updates are processed on the loop thread while Flask
serves requests on another.

The `update_id` dedup ring is in-process and bounded at 2048. Losing it on a
restart is fine: Telegram's retries arrive within minutes, well inside one
container's life.
