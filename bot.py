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
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO

from flask import Flask, request
from telegram import InputFile, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import firestore_db as db
import gemini_client as gemini

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("sb-groupchat-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# Voice notes longer than this are not stored or logged at all.
MAX_VOICE_SECONDS = 180

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


# --- small helpers --------------------------------------------------------


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
        await update.message.reply_text(
            "I couldn't message you privately. Open a private chat with me, "
            "press Start, then run that command again."
        )


# --- registry bookkeeping -------------------------------------------------

# register_group/add_member are idempotent, but writing them on every single
# message is wasteful. One in-process cache keeps it to one write per member
# per container lifetime.
_registry_seen = set()


def _touch_registry(group_id, group_name, user_id):
    key = (group_id, group_name, user_id)
    if key in _registry_seen:
        return
    db.register_group(group_id, group_name)
    db.add_member(group_id, user_id)
    _registry_seen.add(key)


async def _resolve_target_group(update, alias):
    """Which group is this command about? (group_id, name) or (None, error text).

    In a group, that group is the default. From a DM an alias is required, and
    membership is checked -- this is what stops someone DM-querying a project
    group they are not part of.
    """
    user_id = update.effective_user.id

    if alias:
        group_id = db.resolve_alias(alias)
        if group_id is None or not db.is_member(group_id, user_id):
            # Same message either way, so this cannot be used to probe which
            # aliases exist.
            return None, (
                f"I don't know a group called '{alias}' that you're a member of. "
                "An admin sets this with /setalias inside the group."
            )
        group = db.get_group(group_id) or {}
        return group_id, group.get("name") or alias

    if _is_group(update):
        return update.effective_chat.id, update.effective_chat.title or "this group"

    groups = db.list_user_groups(user_id)
    named = [g for g in groups if g[2]]
    if len(named) == 1:
        return named[0][0], named[0][1] or named[0][2]
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

    if not db.check_group_voice_limit(group_id):
        return "[voice transcription limit reached for today]"
    if not db.check_and_increment(group_id, requester_id, "voice"):
        return "[voice transcription limit reached for today]"

    try:
        tg_file = await bot.get_file(file_id)
        audio = bytes(await tg_file.download_as_bytearray())
        raw = gemini.transcribe_and_translate(audio)
    except Exception as exc:
        # Most likely cause: the sender deleted the voice message, so Telegram
        # no longer serves it. Nothing to do but say so and move on.
        log.exception("transcription failed for file_id %s: %s", file_id, exc)
        return "[voice transcription failed]"

    text = _extract_transcript_text(raw)
    # Only charged against the group once the call actually succeeded.
    db.increment_group_voice(group_id)
    db.save_transcript(group_id, message["message_id"], text)
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


# --- command handlers -----------------------------------------------------


async def cmd_start(update, context):
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update, context):
    await update.message.reply_text(HELP_TEXT)


async def cmd_ask(update, context):
    question = " ".join(context.args or []).strip()
    if not question:
        await update.message.reply_text("Ask me something: /ask when is concrete cured")
        return

    user_id = update.effective_user.id
    group_id = update.effective_chat.id  # in DM this is the user's own chat id
    if not db.check_and_increment(group_id, user_id, "ask"):
        await update.message.reply_text(
            f"You've used all {db.DAILY_LIMITS['ask']} of today's /ask requests. "
            "Resets at midnight UTC."
        )
        return

    try:
        answer = gemini.ask(question)
    except Exception as exc:
        log.exception("ask failed: %s", exc)
        await update.message.reply_text("Gemini didn't answer that one. Try again.")
        return

    for part in _chunks(answer or "No answer came back."):
        await update.message.reply_text(part)


async def cmd_summary(update, context):
    alias, period = _parse_group_and_period(context.args)
    group_id, group_name = await _resolve_target_group(update, alias)
    if group_id is None:
        await update.message.reply_text(group_name)
        return

    user_id = update.effective_user.id
    if not db.check_and_increment(group_id, user_id, "summary"):
        await update.message.reply_text(
            f"You've used all {db.DAILY_LIMITS['summary']} of today's /summary "
            "requests. Resets at midnight UTC."
        )
        return

    if _is_group(update):
        await update.message.reply_text("Working on it - I'll send it to you privately.")

    start, end, label = _period_range(period)
    messages = db.get_messages_in_range(group_id, start, end)
    if not messages:
        if not await _dm(context, user_id, f"No messages in {group_name} for {label}."):
            await _tell_dm_failed(update)
        return

    conversation = await _render_conversation(context.bot, group_id, messages, user_id)
    if not conversation.strip():
        if not await _dm(context, user_id, f"Nothing readable in {group_name} for {label}."):
            await _tell_dm_failed(update)
        return

    try:
        summary = gemini.summarize(conversation, group_name, label)
    except Exception as exc:
        log.exception("summarize failed: %s", exc)
        if not await _dm(context, user_id, "Gemini couldn't summarise that. Try again."):
            await _tell_dm_failed(update)
        return

    header = f"{group_name} - {label}\n\n"
    if not await _dm(context, user_id, header + (summary or "(empty summary)")):
        await _tell_dm_failed(update)


async def cmd_export(update, context):
    alias, period = _parse_group_and_period(context.args)
    group_id, group_name = await _resolve_target_group(update, alias)
    if group_id is None:
        await update.message.reply_text(group_name)
        return

    user_id = update.effective_user.id
    if not db.check_and_increment(group_id, user_id, "export"):
        await update.message.reply_text(
            f"You've used all {db.DAILY_LIMITS['export']} of today's /export "
            "requests. Resets at midnight UTC."
        )
        return

    if _is_group(update):
        await update.message.reply_text("Working on it - I'll send the file to you privately.")

    start, end, label = _period_range(period)
    messages = db.get_messages_in_range(group_id, start, end)
    if not messages:
        if not await _dm(context, user_id, f"No messages in {group_name} for {label}."):
            await _tell_dm_failed(update)
        return

    conversation = await _render_conversation(context.bot, group_id, messages, user_id)
    body = f"{group_name} - {label}\nExported {db.utcnow():%Y-%m-%d %H:%M} UTC\n\n{conversation}\n"
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
        await _tell_dm_failed(update)


async def cmd_limits(update, context):
    user_id = update.effective_user.id

    if _is_group(update):
        group_id = update.effective_chat.id
        group_name = update.effective_chat.title or "this group"
    else:
        groups = db.list_user_groups(user_id)
        if not groups:
            group_id, group_name = user_id, "private chat"
        else:
            group_id, group_name = groups[0][0], groups[0][1] or "your group"

    if db.is_admin(user_id):
        await update.message.reply_text("You're an admin - no limits apply to you.")
        return

    usage = db.get_usage(group_id, user_id)
    order = ["ask", "summary", "export", "voice"]
    lines = [f"Your quota for {group_name} today (resets midnight UTC):"]
    for kind in order:
        used, limit = usage[kind]
        label = "voice transcriptions" if kind == "voice" else f"/{kind}"
        lines.append(f"  {label}: {max(limit - used, 0)} of {limit} left")
    await update.message.reply_text("\n".join(lines))


async def cmd_setalias(update, context):
    if not _is_group(update):
        await update.message.reply_text("Run /setalias inside the group you want to name.")
        return

    alias = (context.args[0].strip().lower() if context.args else "")
    if not alias or not re.fullmatch(r"[a-z0-9_-]{2,20}", alias):
        await update.message.reply_text(
            "Pick a short name, 2-20 characters, letters/numbers/dashes only. "
            "Example: /setalias uvp2"
        )
        return
    if alias in ("today", "week"):
        await update.message.reply_text("That name is reserved. Pick another.")
        return

    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("Only group admins can set the group name.")
                return
        except Exception as exc:
            log.warning("get_chat_member failed: %s", exc)
            await update.message.reply_text("I couldn't verify that you're a group admin.")
            return

    existing = db.resolve_alias(alias)
    if existing is not None and int(existing) != int(update.effective_chat.id):
        await update.message.reply_text(f"'{alias}' is already used by another group.")
        return

    group_id = update.effective_chat.id
    db.register_group(group_id, update.effective_chat.title or str(group_id))
    db.set_alias(group_id, alias)
    await update.message.reply_text(
        f"Done. From a private chat with me you can now run:\n"
        f"/summary {alias} today\n/export {alias} week"
    )


# --- silent logging middleware -------------------------------------------


async def log_all_messages(update, context):
    """Store every non-command group message. Voice notes are downloaded and
    parked in Storage; no AI call happens here."""
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
        _touch_registry(group_id, group_name, user.id)

        if message.voice is not None:
            duration = message.voice.duration or 0
            if duration > MAX_VOICE_SECONDS:
                log.info("skipping %ss voice note in %s", duration, group_id)
                return
            # Just record the file_id. Telegram already hosts the audio, so
            # there is nothing to download or upload until someone actually
            # asks for a summary or export covering this note.
            db.log_message(
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
            db.log_message(
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


# --- application + the one event loop ------------------------------------

tg_app = (
    Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()
)
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

# ONE event loop, created once, reused for every webhook call.
#
# Do NOT replace this with asyncio.run() inside the route. asyncio.run() tears
# its loop down on return, but PTB's Application/ExtBot build loop-bound
# internals during initialize(); driving them from a later, different loop
# breaks on the *second* update the bot ever receives, not the first.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
_loop.run_until_complete(tg_app.initialize())

app = Flask(__name__)


@app.route("/")
def health():
    return "sb-groupchat-bot ok", 200


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), tg_app.bot)
    _loop.run_until_complete(tg_app.process_update(update))
    return "ok", 200


@app.route("/cleanup")
def cleanup():
    if request.args.get("key") != WEBHOOK_SECRET:
        return "forbidden", 403
    deleted = db.cleanup_old_messages()
    log.info("cleanup removed %s documents", deleted)
    return f"deleted {deleted}", 200


if __name__ == "__main__":
    # Single-threaded on purpose: _loop is shared and is not thread-safe. If you
    # ever put this behind gunicorn or set threaded=True, hand work to the loop
    # with asyncio.run_coroutine_threadsafe instead of run_until_complete.
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        threaded=False,
    )
