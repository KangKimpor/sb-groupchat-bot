"""All persistence for SingBuildGroupChatBot: Firestore only.

One module because it is all "talk to Firestore", and there is exactly one
caller (bot.py). Layout:

    groups/{group_id}                    -> name, alias, member_ids[]
    messages/{group_id}/log/{message_id} -> author, text|transcript, ts, file_id
    usage/u{user_id}_{date}              -> per-user daily command counters
    usage/group_{group_id}_{date}        -> group-wide daily voice counter

Note the usage key: quotas are per person per day, full stop. They used to be
keyed per group as well, which quietly multiplied everyone's allowance by the
number of groups they were in -- and since the whole point of these caps is
staying inside the Gemini free tier, that mattered.

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

import os
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

# --- policy ---------------------------------------------------------------

RETENTION_DAYS = 10

# Per user, per day, across every group. Resets at UTC midnight because the doc
# id carries the date.
DAILY_LIMITS = {
    "ask": 20,
    "summary": 5,
    "export": 3,
    "voice": 15,
}

# Whole-group backstop on transcriptions, so one group cannot burn the shared
# Gemini free-tier quota for every other Singbuild project.
GROUP_VOICE_DAILY_LIMIT = 200

# Firestore caps a write batch at 500 operations; stay under it.
_BATCH_LIMIT = 400

_ADMIN_IDS = {
    int(part)
    for part in os.environ.get("ADMIN_USER_IDS", "").replace(" ", "").split(",")
    if part
}

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
    """Record one message. Voice notes land here with just a Telegram file_id;
    they are only fetched and transcribed later, on demand, by /summary or
    /export.

    merge=True, and `transcript` is deliberately absent from the payload. The
    same message_id can legitimately arrive twice -- an edit, or a Telegram
    re-delivery -- and a plain .set() carrying transcript=None wiped the cached
    transcription, so the note got sent to Gemini a second time.
    """
    _log_col(group_id).document(str(message_id)).set(
        {
            "message_id": int(message_id),
            "user_id": int(user_id),
            "username": username or str(user_id),
            "kind": kind,
            "text": text,
            "file_id": file_id,
            "duration": duration,
            "ts": ts or utcnow(),
        },
        merge=True,
    )


def save_transcript(group_id, message_id, transcript):
    """Cache a transcript so the same voice note is never sent to Gemini twice."""
    _log_col(group_id).document(str(message_id)).set(
        {"transcript": transcript}, merge=True
    )


def get_messages_in_range(group_id, start, end, limit=2000):
    """Up to `limit` messages between two datetimes, oldest first.

    Ordered descending in the query so that hitting the cap keeps the *most
    recent* messages rather than the oldest, then reversed for reading. Still a
    single-field range plus an order_by on that same field, so still no
    composite index.
    """
    query = (
        _log_col(group_id)
        .where(filter=firestore.FieldFilter("ts", ">=", start))
        .where(filter=firestore.FieldFilter("ts", "<=", end))
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(int(limit))
    )
    messages = [doc.to_dict() for doc in query.stream()]
    messages.reverse()
    return messages


def _delete_query(query):
    """Delete everything a query returns, in batches. Returns the count."""
    deleted = 0
    batch = db().batch()
    pending = 0
    for doc in query.stream():
        batch.delete(doc.reference)
        pending += 1
        deleted += 1
        if pending >= _BATCH_LIMIT:
            batch.commit()
            batch = db().batch()
            pending = 0
    if pending:
        batch.commit()
    return deleted


def cleanup_old_messages():
    """Drop everything older than RETENTION_DAYS: message docs, cached
    transcripts and stale usage counters. Called by the /cleanup route.

    There is no audio to delete -- we never stored any. Dropping the doc drops
    the file_id, so the bot loses its reference to the audio too.

    Walks the `messages` collection rather than `groups`, so a log left behind
    by a group that never made it into the registry still gets swept.
    """
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    deleted = 0

    for parent in db().collection("messages").list_documents():
        deleted += _delete_query(
            parent.collection("log").where(
                filter=firestore.FieldFilter("ts", "<", cutoff)
            )
        )

    cutoff_day = cutoff.strftime("%Y-%m-%d")
    deleted += _delete_query(
        db()
        .collection("usage")
        .where(filter=firestore.FieldFilter("date", "<", cutoff_day))
    )

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


def remove_member(group_id, user_id):
    """Revoke DM access to a group's history.

    Without this the member list only ever grew, so somebody removed from a
    project group kept the ability to DM the bot for that group's recaps.
    """
    db().collection("groups").document(str(group_id)).set(
        {"member_ids": firestore.ArrayRemove([int(user_id)])}, merge=True
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
    """First gate for DM-based group commands.

    Treat this as a cheap hint, not the whole boundary: bot.py confirms with
    Telegram via get_chat_member before handing over any history.
    """
    group = get_group(group_id)
    if not group:
        return False
    return int(user_id) in (group.get("member_ids") or [])


def list_user_groups(user_id):
    """[(group_id, name, alias)] for every group we currently believe this user
    belongs to."""
    query = (
        db()
        .collection("groups")
        .where(
            filter=firestore.FieldFilter("member_ids", "array_contains", int(user_id))
        )
    )
    out = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        out.append((int(doc.id), data.get("name"), data.get("alias")))
    return out


# --- rate limiting --------------------------------------------------------


def _usage_ref(user_id):
    """Per person, per day -- not per group. See the module docstring."""
    return db().collection("usage").document(f"u{user_id}_{_today()}")


def _group_usage_ref(group_id):
    return db().collection("usage").document(f"group_{group_id}_{_today()}")


def check_and_increment(user_id, kind):
    """Consume one unit of a user's daily quota. True if allowed, False if spent.

    Transactional so two messages arriving together cannot both slip past the
    last remaining unit. Admins bypass entirely.
    """
    if is_admin(user_id):
        return True

    limit = DAILY_LIMITS[kind]
    ref = _usage_ref(user_id)
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
            {"date": today, "user_id": int(user_id), kind: used + 1},
            merge=True,
        )
        return True

    return run(db().transaction())


def refund_usage(user_id, kind):
    """Hand one unit back after work that never reached the user.

    Charging someone for a summary that failed to deliver -- usually because
    they never pressed Start in a private chat -- burns quota for nothing.
    """
    if is_admin(user_id):
        return

    ref = _usage_ref(user_id)

    @firestore.transactional
    def run(txn):
        snap = ref.get(transaction=txn)
        data = (snap.to_dict() or {}) if snap.exists else {}
        used = int(data.get(kind, 0))
        if used <= 0:
            return
        txn.set(ref, {kind: used - 1}, merge=True)

    run(db().transaction())


def get_usage(user_id):
    """{kind: (used, limit)} for the /limits command."""
    snap = _usage_ref(user_id).get()
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
