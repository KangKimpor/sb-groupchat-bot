"""SingBuildGroupChatBot -- Telegram + Flask glue.

One deployment serves every Singbuild project group. This is the only file that
imports flask or telegram; persistence lives in firestore_db.py and every AI
call lives in gemini_client.py.

Commands
    /ask <question>                        public answer, or standalone in DM
    /summary [group_alias] [today|week]    recap, always DM'd privately
    /export  [group_alias] [today|week]    .txt log, always DM'd privately
    /limits                                remaining daily quota
    /setalias <name>                       group admins only, enables DM usage
    /help, /start

Non-command group messages are logged silently. For voice notes we record only
Telegram's file_id -- no audio is downloaded or stored on receipt. The audio is
fetched from Telegram and transcribed strictly on demand, when a /summary or
/export range actually touches the note.

Threading model, read before editing:

    The webhook route ACKs Telegram immediately and hands the update to a single
    background thread that owns the one event loop. Telegram gives a webhook
    about a minute to respond and re-delivers anything slower, so a /summary
    that transcribes twenty voice notes must never be processed inside the
    request. See _submit() and the _loop block near the bottom.
"""

import asyncio
import concurrent.futures
import hmac
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO

from flask import Flask, request
from telegram import InputFile, Update
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import firestore_db as db
import gemini_client as gemini

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("sb-groupchat-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Secret embedded in the webhook URL path.
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# Separate secret for /cleanup, because that one travels in a query string and
# therefore lands in cron-job.org's dashboard and in Render's request logs.
# Sharing it with WEBHOOK_SECRET would leak the webhook path into those logs,
# and anyone holding the webhook path can POST forged updates -- including one
# claiming to come from an ADMIN_USER_IDS account, which bypasses every quota.
CLEANUP_KEY = os.environ.get("CLEANUP_KEY") or WEBHOOK_SECRET
if CLEANUP_KEY == WEBHOOK_SECRET:
    log.warning(
        "CLEANUP_KEY is unset, so /cleanup reuses WEBHOOK_SECRET. Set a "
        "separate CLEANUP_KEY to keep the webhook path out of cron and "
        "request logs."
    )

# Optional hardening. Set this and pass the same value as `secret_token` to
# Telegram's setWebhook, and forged POSTs to the webhook path are rejected even
# if the path itself leaks. Left empty the bot behaves as before.
WEBHOOK_HEADER_SECRET = os.environ.get("WEBHOOK_HEADER_SECRET", "")

# Voice notes longer than this are not stored or logged at all.
MAX_VOICE_SECONDS = 180

# Hard ceiling on how many messages one /summary or /export may pull. Without
# it a busy group's week is fully materialised in memory on a 512MB free Render
# instance and then shipped to Gemini in a single request.
MAX_MESSAGES_PER_REQUEST = int(os.environ.get("MAX_MESSAGES_PER_REQUEST", "2000"))

# Used only to decide where "today" starts. Rate limits stay on UTC midnight.
# Singbuild sites run on Cambodia time (UTC+7), so a UTC "today" would silently
# drop every message sent before 7am local.
LOCAL_OFFSET = timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "7")))

TELEGRAM_MAX_CHARS = 3900  # real ceiling is 4096; leave room for formatting

HELP_TEXT = (
    "SingBuild group chat bot\n\n"
    "/ask <question> - ask me anything, I answer in the chat\n"
    "/summary [group] [today|week] - chat recap, sent to you privately\n"
    "/export [group] [today|week] - full chat log as a .txt, sent privately\n"
    "/limits - how much of your daily quota is left\n"
    "/setalias <name> - group admins: give this group a short name so you can "
    "run /summary and /export from a private chat with me\n"
    "/help - this list\n\n"
    "Voice notes are included in summaries and exports, transcribed and "
    "translated to English when needed."
)

# One wording for "no such group" and "you are not in that group", so aliases
# cannot be probed by watching which error comes back.
_NO_SUCH_GROUP = (
    "I don't know a group called '{alias}' that you're a member of. "
    "An admin sets this with /setalias inside the group."
)


# --- small helpers --------------------------------------------------------


async def _off_loop(fn, *args, **kwargs):
    """Run a blocking Firestore or Gemini call in a worker thread.

    Everything in firestore_db and gemini_client is synchronous. Calling it
    directly from a handler pins the shared event loop for the duration, so one
    slow summary would stall every other project group. Handing it to a thread
    keeps the loop free to interleave other updates.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def _is_group(update):
    chat = update.effective_chat
    return chat is not None and chat.type in ("group", "supergroup")


def _parse_group_and_period(args):
    """`[group_alias] [today|week]`, both optional, order-insensitive.

    Returns (alias_or_None, "today"|"week").
    """
    alias = None
    period = "today"
    for arg in args or []:
        low = arg.strip().lower()
        if not low:
            continue
        if low in ("today", "week"):
            period = low
        elif alias is None:
            alias = low
    return alias, period


def _period_range(period):
    """(start, end, human_label) as timezone-aware UTC datetimes."""
    now = db.utcnow()
    if period == "week":
        return now - timedelta(days=7), now, "the last 7 days"
    local_midnight = (now + LOCAL_OFFSET).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight - LOCAL_OFFSET, now, "today"


_TEXT_RE = re.compile(r"TEXT:\s*(.+)", re.DOTALL)


def _extract_transcript_text(raw):
    """Pull the English text out of Gemini's `LANGUAGE:` / `TEXT:` reply.

    If Gemini ever changes response shape this regex is the first thing to
    check -- but fall back to the whole reply rather than losing the content.
    """
    raw = (raw or "").strip()
    match = _TEXT_RE.search(raw)
    text = (match.group(1).strip() if match else raw).strip()
    return text or "[empty transcription]"


def _chunks(text, size=TELEGRAM_MAX_CHARS):
    for i in range(0, max(len(text), 1), size):
        yield text[i : i + size]


async def _reply(update, text):
    """Reply in the originating chat.

    Uses effective_message, not message. PTB dispatches commands on
    effective_message, so editing a message into `/help` produces an update
    where `message` is None -- reaching for it raises AttributeError and the
    user silently gets nothing back.
    """
    message = update.effective_message
    if message is None:
        log.warning("no message to reply to on update %s", update.update_id)
        return
    for part in _chunks(text):
        await message.reply_text(part)


async def _dm(context, user_id, text):
    """Returns False if the user has never started a private chat with the bot."""
    try:
        for part in _chunks(text):
            await context.bot.send_message(chat_id=user_id, text=part)
        return True
    except Exception as exc:
        log.warning("DM to %s failed: %s", user_id, exc)
        return False


async def _tell_dm_failed(update):
    if _is_group(update):
        await _reply(
            update,
            "I couldn't message you privately. Open a private chat with me, "
            "press Start, then run that command again.",
        )


# --- registry bookkeeping -------------------------------------------------

# register_group/add_member are idempotent, but writing them on every single
# message is wasteful. One in-process cache keeps it to one write per member
# per container lifetime. Guarded by a lock because updates are now processed
# on the loop thread while Flask serves requests on another.
_registry_seen = set()
_registry_lock = threading.Lock()


def _touch_registry(group_id, group_name, user_id):
    key = (group_id, group_name, user_id)
    with _registry_lock:
        if key in _registry_seen:
            return
    db.register_group(group_id, group_name)
    db.add_member(group_id, user_id)
    with _registry_lock:
        _registry_seen.add(key)


def _forget_member(group_id, user_id):
    """Drop cached registry entries so a rejoin writes membership again."""
    with _registry_lock:
        for key in [k for k in _registry_seen if k[0] == group_id and k[2] == user_id]:
            _registry_seen.discard(key)


async def _still_in_group(bot, group_id, user_id):
    """Confirm membership against Telegram rather than against our own cache.

    Firestore's member_ids list only ever grew: ArrayUnion on every message and
    nothing to undo it. That made someone removed from a project group able to
    keep DM-querying it forever. Telegram is the authority, so ask Telegram.
    Fails closed -- if the check itself errors, access is refused.
    """
    try:
        member = await bot.get_chat_member(group_id, user_id)
    except Exception as exc:
        log.warning("get_chat_member(%s, %s) failed: %s", group_id, user_id, exc)
        return False

    if getattr(member, "status", None) in ("left", "kicked"):
        await _off_loop(db.remove_member, group_id, user_id)
        _forget_member(group_id, user_id)
        return False
    return True


async def _resolve_target_group(update, context, alias):
    """Which group is this command about? (group_id, name) or (None, error text).

    An explicit alias always wins, and is always membership-checked. Inside a
    group with no alias, that group is the default -- the caller is demonstrably
    present. From a DM with no alias we fall back to the caller's single aliased
    group, still membership-checked.
    """
    user_id = update.effective_user.id

    if alias:
        group_id = await _off_loop(db.resolve_alias, alias)
        if group_id is None or not await _off_loop(db.is_member, group_id, user_id):
            return None, _NO_SUCH_GROUP.format(alias=alias)
        if not await _still_in_group(context.bot, group_id, user_id):
            return None, _NO_SUCH_GROUP.format(alias=alias)
        group = await _off_loop(db.get_group, group_id) or {}
        return group_id, group.get("name") or alias

    if _is_group(update):
        return update.effective_chat.id, update.effective_chat.title or "this group"

    groups = await _off_loop(db.list_user_groups, user_id)
    named = [g for g in groups if g[2]]
    if len(named) == 1:
        group_id, name, only_alias = named[0]
        if not await _still_in_group(context.bot, group_id, user_id):
            return None, _NO_SUCH_GROUP.format(alias=only_alias)
        return group_id, name or only_alias
    if not named:
        return None, (
            "Run this inside a project group, or ask a group admin to set a "
            "short name for it with /setalias first."
        )
    options = ", ".join(sorted(g[2] for g in named))
    return None, f"Which group? Add its short name: {options}"


# --- voice transcription (lazy, on demand only) ---------------------------


async def _transcribe_voice_msg(bot, group_id, message, requester_id):
    """Transcribe one voice note, or explain why we didn't.

    The audio is fetched from Telegram at this point rather than from our own
    storage -- we keep only the file_id. Telegram's download links expire after
    an hour, but calling get_file again with the same file_id mints a fresh one,
    so the id stays usable for the whole 10-day retention window.

    Cached transcripts short-circuit, so a note is never sent to Gemini twice.
    Both the per-user (15/day) and the group-wide (200/day) voice caps are
    enforced here -- the /summary and /export caps alone would not stop a single
    call whose range happens to contain fifty voice notes.
    """
    cached = message.get("transcript")
    if cached:
        return cached

    file_id = message.get("file_id")
    if not file_id:
        return "[voice note: audio unavailable]"

    if not await _off_loop(db.check_group_voice_limit, group_id):
        return "[voice transcription limit reached for today]"
    if not await _off_loop(db.check_and_increment, requester_id, "voice"):
        return "[voice transcription limit reached for today]"

    try:
        tg_file = await bot.get_file(file_id)
        audio = bytes(await tg_file.download_as_bytearray())
        raw = await _off_loop(gemini.transcribe_and_translate, audio)
    except Exception as exc:
        # Most likely cause: the sender deleted the voice message, so Telegram
        # no longer serves it. Nothing to do but say so and move on.
        log.exception("transcription failed for file_id %s: %s", file_id, exc)
        return "[voice transcription failed]"

    text = _extract_transcript_text(raw)
    # Only charged against the group once the call actually succeeded.
    await _off_loop(db.increment_group_voice, group_id)
    await _off_loop(db.save_transcript, group_id, message["message_id"], text)
    return text


async def _render_conversation(bot, group_id, messages, requester_id):
    """Messages -> flat text log, transcribing any voice notes in range."""
    lines = []
    for msg in messages:
        ts = msg.get("ts")
        stamp = (
            ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            if isinstance(ts, datetime)
            else "?"
        )
        who = msg.get("username") or str(msg.get("user_id"))
        if msg.get("kind") == "voice":
            body = "(voice) " + await _transcribe_voice_msg(
                bot, group_id, msg, requester_id
            )
        else:
            body = msg.get("text") or ""
        if body.strip():
            lines.append(f"[{stamp} UTC] {who}: {body}")
    return "\n".join(lines)


async def _fetch_range(group_id, period):
    """(messages, label, truncation_notice) for a period, capped in size."""
    start, end, label = _period_range(period)
    messages = await _off_loop(
        db.get_messages_in_range,
        group_id,
        start,
        end,
        MAX_MESSAGES_PER_REQUEST,
    )
    notice = ""
    if len(messages) >= MAX_MESSAGES_PER_REQUEST:
        notice = (
            f"\n\n(Only the most recent {MAX_MESSAGES_PER_REQUEST} messages of "
            f"{label} are included -- the group was busier than that.)"
        )
    return messages, label, notice


async def _deliver_or_refund(update, context, user_id, kind, text):
    """DM the result, and hand the quota back if the DM could not be delivered.

    Charging someone a /summary for an error message they never received is
    just rude, and the most common cause is simply never having pressed Start.
    """
    if await _dm(context, user_id, text):
        return
    await _off_loop(db.refund_usage, user_id, kind)
    await _tell_dm_failed(update)


# --- command handlers -----------------------------------------------------


async def cmd_start(update, context):
    await _reply(update, HELP_TEXT)


async def cmd_help(update, context):
    await _reply(update, HELP_TEXT)


async def cmd_ask(update, context):
    question = " ".join(context.args or []).strip()
    if not question:
        await _reply(update, "Ask me something: /ask when is concrete cured")
        return

    user_id = update.effective_user.id
    if not await _off_loop(db.check_and_increment, user_id, "ask"):
        await _reply(
            update,
            f"You've used all {db.DAILY_LIMITS['ask']} of today's /ask requests. "
            "Resets at midnight UTC.",
        )
        return

    try:
        answer = await _off_loop(gemini.ask, question)
    except Exception as exc:
        log.exception("ask failed: %s", exc)
        await _off_loop(db.refund_usage, user_id, "ask")
        await _reply(update, "Gemini didn't answer that one. Try again.")
        return

    await _reply(update, answer or "No answer came back.")


async def cmd_summary(update, context):
    alias, period = _parse_group_and_period(context.args)
    group_id, group_name = await _resolve_target_group(update, context, alias)
    if group_id is None:
        await _reply(update, group_name)
        return

    user_id = update.effective_user.id
    if not await _off_loop(db.check_and_increment, user_id, "summary"):
        await _reply(
            update,
            f"You've used all {db.DAILY_LIMITS['summary']} of today's /summary "
            "requests. Resets at midnight UTC.",
        )
        return

    if _is_group(update):
        await _reply(update, "Working on it - I'll send it to you privately.")

    messages, label, notice = await _fetch_range(group_id, period)
    if not messages:
        await _deliver_or_refund(
            update,
            context,
            user_id,
            "summary",
            f"No messages in {group_name} for {label}.",
        )
        return

    conversation = await _render_conversation(context.bot, group_id, messages, user_id)
    if not conversation.strip():
        await _deliver_or_refund(
            update,
            context,
            user_id,
            "summary",
            f"Nothing readable in {group_name} for {label}.",
        )
        return

    try:
        summary = await _off_loop(gemini.summarize, conversation, group_name, label)
    except Exception as exc:
        log.exception("summarize failed: %s", exc)
        await _deliver_or_refund(
            update,
            context,
            user_id,
            "summary",
            "Gemini couldn't summarise that. Try again.",
        )
        return

    header = f"{group_name} - {label}\n\n"
    await _deliver_or_refund(
        update,
        context,
        user_id,
        "summary",
        header + (summary or "(empty summary)") + notice,
    )


async def cmd_export(update, context):
    alias, period = _parse_group_and_period(context.args)
    group_id, group_name = await _resolve_target_group(update, context, alias)
    if group_id is None:
        await _reply(update, group_name)
        return

    user_id = update.effective_user.id
    if not await _off_loop(db.check_and_increment, user_id, "export"):
        await _reply(
            update,
            f"You've used all {db.DAILY_LIMITS['export']} of today's /export "
            "requests. Resets at midnight UTC.",
        )
        return

    if _is_group(update):
        await _reply(update, "Working on it - I'll send the file to you privately.")

    messages, label, notice = await _fetch_range(group_id, period)
    if not messages:
        await _deliver_or_refund(
            update,
            context,
            user_id,
            "export",
            f"No messages in {group_name} for {label}.",
        )
        return

    conversation = await _render_conversation(context.bot, group_id, messages, user_id)
    body = (
        f"{group_name} - {label}\n"
        f"Exported {db.utcnow():%Y-%m-%d %H:%M} UTC{notice}\n\n"
        f"{conversation}\n"
    )
    safe_name = re.sub(r"[^A-Za-z0-9]+", "-", group_name).strip("-").lower() or "group"
    filename = f"{safe_name}-{period}-{db.utcnow():%Y%m%d}.txt"

    try:
        await context.bot.send_document(
            chat_id=user_id,
            document=InputFile(BytesIO(body.encode("utf-8")), filename=filename),
            caption=f"{group_name} - {label}",
        )
    except Exception as exc:
        log.warning("export DM to %s failed: %s", user_id, exc)
        await _off_loop(db.refund_usage, user_id, "export")
        await _tell_dm_failed(update)


async def cmd_limits(update, context):
    user_id = update.effective_user.id

    if await _off_loop(db.is_admin, user_id):
        await _reply(update, "You're an admin - no limits apply to you.")
        return

    # Quotas are per person per day, not per group, so there is no group to
    # pick here and nothing to look up in the registry.
    usage = await _off_loop(db.get_usage, user_id)
    lines = ["Your quota today (resets midnight UTC):"]
    for kind in ("ask", "summary", "export", "voice"):
        used, limit = usage[kind]
        label = "voice transcriptions" if kind == "voice" else f"/{kind}"
        lines.append(f"  {label}: {max(limit - used, 0)} of {limit} left")
    await _reply(update, "\n".join(lines))


async def cmd_setalias(update, context):
    if not _is_group(update):
        await _reply(update, "Run /setalias inside the group you want to name.")
        return

    alias = context.args[0].strip().lower() if context.args else ""
    if not alias or not re.fullmatch(r"[a-z0-9_-]{2,20}", alias):
        await _reply(
            update,
            "Pick a short name, 2-20 characters, letters/numbers/dashes only. "
            "Example: /setalias uvp2",
        )
        return
    if alias in ("today", "week"):
        await _reply(update, "That name is reserved. Pick another.")
        return

    user_id = update.effective_user.id
    if not await _off_loop(db.is_admin, user_id):
        try:
            member = await context.bot.get_chat_member(
                update.effective_chat.id, user_id
            )
            if member.status not in ("administrator", "creator"):
                await _reply(update, "Only group admins can set the group name.")
                return
        except Exception as exc:
            log.warning("get_chat_member failed: %s", exc)
            await _reply(update, "I couldn't verify that you're a group admin.")
            return

    existing = await _off_loop(db.resolve_alias, alias)
    if existing is not None and int(existing) != int(update.effective_chat.id):
        await _reply(update, f"'{alias}' is already used by another group.")
        return

    group_id = update.effective_chat.id
    await _off_loop(
        db.register_group, group_id, update.effective_chat.title or str(group_id)
    )
    await _off_loop(db.set_alias, group_id, alias)
    # Without this, an admin who only ever ran /setalias and never chatted is
    # not a member of their own group, so their DM queries are refused.
    await _off_loop(db.add_member, group_id, user_id)
    await _reply(
        update,
        f"Done. From a private chat with me you can now run:\n"
        f"/summary {alias} today\n/export {alias} week",
    )


# --- silent logging middleware -------------------------------------------


async def log_all_messages(update, context):
    """Store every non-command group message.

    Voice notes are recorded as a Telegram file_id and nothing else: no
    download, no upload, no AI call, no reply in the chat. We deliberately
    store no audio -- see the note at the top of firestore_db.py.
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or not _is_group(update):
        return

    user = update.effective_user
    if user is None:
        return

    group_id = chat.id
    group_name = chat.title or str(group_id)
    username = user.username or user.full_name or str(user.id)

    try:
        await _off_loop(_touch_registry, group_id, group_name, user.id)

        if message.voice is not None:
            duration = message.voice.duration or 0
            if duration > MAX_VOICE_SECONDS:
                log.info("skipping %ss voice note in %s", duration, group_id)
                return
            # Just record the file_id. Telegram already hosts the audio, so
            # there is nothing to download or upload until someone actually
            # asks for a summary or export covering this note.
            await _off_loop(
                db.log_message,
                group_id,
                message.message_id,
                user.id,
                username,
                kind="voice",
                file_id=message.voice.file_id,
                duration=duration,
                ts=message.date or db.utcnow(),
            )
        elif message.text:
            await _off_loop(
                db.log_message,
                group_id,
                message.message_id,
                user.id,
                username,
                text=message.text,
                kind="text",
                ts=message.date or db.utcnow(),
            )
    except Exception as exc:
        # Logging must never break the chat.
        log.exception("failed to log message %s: %s", message.message_id, exc)


async def on_members_joined(update, context):
    """Record joins directly, so membership does not depend on posting first."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or not _is_group(update):
        return
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        try:
            await _off_loop(
                _touch_registry, chat.id, chat.title or str(chat.id), member.id
            )
        except Exception as exc:
            log.warning("could not record join for %s: %s", member.id, exc)


async def on_member_left(update, context):
    """Revoke DM access when someone leaves or is removed from a group."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or not _is_group(update):
        return
    member = message.left_chat_member
    if member is None or member.is_bot:
        return
    await _revoke(chat.id, member.id)


async def on_chat_member_update(update, context):
    """Same revocation, via the richer chat_member update.

    Service messages for leaving are not always emitted (large supergroups
    suppress them), so this is the reliable path. It requires `chat_member` in
    the setWebhook allowed_updates list -- see INSTALL.md.
    """
    event = update.chat_member
    if event is None or event.chat is None or event.new_chat_member is None:
        return
    member = event.new_chat_member
    user = getattr(member, "user", None)
    if user is None or user.is_bot:
        return
    if member.status in ("left", "kicked"):
        await _revoke(event.chat.id, user.id)


async def _revoke(group_id, user_id):
    try:
        await _off_loop(db.remove_member, group_id, user_id)
        _forget_member(group_id, user_id)
        log.info("revoked DM access for %s in group %s", user_id, group_id)
    except Exception as exc:
        log.warning("could not revoke %s in %s: %s", user_id, group_id, exc)


async def on_error(update, context):
    """Last resort. Without this PTB logs 'No error handlers are registered'
    and the caller just gets silence."""
    log.error(
        "unhandled error processing update: %s", context.error, exc_info=context.error
    )
    try:
        if isinstance(update, Update) and update.effective_message is not None:
            await update.effective_message.reply_text(
                "Something went wrong on my side. Try that again in a moment."
            )
    except Exception:
        pass


# --- application + the one event loop ------------------------------------

tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("help", cmd_help))
tg_app.add_handler(CommandHandler("ask", cmd_ask))
tg_app.add_handler(CommandHandler("summary", cmd_summary))
tg_app.add_handler(CommandHandler("export", cmd_export))
tg_app.add_handler(CommandHandler("limits", cmd_limits))
tg_app.add_handler(CommandHandler("setalias", cmd_setalias))
tg_app.add_handler(
    MessageHandler(
        filters.ChatType.GROUPS & ((filters.TEXT & ~filters.COMMAND) | filters.VOICE),
        log_all_messages,
    ),
    group=1,
)
tg_app.add_handler(
    MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_members_joined), group=1
)
tg_app.add_handler(
    MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_member_left), group=1
)
tg_app.add_handler(
    ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER), group=1
)
tg_app.add_error_handler(on_error)

# ONE event loop, created once, owned by exactly one thread.
#
# Do NOT replace this with asyncio.run() inside the route. asyncio.run() tears
# its loop down on return, but PTB's Application/ExtBot build loop-bound
# internals during initialize(); driving them from a later, different loop
# breaks on the *second* update the bot ever receives, not the first.
#
# initialize() runs on this loop before the thread takes it over, so every PTB
# internal stays bound to the one loop that will drive it. Flask never touches
# the loop directly -- it only ever posts work with run_coroutine_threadsafe,
# which is the thread-safe entry point.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
_loop.run_until_complete(tg_app.initialize())

_loop_thread = threading.Thread(
    target=_loop.run_forever, name="ptb-event-loop", daemon=True
)
_loop_thread.start()

_pending = set()
_pending_lock = threading.Lock()


def _submit(coro):
    """Hand a coroutine to the loop thread and return without waiting.

    This is what keeps the webhook fast. Telegram allows a webhook roughly a
    minute to answer and re-delivers anything slower, so processing inline meant
    a /summary over twenty voice notes got replayed: quota charged twice, DM
    sent twice, and every other group stalled meanwhile.
    """
    future = asyncio.run_coroutine_threadsafe(coro, _loop)

    def _done(fut):
        with _pending_lock:
            _pending.discard(fut)
        try:
            exc = fut.exception()
        except concurrent.futures.CancelledError:
            return
        if exc is not None:
            log.error("update processing failed: %s", exc, exc_info=exc)

    with _pending_lock:
        _pending.add(future)
    future.add_done_callback(_done)
    return future


def wait_for_idle(timeout=30.0):
    """Block until in-flight updates finish. Used by the tests."""
    deadline = time.monotonic() + timeout
    while True:
        with _pending_lock:
            futures = list(_pending)
        if not futures:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with _pending_lock:
                return not _pending
        concurrent.futures.wait(futures, timeout=remaining)


# Telegram re-delivers an update it thinks failed, and PTB does not deduplicate.
# A bounded ring of seen ids makes a replay a no-op instead of a second charge
# against someone's quota. Losing this on a restart is fine -- retries arrive
# within minutes, well inside one container's life.
_SEEN_LIMIT = 2048
_seen_ids = {}
_seen_order = []
_seen_lock = threading.Lock()


def _already_handled(update_id):
    if update_id is None:
        return False
    with _seen_lock:
        if update_id in _seen_ids:
            return True
        _seen_ids[update_id] = True
        _seen_order.append(update_id)
        while len(_seen_order) > _SEEN_LIMIT:
            _seen_ids.pop(_seen_order.pop(0), None)
        return False


app = Flask(__name__)


@app.route("/")
def health():
    return "sb-groupchat-bot ok", 200


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    if WEBHOOK_HEADER_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(header, WEBHOOK_HEADER_SECRET):
            return "forbidden", 403

    payload = request.get_json(silent=True, force=True)
    if not isinstance(payload, dict):
        log.warning("webhook received a body that was not a JSON object")
        return "ignored", 200

    # A parse failure must still ACK. PTB raises on update shapes it doesn't
    # know -- a new Bot API required field is enough -- and answering non-2xx
    # makes Telegram retry that same update indefinitely, wedging the queue.
    try:
        update = Update.de_json(payload, tg_app.bot)
    except Exception as exc:
        log.warning("could not parse update %s: %s", payload.get("update_id"), exc)
        return "ignored", 200
    if update is None:
        return "ignored", 200

    if _already_handled(update.update_id):
        log.info("ignoring duplicate delivery of update %s", update.update_id)
        return "ok", 200

    _submit(tg_app.process_update(update))
    return "ok", 200


@app.route("/cleanup")
def cleanup():
    if not hmac.compare_digest(request.args.get("key", ""), CLEANUP_KEY):
        return "forbidden", 403
    deleted = db.cleanup_old_messages()
    log.info("cleanup removed %s documents", deleted)
    return f"deleted {deleted}", 200


if __name__ == "__main__":
    # threaded=False on purpose. Update processing already happens on the loop
    # thread, so the web server has nothing to gain from concurrency here, and
    # one worker keeps memory well inside the free instance.
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        threaded=False,
    )
