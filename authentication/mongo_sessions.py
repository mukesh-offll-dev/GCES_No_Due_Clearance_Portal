"""
MongoDB-backed Django session engine.

Sessions live in the ``django_sessions`` collection of the same MongoDB
database as the rest of the app, so the project needs no SQL database at all.

Document shape::

    {"_id": <session_key>, "data": <signed/encoded session dict>, "expire_date": <UTC datetime>}

A TTL index on ``expire_date`` (see db_indexes.py) makes MongoDB delete expired
sessions automatically; ``load()`` also ignores expired documents, so expiry is
exact even before the TTL monitor runs.

Enable with ``SESSION_ENGINE = "authentication.mongo_sessions"``.
"""
import logging
from datetime import datetime, timezone

from django.contrib.sessions.backends.base import CreateError, SessionBase, UpdateError
from pymongo.errors import DuplicateKeyError, PyMongoError

from .mongo import db

logger = logging.getLogger("nodue.mongo")

sessions_col = db["django_sessions"]


def _utc(dt):
    """Mongo returns naive UTC datetimes; make them tz-aware for comparison."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class SessionStore(SessionBase):

    def load(self):
        try:
            doc = sessions_col.find_one({"_id": self.session_key})
        except PyMongoError as exc:
            logger.error("Session load failed: %s", exc)
            doc = None
        if doc and _utc(doc.get("expire_date")) > datetime.now(timezone.utc):
            return self.decode(doc.get("data", ""))
        # Missing, expired or unreadable → start a fresh, empty session.
        self._session_key = None
        return {}

    def exists(self, session_key):
        if not session_key:
            return False
        return sessions_col.count_documents({"_id": session_key}, limit=1) > 0

    def create(self):
        while True:
            self._session_key = self._get_new_session_key()
            try:
                self.save(must_create=True)
            except CreateError:
                continue  # key collision — try another
            self.modified = True
            return

    def save(self, must_create=False):
        if self.session_key is None:
            return self.create()
        doc = {
            "data": self.encode(self._get_session(no_load=must_create)),
            "expire_date": self.get_expiry_date(),
        }
        if must_create:
            try:
                sessions_col.insert_one({"_id": self._get_or_create_session_key(), **doc})
            except DuplicateKeyError:
                raise CreateError
        else:
            result = sessions_col.update_one({"_id": self.session_key}, {"$set": doc}, upsert=True)
            if result.matched_count == 0 and result.upserted_id is None:
                raise UpdateError

    def delete(self, session_key=None):
        if session_key is None:
            if self.session_key is None:
                return
            session_key = self.session_key
        sessions_col.delete_one({"_id": session_key})

    @classmethod
    def clear_expired(cls):
        sessions_col.delete_many({"expire_date": {"$lt": datetime.now(timezone.utc)}})
