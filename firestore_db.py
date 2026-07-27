"""All persistence for SingBuildGroupChatBot: Firestore only.

One module because it is all "talk to Firestore", and there is exactly one
caller (bot.py). Layout:

    groups/{group_id}                    -> name, alias, member_ids[]
    messages/{group_id}/log/{message_id} -> author, text|transcript, ts, file_id
    usage/{group_id}_{user_id}_{date}    -> per-user daily command counters
    usage/group_{group_id}_{date}        -> group-wide daily voice counter

We deliberately store NO audio. Voice notes are kept only as the Telegram
`file_id`, and the audio is fetched from Telegram when a transcription is
actually needed. Cloud Storage for Firebase would have required the Blaze
plan (billing account) since September 2024, and linking billing would also
have dropped the project off the Gemini API free tier -- so a second copy of
audio Telegram already hosts was pure cost for no benefit.

Firestore query note: every query here is a single-field filter, and the only
one that combines a range with an order_by orders by that *same* field (`ts`).
That needs no composite index. Do not add a query that filters on one field and
orders by a different one without creating the index first.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

_log = logging.getLogger("sb-groupchat-bot.db")

# --- policy ---------------------------------------------------------------

RETENTION_DAYS = 10

# Per user, per day. Resets at UTC midnight because the doc id carries the date.
DAILY_LIMITS = {
    "ask": 20,
    "summary": 5,
    "export": 3,
    "voice": 15,
}

# Whole-group backstop on transcriptions, so one group cannot burn the shared
# Gemini free-tier quota for every other Singbuild project.
GROUP_VOICE_DAILY_LIMIT = 200

def _parse_admin_ids(raw):
    """Tolerant on purpose. A typo like `@por` or `123,,456` in a Render env var
    must not crash the whole service at import with a bare ValueError -- that
    failure looks nothing like its cause. Skip the bad entry, log it, carry on.
    """
    ids = set()
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            _log.warning(
                "ADMIN_USER_IDS: ignoring %r, not a numeric Telegram user id "
                "(get yours from @userinfobot)",
                part,
            )
    return ids


_ADMIN_IDS = _parse_admin_ids(os.environ.get("ADMIN_USER_IDS", ""))

# --- client (lazy so importing this module never needs credentials) --------

_db = None


def db():
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def utcnow():
    return datetime.now(timezone.utc)


def _today():
    return utcnow().strftime("%Y-%m-%d")


def is_admin(user_id):
    return int(user_id) in _ADMIN_IDS


# --- messages -------------------------------------------------------------


def _log_col(group_id):
    return db().collection("messages").document(str(group_id)).collection("log")


def log_message(
    group_id,
    message_id,
    user_id,
    username,
    text=None,
    kind="text",
    file_id=None,
    duration=None,
    ts=None,
):
    """Record one message. Voice notes land here with transcript=None and just a
    Telegram file_id; they are only fetched and transcribed later, on demand, by
    /summary or /export."""
    _log_col(group_id).document(str(message_id)).set(
        {
            "message_id": int(message_id),
            "user_id": int(user_id),
            "username": username or str(user_id),
            "kind": kind,
            "text": text,
            "file_id": file_id,
            "duration": duration,
            "transcript": None,
            "ts": ts or utcnow(),
        }
    )


def save_transcript(group_id, message_id, transcript):
    """Cache a transcript so the same voice note is never sent to Gemini twice."""
    _log_col(group_id).document(str(message_id)).update({"transcript": transcript})


def get_messages_in_range(group_id, start, end):
    """Messages between two datetimes, oldest first."""
    query = (
        _log_col(group_id)
        .where(filter=firestore.FieldFilter("ts", ">=", start))
        .where(filter=firestore.FieldFilter("ts", "<=", end))
        .order_by("ts")
    )
    return [doc.to_dict() for doc in query.stream()]


def cleanup_old_messages():
    """Drop everything older than RETENTION_DAYS: message docs, cached
    transcripts and stale usage counters. Called by the /cleanup route.

    There is no audio to delete -- we never stored any. Dropping the doc drops
    the file_id, so the bot loses its reference to the audio too.
    """
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    deleted = 0

    for group in db().collection("groups").stream():
        stale = _log_col(group.id).where(
            filter=firestore.FieldFilter("ts", "<", cutoff)
        )
        for doc in stale.stream():
            doc.reference.delete()
            deleted += 1

    cutoff_day = cutoff.strftime("%Y-%m-%d")
    stale_usage = db().collection("usage").where(
        filter=firestore.FieldFilter("date", "<", cutoff_day)
    )
    for doc in stale_usage.stream():
        doc.reference.delete()
        deleted += 1

    return deleted


# --- group registry -------------------------------------------------------


def register_group(group_id, name):
    db().collection("groups").document(str(group_id)).set(
        {"group_id": int(group_id), "name": name}, merge=True
    )


def set_alias(group_id, alias):
    db().collection("groups").document(str(group_id)).set(
        {"alias": alias.lower()}, merge=True
    )


def add_member(group_id, user_id):
    db().collection("groups").document(str(group_id)).set(
        {"member_ids": firestore.ArrayUnion([int(user_id)])}, merge=True
    )


def resolve_alias(alias):
    """alias -> group_id, or None."""
    query = (
        db()
        .collection("groups")
        .where(filter=firestore.FieldFilter("alias", "==", alias.lower()))
        .limit(1)
    )
    for doc in query.stream():
        return int(doc.id)
    return None


def get_group(group_id):
    snap = db().collection("groups").document(str(group_id)).get()
    return snap.to_dict() if snap.exists else None


def is_member(group_id, user_id):
    """The access control boundary for DM-based group commands."""
    group = get_group(group_id)
    if not group:
        return False
    return int(user_id) in (group.get("member_ids") or [])


def list_user_groups(user_id):
    """[(group_id, name, alias)] for every group we've seen this user post in."""
    query = (
        db()
        .collection("groups")
        .where(filter=firestore.FieldFilter("member_ids", "array_contains", int(user_id)))
    )
    out = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        out.append((int(doc.id), data.get("name"), data.get("alias")))
    return out


# --- rate limiting --------------------------------------------------------


def _usage_ref(group_id, user_id):
    return db().collection("usage").document(f"{group_id}_{user_id}_{_today()}")


def _group_usage_ref(group_id):
    return db().collection("usage").document(f"group_{group_id}_{_today()}")


def check_and_increment(group_id, user_id, kind):
    """Consume one unit of a user's daily quota. True if allowed, False if spent.

    Transactional so two messages arriving together cannot both slip past the
    last remaining unit. Admins bypass entirely.
    """
    if is_admin(user_id):
        return True

    limit = DAILY_LIMITS[kind]
    ref = _usage_ref(group_id, user_id)
    today = _today()

    @firestore.transactional
    def run(txn):
        snap = ref.get(transaction=txn)
        data = snap.to_dict() if snap.exists else {}
        used = int((data or {}).get(kind, 0))
        if used >= limit:
            return False
        txn.set(
            ref,
            {
                "date": today,
                "group_id": int(group_id),
                "user_id": int(user_id),
                kind: used + 1,
            },
            merge=True,
        )
        return True

    return run(db().transaction())


def get_usage(group_id, user_id):
    """{kind: (used, limit)} for the /limits command."""
    snap = _usage_ref(group_id, user_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    return {
        kind: (int(data.get(kind, 0)), limit) for kind, limit in DAILY_LIMITS.items()
    }


def check_group_voice_limit(group_id):
    """True while the group still has transcriptions left today."""
    snap = _group_usage_ref(group_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    return int(data.get("voice", 0)) < GROUP_VOICE_DAILY_LIMIT


def increment_group_voice(group_id):
    _group_usage_ref(group_id).set(
        {"date": _today(), "voice": firestore.Increment(1)}, merge=True
    )
