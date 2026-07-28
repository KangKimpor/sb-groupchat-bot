---
name: sb-groupchat-bot
description: >-
  Telegram group chat bot for Singbuild construction project groups
  (multi-group: Urban Village, KFK KMall 2, Norea SuperVilla, etc). Use whenever
  Por asks to build, extend, fix, audit, deploy, or discuss this bot, or
  references "the groupchat bot", "the telegram bot", "sb-groupchat-bot",
  commands /ask /summary /export /limits /setalias, voice transcription for the
  group bot, or the free Render/Firebase/Gemini stack backing it. Load before
  touching bot.py, firestore_db.py, gemini_client.py, test_bot.py, INSTALL.md or
  the deploy config, so the threading model, the access-control boundary and the
  free-tier constraints aren't rebuilt or re-litigated from scratch.
---

# SB Groupchat Bot

Telegram bot for Singbuild construction project group chats. Built
ponytail-style: 3 code files, no framework scaffolding, fully free stack.

Telegram bot name: **SingBuildGroupChatBot** (display name and username, set
via BotFather — username may have a `_bot` suffix if the exact string was taken,
check what Por actually registered).

Repo location: `C:\Users\Por\Documents\GitHub\sb-groupchat-bot` on Por's
Windows machine, with a private GitHub remote at `KangKimpor/sb-groupchat-bot`
(confirmed from git history). The GitHub repo name, the Render service name
(`sb-groupchat-bot`) and the Telegram bot's own display name
(`SingBuildGroupChatBot`) are independent identifiers, not required to match.

**Check the working tree before trusting this file.** A full audit and fix pass
landed 2026-07-27 and may not have been committed or deployed yet — run
`git status` and `git log --oneline -5` first.

Por is on **Windows / PowerShell**. Use `;` not `&&` to join commands, and
`.\.venv\Scripts\python.exe` rather than `python3`.

## Final locked spec

**Commands**

- `/ask <question>` — general Q&A, public reply in group or standalone in a DM.
  No topic restriction beyond Gemini's own safety filters.
- `/summary [group_alias] [today|week]` — chat recap, **always DM'd privately**
  regardless of where it's called from. Transcribes any voice notes in range on
  the fly, caches the transcript after. Defaults to `today`.
- `/export [group_alias] [today|week]` — `.txt` file of all messages in range,
  **always DM'd privately**. Voice notes included as transcribed text inline.
- `/limits` — caller's remaining daily quota per command.
- `/setalias <name>` — group-admin-only, run inside a group, registers a short
  alias (e.g. `uvp2`) so that group can be referenced from a DM.
- `/help`, `/start` — command list.

**Explicitly cut from scope** (don't re-add without Por asking):

- No `/translate` command — `/ask`, `/summary` and `/export` cover the real need.
- No automatic transcription on receipt. Strictly on-demand, triggered only by a
  `/summary` or `/export` range touching a voice note.
- No gamification (streaks, leaderboards, points) — rejected early, doesn't
  serve a work group.
- No image generation.

**Voice handling**

- On receipt: **only Telegram's `file_id` is logged in Firestore.** No download,
  no upload, no AI call, no chat message. There is no audio storage anywhere —
  see invariant 8.
- Max 3 minutes (`MAX_VOICE_SECONDS = 180`). Longer notes are not stored or
  logged at all (hard skip in `log_all_messages`).
- Transcription is lazy: at `/summary` or `/export` time the audio is fetched
  back with `bot.get_file(file_id)`, sent inline to Gemini (auto-detect language
  + translate to English), and the result cached to the Firestore `transcript`
  field so a note is never sent to Gemini twice.
- Gemini is prompted for `LANGUAGE: <x>` / `TEXT: <x>` and parsed by
  `_extract_transcript_text` in `bot.py`. **If transcripts come back malformed,
  that regex is the first thing to check.** It falls back to the whole reply
  rather than dropping content.
- Failure mode to expect: if the sender deletes the voice message, Telegram stops
  serving it and transcription returns `[voice transcription failed]`. Cached
  transcripts are unaffected.

**Retention:** 10 days rolling for Firestore message docs, cached transcripts and
stale usage counters. There is no audio to purge. Enforced by
`firestore_db.cleanup_old_messages()` via the `/cleanup` Flask route, called
daily by cron-job.org (free) — not Render's cron, which needs a paid plan.
Deletes are batched 400 per commit, and the sweep walks the `messages`
collection via `list_documents()` rather than `groups`, so an orphaned log still
gets collected.

**Rate limits** — per user, per day, **across every group**, reset at UTC
midnight. `ADMIN_USER_IDS` bypasses everything.

| | Limit |
| --- | --- |
| `/ask` | 20 |
| `/summary` | 5 |
| `/export` | 3 |
| voice transcription, per user | 15 |
| voice transcription, whole group | 200 |

Usage doc id is `usage/u{user_id}_{date}`. It used to carry the group id too,
which silently multiplied everyone's allowance by the number of groups they were
in. Both voice caps are enforced inside `_transcribe_voice_msg` before any Gemini
call: group cap checked first, then the per-user quota consumed, and the group
counter only increments **after** a successful call so a Gemini outage doesn't
burn the group's day.

**Refunds:** a failed `/ask` and an undeliverable `/summary` or `/export` hand
the quota unit back via `db.refund_usage`. The usual cause of an undeliverable DM
is never having pressed Start, which shouldn't cost anything.

**Free-tier pacing lives in `gemini_client`, not in the daily caps.** The free
tier limits requests per *minute* too, so `_pace()` enforces a process-wide
minimum gap (`GEMINI_MIN_INTERVAL_SECONDS`, default 4s ≈ 15 req/min) and
`_generate` retries 429s and 5xx with jittered backoff.

**Multi-group design:** one deployment serves every Singbuild project group.
Each group is a Firestore doc at `groups/{group_id}` holding `name`, `alias`,
`member_ids`. Messages are namespaced at `messages/{group_id}/log/{message_id}`.

**Access control — Telegram is the authority, not Firestore.** `member_ids` is a
cheap first gate, but `bot._still_in_group()` confirms with
`bot.get_chat_member` before any history is handed over, and **fails closed** if
that check errors. This matters because `member_ids` only ever grew (ArrayUnion
on every message, nothing to undo it), so someone removed from a project group
kept DM access to its recaps indefinitely. Firestore is kept tidy from both
directions: `ChatMemberHandler` and the `left_chat_member` service message both
call `db.remove_member`, and `new_chat_members` records joins so membership no
longer depends on posting first. A failed alias lookup and a failed membership
check return the same message, so aliases can't be probed.

## Architecture

3 code files plus tests, deliberately not split further (ponytail: no layers for
one caller):

- **`bot.py`** — the only file importing `flask` or `telegram`. Flask routes, PTB
  `Application`, all command handlers, the silent logging middleware
  (`log_all_messages`), membership revocation, transcription orchestration, the
  event loop and the webhook ACK machinery.
- **`firestore_db.py`** — the only file touching Firestore. Message logging,
  group registry, transactional rate-limit counters and refunds, retention sweep.
  No blob storage; see invariant 8.
- **`gemini_client.py`** — the only file calling the AI. `ask()`, `summarize()`,
  `transcribe_and_translate()`, plus the free-tier pace gate and retry.
- **`test_bot.py`** — stdlib `unittest`, 54 tests, Firestore and Gemini stubbed.
  No pytest unless the project starts trending that way.

Docs: `README.md` is the reference (what it does, limits, troubleshooting).
`INSTALL.md` is a self-contained step-by-step install guide written to be handed
to an AI assistant. `.kiro/steering/sb-groupchat-bot.md` holds the same project
knowledge for Kiro sessions — **keep it and this file in sync.**

## Hard invariants — breaking these causes real outages

1. **One event loop, created once at module load, owned by one thread.**
   `asyncio.new_event_loop()` once, `tg_app.initialize()` on it, then a single
   daemon thread runs `run_forever()`. Never use `asyncio.run()` in the route: it
   destroys its loop on return, but PTB binds internals to the loop they were
   initialised on, so it breaks on the **second** update, not the first.
2. **The webhook route ACKs before doing the work.** Parse, dedupe on
   `update_id`, `_submit()` (which is `asyncio.run_coroutine_threadsafe`), return
   200. Telegram allows a webhook roughly a minute and re-delivers anything
   slower, so a `/summary` over twenty voice notes must never be processed
   inline — it used to get replayed, charging quota twice and DM'ing twice while
   stalling every other group.
3. **Flask stays single-threaded (`threaded=False`) and never touches the loop
   directly.** The only legal way in from a request thread is
   `run_coroutine_threadsafe`. Processing already happens off the request thread,
   so gunicorn or `threaded=True` buys nothing.
4. **Blocking calls go through `_off_loop`** (`asyncio.to_thread`). Everything in
   `firestore_db` and `gemini_client` is synchronous; awaiting it directly pins
   the shared loop and one slow summary stalls every group.
5. **Use the `google-genai` SDK.** `google-generativeai` (imported as
   `google.generativeai`) is dead — end of life 2025-11-30.
6. **The Gemini model name lives in `GEMINI_MODEL`.** `gemini-2.0-flash`, the
   original, was **shut down 2026-06-01**. Current default
   `gemini-3.5-flash-lite` — verified 2026-07-27 as GA, audio-capable, 1M
   context, on the free tier. When it's retired, change the env var in Render,
   not the code. **Re-verify the current model name in any future session
   rather than trusting this line.**
7. **No composite Firestore indexes required.** Every query is single-field, and
   the only range query orders by the same field it filters on (`ts`) — now
   descending plus `.limit()`, still the same field. A query filtering on one
   field and ordering by another needs a hand-created index; avoid it.
8. **Never store audio, and never add Cloud Storage for Firebase.** Since
   September 2024 it needs the paid **Blaze** plan, and linking billing would
   also drop the project off the **Gemini API free tier** — two metered services
   where there were none. We persist Telegram's `file_id` and re-fetch with
   `get_file`. Download URLs expire hourly but `get_file` mints a fresh one, so a
   `file_id` stays usable past the 10-day window. There is no
   `google-cloud-storage` dependency and no `GCS_BUCKET` env var. **This was
   removed deliberately in a dedicated PR; do not reintroduce it.**
9. **`log_message` writes with `merge=True` and never writes `transcript`.** The
   same `message_id` legitimately arrives twice (an edit, or a re-delivery), and
   a plain `.set()` carrying `transcript: None` wiped the cache and re-billed
   Gemini.

## Stack (all free tier)

```
Telegram → Webhook → Render (free web service)
    ├─ Firestore  (message metadata + transcripts + usage counters + registry)
    └─ Gemini API (text Q&A / summarization + audio transcription/translation)

cron-job.org (free) → daily GET /cleanup?key=<CLEANUP_KEY> → purges >10-day data
```

No object storage anywhere. Voice audio stays on Telegram's servers.

**Nothing here has a payment method attached, so every limit fails by refusing
work, not by billing.** Keep it that way. Verified 2026-07-27:

- **Render free web service:** 750 free instance hours per workspace per month;
  spun-down services don't consume them. Sleeps after 15 minutes without inbound
  traffic and takes about a minute to wake. With no card on file Render
  *suspends* free services rather than charging. **Do not add a keep-alive cron**
  — a 31-day month is 744 hours, so one always-on free service eats essentially
  the whole allowance and any second service tips the workspace into suspension.
- **Firebase Spark / Firestore:** 1 GiB stored, 10 GiB/month egress, 20K writes,
  50K reads, 20K deletes per day. Roughly one write per chat message. Reads are
  the binding constraint, bounded by `MAX_MESSAGES_PER_REQUEST` × the daily
  `/summary` and `/export` caps × users; lower the cap if the quota is ever hit.
- **Gemini API free tier**, which `gemini-3.5-flash-lite` is on. **Tradeoff worth
  surfacing to Por: on the free tier Google states content may be used to
  improve their products.** Flag this before any client-confidential group goes
  on it.
- **cron-job.org free** for the daily cleanup trigger.

## Configuration

`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `WEBHOOK_SECRET`, `CLEANUP_KEY`,
`WEBHOOK_HEADER_SECRET`, `ADMIN_USER_IDS`, `GEMINI_MODEL`,
`GEMINI_MIN_INTERVAL_SECONDS`, `GEMINI_MAX_ATTEMPTS`,
`MAX_MESSAGES_PER_REQUEST`, `LOCAL_UTC_OFFSET_HOURS`,
`GOOGLE_APPLICATION_CREDENTIALS`. See `.env.example` and `render.yaml`.

- `WEBHOOK_SECRET` sits in the webhook URL path (`/webhook/<secret>`).
- **`CLEANUP_KEY` is a separate secret** for `/cleanup?key=`. Keep them
  different: a query string lands in cron-job.org's dashboard and Render's
  request logs, and whoever holds the webhook path can POST forged updates —
  including one claiming an `ADMIN_USER_IDS` sender, which bypasses every quota.
  If unset, the code falls back to `WEBHOOK_SECRET` and logs a startup warning.
- `WEBHOOK_HEADER_SECRET` is optional hardening. Set it and pass the same value
  as `secret_token` to setWebhook and forged POSTs are rejected even if the path
  leaks. Left empty the check is skipped.
- `MAX_MESSAGES_PER_REQUEST` (default 2000) caps one `/summary` or `/export`.
  The query is descending so the cap keeps the *newest* messages; results are
  reversed before rendering, and the user is told when it truncated.
- `LOCAL_UTC_OFFSET_HOURS` (default `7`) decides when "today" starts. A
  UTC-midnight boundary would drop every message sent before 7am on a Cambodian
  site. Rate limits still reset at UTC midnight regardless.

## Known gaps / next things to fix if Por reports issues

1. **Two security fixes only activate if the env var is actually set.**
   `CLEANUP_KEY` falls back to `WEBHOOK_SECRET` (with a startup warning) and
   `WEBHOOK_HEADER_SECRET` defaults to empty, skipping the header check. If Por
   skipped either during install, that hardening isn't live. Check the Render
   logs for `CLEANUP_KEY is unset`.
2. **The 200/day group voice counter is read-then-write, not transactional.**
   `check_group_voice_limit` then `increment_group_voice` can overshoot slightly
   under concurrency. Judged acceptable because it sits behind the per-user cap
   as a backstop. Make it transactional only if Por asks.
3. **ACK-before-work has a tradeoff: an update lost to a restart or spin-down
   mid-processing is gone for good**, because Telegram won't retry something it
   already got a 200 for. Render sleeps after 15 minutes without inbound traffic,
   and a very large summary at 4s Gemini pacing could in principle run past that.
   Rare, but it's the honest cost of invariant 2. Don't "fix" it with a
   keep-alive cron (see the Render note above).
4. **Nothing has been verified against the real services.** All 54 tests run with
   Firestore, Gemini and Telegram stubbed. No deploy, no real webhook, no actual
   Firestore write has been confirmed. `INSTALL.md` step 10 is the smoke test
   that closes that gap.
5. **`WEBHOOK_HEADER_SECRET` is a setup footgun.** If it's set in Render but
   `secret_token` was omitted from setWebhook, every update is rejected with 403
   and the bot looks completely dead. This is the first thing to check when Por
   says "the bot does nothing".

## Bugs found and fixed (don't reintroduce)

From the original build:

1. **`asyncio.run()` per webhook request** — would have broken on the 2nd
   incoming message. See invariant 1.
2. **`google-generativeai`** — deprecated, EOL 2025-11-30. See invariant 5.
3. **Stale `requirements.txt` placeholders** — now four pins, all installed and
   test-verified 2026-07-27: `python-telegram-bot==22.8`,
   `google-cloud-firestore==2.28.0`, `google-genai==2.14.0`, `flask==3.1.3`.
   `google-cloud-storage` was removed entirely. Re-check PyPI before bumping.
4. **Voice caps defined but not wired** into `_transcribe_voice_msg` — now
   enforced there, group cap first, group counter incremented only on success.
5. **Firebase Storage removed** in favour of Telegram `file_id`. See invariant 8.

From the 2026-07-27 audit (19 findings, all fixed, 54 tests green):

6. **Membership could not be revoked** — `member_ids` was append-only, so anyone
   removed from a group kept DM access. See the access-control section.
7. **Long `/summary` blew Telegram's webhook timeout and got replayed** — quota
   charged twice, DM sent twice, every other group stalled. See invariant 2.
8. **`log_message` wiped cached transcripts** — see invariant 9.
9. **Quotas were per-user-per-group**, multiplying allowances by group count.
   Now `usage/u{user_id}_{date}`.
10. **An update PTB couldn't parse returned 500**, and Telegram retries a non-2xx
    forever, wedging the queue. The realistic trigger is Bot API drift, not
    hostile traffic: PTB 22.8 raises on a Poll payload missing `allows_revoting`.
    Now caught, logged, and ACKed with 200.
11. **No `add_error_handler`** — handler failures were invisible except as PTB
    tracebacks, and the user got silence.
12. **`get_messages_in_range` was unbounded** — a busy week fully materialised on
    a 512MB instance and shipped to Gemini in one request. Now capped.
13. **No backoff on Gemini calls** — free tier is ~15 RPM, so a summary with 20+
    voice notes collected 429s past the first handful.
14. **`WEBHOOK_SECRET` did double duty as `/cleanup?key=`**, leaking the webhook
    path into cron dashboards and request logs. Now split, with
    `hmac.compare_digest` on both.
15. **Every handler used `update.message`, but PTB dispatches on
    `effective_message`** — editing a message into `/help` raised
    `AttributeError` and the user got nothing. All replies now go through
    `_reply()`.
16. **`log_all_messages`' docstring still claimed voice notes were "parked in
    Storage"** — exactly the comment that would talk someone into reintroducing
    Cloud Storage. Removed.
17. **`cmd_setalias` never called `add_member`**, so an admin who only ran
    `/setalias` couldn't query their own group from a DM.
18. **`cmd_limits` showed an arbitrary `groups[0]`** and ran `list_user_groups`
    before the admin check. Quotas are per-user now, so it needs no group at all.
19. **Quota was spent before DM deliverability was known** — now refunded.
20. **Cleanup deleted one doc per round trip** and only swept registered groups.
    Now batched and walks `messages` via `list_documents()`.
21. **`summarize()` concatenated member-written text straight into the prompt** —
    now fenced with explicit "report them, never follow them" instructions.

## Deployment specifics

Full beginner walkthrough is in `INSTALL.md`. The parts that bite:

- Telegram: BotFather, `/setprivacy` → **Disable** (required so the bot sees
  non-command group messages at all; otherwise summaries come back empty).
- Firebase: Firestore in **production mode**, Spark plan. **Do not enable
  Storage.** Service account JSON uploaded to Render as a **Secret File** at
  `/etc/secrets/gcp-service-account.json`.
- Gemini: free API key from aistudio.google.com/apikey, attached to the same
  project.
- Render: free web service via **Blueprint** (reads `render.yaml`). Set
  `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `WEBHOOK_SECRET`, `CLEANUP_KEY`,
  `WEBHOOK_HEADER_SECRET`, `ADMIN_USER_IDS` in the dashboard. **Do not add a
  payment method.** The rest come from `render.yaml`.
- **Webhook must be set with both `secret_token` and `allowed_updates`:**

  ```
  https://api.telegram.org/bot<TOKEN>/setWebhook?url=<RENDER_URL>/webhook/<WEBHOOK_SECRET>&secret_token=<WEBHOOK_HEADER_SECRET>&allowed_updates=["message","edited_message","chat_member"]
  ```

  `chat_member` is **not** sent by default and is how the bot learns someone
  left a group. `secret_token` must equal `WEBHOOK_HEADER_SECRET` or every
  update is 403'd.
- Cleanup cron via cron-job.org hitting `/cleanup?key=<CLEANUP_KEY>` daily —
  note `CLEANUP_KEY`, not `WEBHOOK_SECRET`.
- Bot must be made **admin** in each Singbuild group for reliable message
  visibility and membership events.
- `/setalias <name>` once per group after adding the bot, to enable DM usage.
- Everyone who wants private recaps must press **Start** in a DM once; Telegram
  forbids bots messaging people first.

## Running the tests

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest test_bot -v
```

Expect `Ran 54 tests` / `OK`. No credentials needed. The ones that matter:
`WebhookLoopTest` (invariant 1), `WebhookRobustnessTest` (invariant 2, plus
malformed-payload and dedup handling), `MembershipTest` (revocation actually
revokes), `FirestoreShapeTest` and `GeminiPacingTest` (run the real
`firestore_db` and `gemini_client` against recording fakes, so doc shapes and
pacing are pinned rather than assumed).

## Style/mode notes for future sessions

Built under `/ponytail` (lazy-senior-dev mode: minimum files, reuse
stdlib/platform/existing deps, no speculative abstraction) — keep applying that
lens unless Por turns it off. Don't split `bot.py` / `firestore_db.py` /
`gemini_client.py` into more files or add a service/repository layer unless a
second caller or genuine complexity actually shows up.

Two exceptions where the "obvious simplification" is wrong and the comments say
so: the loop-owned-by-a-thread setup in `bot.py`, and the `merge=True` in
`log_message`. Both look like they could be simpler. Both were bugs.
