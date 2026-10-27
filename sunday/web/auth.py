"""Who is looking at the family page, and how we know.

Family members have no passwords. They log in the way they already hear from
Sunday: we send a one-time link to the phone number on their family record,
over the same channel their notes arrive on (WhatsApp, SMS, or the console in
development). Tapping it sets a signed, HttpOnly session cookie.

* Links are signed with the server secret (``SUNDAY_SECRET_KEY``), expire after
  ``SUNDAY_LOGIN_LINK_MINUTES`` and can be used once.
* The login form never reveals whether a number is known, and is rate limited
  per number and per client address.
* Authorization is by membership: a member sees only the parents whose family
  record carries their phone number. Every page and action re-checks this.

Signing uses ``itsdangerous`` (the library behind Flask sessions); nothing
cryptographic is hand-rolled here.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from sunday.logging_setup import get_logger
from sunday.models import FamilyMember, Parent
from sunday.repository import Repository

log = get_logger("web.auth")

SESSION_COOKIE = "sunday_session"
_LOGIN_SALT = "sunday.login-link"
_SESSION_SALT = "sunday.session"


def digits_only(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


class RateLimiter:
    """Small sliding-window limiter. One process, in memory: right for this server."""

    def __init__(self, limit: int, window_s: int) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


@dataclass
class Viewer:
    """A logged-in family member and the parents they may see."""

    phone: str  # digits only
    memberships: list[FamilyMember]
    parents: list[Parent]

    @property
    def name(self) -> str:
        return self.memberships[0].name if self.memberships else "Family"

    def membership_for(self, parent_id: str) -> FamilyMember | None:
        return next((m for m in self.memberships if m.parent_id == parent_id), None)

    def parent(self, parent_id: str | None) -> Parent | None:
        """The requested parent if this viewer may see them, else their first parent."""
        if parent_id:
            return next((p for p in self.parents if p.id == parent_id), None)
        return self.parents[0] if self.parents else None


class Auth:
    def __init__(
        self,
        repo: Repository,
        secret_key: str,
        *,
        login_link_minutes: int = 15,
        session_days: int = 30,
    ) -> None:
        self.repo = repo
        self._links = URLSafeTimedSerializer(secret_key, salt=_LOGIN_SALT)
        self._sessions = URLSafeTimedSerializer(secret_key, salt=_SESSION_SALT)
        self.login_link_s = login_link_minutes * 60
        self.session_s = session_days * 24 * 3600
        self.limit_phone = RateLimiter(limit=3, window_s=15 * 60)
        self.limit_addr = RateLimiter(limit=20, window_s=15 * 60)
        self._used: dict[str, float] = {}  # token -> expiry; makes links single-use

    # -- login links --------------------------------------------------------------

    def members_for_phone(self, phone: str) -> list[FamilyMember]:
        return self.repo.members_by_phone(phone)

    def make_login_token(self, phone: str) -> str:
        return self._links.dumps({"p": digits_only(phone)})

    def peek_login_token(self, token: str) -> bool:
        """Valid, unexpired and unused, without consuming it."""
        self._prune_used()
        if token in self._used:
            return False
        try:
            data = self._links.loads(token, max_age=self.login_link_s)
        except (SignatureExpired, BadSignature):
            return False
        return bool(self.members_for_phone(str(data.get("p", ""))))

    def redeem_login_token(self, token: str) -> str | None:
        """Phone digits if the token is valid, unexpired and unused; else None."""
        self._prune_used()
        if token in self._used:
            return None
        try:
            data = self._links.loads(token, max_age=self.login_link_s)
        except SignatureExpired:
            return None
        except BadSignature:
            return None
        phone = str(data.get("p", ""))
        if not phone or not self.members_for_phone(phone):
            return None
        self._used[token] = time.monotonic() + self.login_link_s
        return phone

    def _prune_used(self) -> None:
        now = time.monotonic()
        for t, exp in list(self._used.items()):
            if exp < now:
                del self._used[t]

    # -- sessions -------------------------------------------------------------------

    def make_session_cookie(self, phone: str) -> str:
        return self._sessions.dumps({"p": digits_only(phone)})

    def viewer_from_request(self, request: Request) -> Viewer | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            data = self._sessions.loads(raw, max_age=self.session_s)
        except (BadSignature, SignatureExpired):
            return None
        phone = str(data.get("p", ""))
        memberships = self.members_for_phone(phone)
        if not memberships:
            return None  # removed from the family since they logged in
        parents = []
        for m in memberships:
            p = self.repo.get_parent(m.parent_id)
            if p is not None and p.active:
                parents.append(p)
        if not parents:
            return None
        return Viewer(phone=phone, memberships=memberships, parents=parents)


def login_message(name: str, link: str, minutes: int) -> str:
    return (
        f"Sunday: hello {name}. Tap to open your family page:\n{link}\n"
        f"The link works for {minutes} minutes and once only. "
        "If you did not ask for this, ignore it."
    )
