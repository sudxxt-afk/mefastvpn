from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock
from typing import Iterable
from urllib.parse import urlparse

from app.config import Settings
from app.schemas.contracts import VpnLocation

PUBLISH_STATES = frozenset({"published", "hidden", "disabled"})
SOURCE_KINDS = frozenset({"marzban_import", "subscription_url", "raw_links"})
DEFAULT_PUSH_PROVIDER = "fcm"
OPEN_ORDER_STATUSES = frozenset({"pending_payment", "waiting_confirmation"})
PROFILE_UNSET = object()


class TelegramIdentityConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class CatalogEntryPayload:
    fingerprint: str
    identity_key: str
    location: VpnLocation
    config_variants: dict[str, str]
    share_uri: str | None = None


@dataclass(slots=True)
class StoredCatalogEntry:
    entry_id: str
    source_id: str
    source_kind: str
    source_title: str
    fingerprint: str
    identity_key: str
    publish_state: str
    location: VpnLocation
    config_variants: dict[str, str]
    share_uri: str | None
    display_override: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class StoredSource:
    source_id: str
    kind: str
    title: str
    source_text: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    last_synced_at: datetime | None
    last_error: str | None
    entry_count: int
    published_count: int
    hidden_count: int
    disabled_count: int


@dataclass(slots=True)
class UserRecord:
    user_id: str
    telegram_user_id: int | None
    telegram_username: str | None
    telegram_linked_at: datetime | None
    telegram_handle: str
    display_name: str
    email: str | None
    preferred_platform: str | None
    trial_activated_at: datetime | None
    legal_accepted_at: datetime | None
    role: str
    blocked: bool
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime


@dataclass(slots=True)
class DevicePushTarget:
    user_id: str
    device_id: str
    platform: str
    push_provider: str
    push_token: str


@dataclass(slots=True)
class DeviceRecord:
    user_id: str
    device_id: str
    platform: str
    label: str
    created_at: datetime
    last_seen_at: datetime


@dataclass(slots=True)
class SubscriptionPlanRecord:
    plan_id: str
    title: str
    subtitle: str
    price_rub: int
    duration_days: int
    sort_order: int
    is_recommended: bool
    is_hidden: bool


@dataclass(slots=True)
class UserSubscriptionRecord:
    user_id: str
    plan_id: str | None
    status: str
    started_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: str | None
    granted_by: str | None
    updated_at: datetime


@dataclass(slots=True)
class SubscriptionOrderRecord:
    order_id: str
    user_id: str
    plan_id: str
    amount_rub: int
    original_amount_rub: int | None
    discount_amount_rub: int | None
    promo_code: str | None
    duration_days: int
    status: str
    created_at: datetime
    paid_marked_at: datetime | None
    confirmed_at: datetime | None
    confirmed_by: str | None
    telegram_invoice_chat_id: int | None
    telegram_invoice_message_id: int | None
    telegram_invoice_sent_at: datetime | None
    payment_provider: str | None
    provider_payment_id: str | None
    payment_confirmation_url: str | None
    payment_idempotence_key: str | None
    provider_payment_status: str | None


@dataclass(slots=True)
class ActivationCodeRecord:
    activation_code_id: int
    code: str
    user_id: str
    purpose: str
    status: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(slots=True)
class PromoCodeRecord:
    code: str
    effect_type: str
    discount_percent: int | None
    duration_days: int | None
    max_redemptions: int | None
    redemption_count: int
    is_active: bool
    expires_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SubscriptionTokenDeviceRecord:
    user_id: str
    token: str
    fingerprint_hash: str
    ip_hash: str
    user_agent_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    request_count: int


@dataclass(slots=True)
class ReferralRecord:
    referral_id: int
    inviter_user_id: str
    invited_telegram_user_id: int
    invited_user_id: str
    referral_token: str
    created_at: datetime


@dataclass(slots=True)
class ReferralRewardRecord:
    reward_id: int
    inviter_user_id: str
    invited_user_id: str
    order_id: str | None
    trigger_type: str
    bonus_days: int
    granted_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None
    revoke_reason: str | None


@dataclass(slots=True)
class ReferralSummaryRecord:
    user_id: str
    invited_total: int
    paid_total: int
    reward_total: int
    active_reward_total: int
    bonus_days_active: int
    bonus_days_revoked: int
    pending_total: int


@dataclass(slots=True)
class AppReleaseRecord:
    release_id: str
    platform: str
    version_name: str
    version_code: int
    download_url: str
    sha256: str
    release_notes: str
    is_current: bool
    published_by: str
    published_at: datetime


class CatalogStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._path = Path(settings.storage_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()
        self.sync_subscription_plans()
        self.seed_default_promos()

    @property
    def path(self) -> Path:
        return self._path



    def touch_user_activity(
        self,
        *,
        user_id: str,
        telegram_handle: str,
        display_name: str,
        email: str | None,
        role: str = "user",
    ) -> UserRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT *
                FROM app_users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            created_at = self._parse_dt(existing["created_at"]) if existing else now
            blocked = bool(existing["blocked"]) if existing else False
            current_role = str(existing["role"]) if existing else role
            current_display_name = (
                str(existing["display_name"]) if existing is not None else display_name
            )
            current_email = (
                str(existing["email"]) if existing is not None and existing["email"] is not None else None
            )
            current_preferred_platform = (
                str(existing["preferred_platform"])
                if existing is not None and existing["preferred_platform"] is not None
                else None
            )
            current_trial_activated_at = (
                str(existing["trial_activated_at"])
                if existing is not None and existing["trial_activated_at"] is not None
                else None
            )
            current_legal_accepted_at = (
                str(existing["legal_accepted_at"])
                if existing is not None and existing["legal_accepted_at"] is not None
                else None
            )
            current_telegram_handle = (
                str(existing["telegram_handle"]) if existing is not None else telegram_handle
            )
            next_email = current_email or email

            connection.execute(
                """
                INSERT INTO app_users(
                    user_id,
                    telegram_user_id,
                    telegram_username,
                    telegram_linked_at,
                    telegram_handle,
                    display_name,
                    email,
                    preferred_platform,
                    trial_activated_at,
                    legal_accepted_at,
                    role,
                    blocked,
                    created_at,
                    updated_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = app_users.display_name,
                    email = coalesce(app_users.email, excluded.email),
                    preferred_platform = coalesce(app_users.preferred_platform, excluded.preferred_platform),
                    trial_activated_at = coalesce(app_users.trial_activated_at, excluded.trial_activated_at),
                    legal_accepted_at = coalesce(app_users.legal_accepted_at, excluded.legal_accepted_at),
                    role = app_users.role,
                    blocked = app_users.blocked,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    user_id,
                    existing["telegram_user_id"] if existing is not None else None,
                    str(existing["telegram_username"]) if existing is not None and existing["telegram_username"] is not None else None,
                    str(existing["telegram_linked_at"]) if existing is not None and existing["telegram_linked_at"] is not None else None,
                    current_telegram_handle if current_telegram_handle.strip() else telegram_handle,
                    current_display_name,
                    next_email,
                    current_preferred_platform,
                    current_trial_activated_at,
                    current_legal_accepted_at,
                    current_role,
                    int(blocked),
                    self._to_wire(created_at),
                    self._to_wire(now),
                    self._to_wire(now),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._row_to_user(row)

    def link_telegram_identity(
        self,
        *,
        user_id: str,
        telegram_user_id: int,
        telegram_username: str | None,
        telegram_handle: str,
    ) -> UserRecord:
        now = self._utcnow()
        normalized_handle = telegram_handle.strip()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(user_id)

            owner = connection.execute(
                """
                SELECT user_id
                FROM app_users
                WHERE telegram_user_id = ?
                    AND user_id != ?
                """,
                (telegram_user_id, user_id),
            ).fetchone()
            if owner is not None:
                raise TelegramIdentityConflictError(
                    f"Telegram account {telegram_user_id} is already linked to another user."
                )

            linked_at = (
                str(existing["telegram_linked_at"])
                if existing["telegram_linked_at"] is not None
                else self._to_wire(now)
            )
            next_username = telegram_username.strip() if telegram_username and telegram_username.strip() else None
            next_handle = normalized_handle or str(existing["telegram_handle"])
            connection.execute(
                """
                UPDATE app_users
                SET telegram_user_id = ?,
                    telegram_username = ?,
                    telegram_linked_at = ?,
                    telegram_handle = ?,
                    updated_at = ?,
                    last_seen_at = ?
                WHERE user_id = ?
                """,
                (
                    telegram_user_id,
                    next_username,
                    linked_at,
                    next_handle,
                    self._to_wire(now),
                    self._to_wire(now),
                    user_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._row_to_user(row)

    def upsert_user(
        self,
        *,
        user_id: str,
        telegram_user_id: int | None,
        telegram_username: str | None = None,
        telegram_handle: str,
        display_name: str,
        email: str | None,
        role: str = "user",
    ) -> UserRecord:
        user = self.touch_user_activity(
            user_id=user_id,
            telegram_handle=telegram_handle,
            display_name=display_name,
            email=email,
            role=role,
        )
        if telegram_user_id is None:
            return user
        return self.link_telegram_identity(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            telegram_handle=telegram_handle,
        )

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._row_to_user(row) if row is not None else None

    def _require_user_exists(self, user_id: str) -> None:
        if self.get_user(user_id) is None:
            raise KeyError(user_id)

    def get_user_by_telegram_id(self, telegram_user_id: int) -> UserRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return self._row_to_user(row) if row is not None else None

    def get_user_by_telegram_username(self, username: str) -> UserRecord | None:
        username_clean = username.lstrip("@").lower()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE lower(telegram_username) = ?",
                (username_clean,),
            ).fetchone()
        return self._row_to_user(row) if row is not None else None

    def set_user_role(self, user_id: str, role: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE app_users SET role = ?, updated_at = ? WHERE user_id = ?",
                (role, self._utcnow(), user_id),
            )
            connection.commit()

    def update_plan_price(self, plan_id: str, new_price_rub: int) -> SubscriptionPlanRecord:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE subscription_plans SET price_rub = ? WHERE plan_id = ?",
                (new_price_rub, plan_id)
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Plan not found: {plan_id}")
            
            row = connection.execute("SELECT * FROM subscription_plans WHERE plan_id = ?", (plan_id,)).fetchone()
            return self._row_to_plan(row)

    def update_user_profile(
        self,
        user_id: str,
        *,
        display_name: object = PROFILE_UNSET,
        email: object = PROFILE_UNSET,
    ) -> UserRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise KeyError(user_id)

            next_display_name = str(row["display_name"])
            if display_name is not PROFILE_UNSET:
                candidate = str(display_name).strip()
                if candidate:
                    next_display_name = candidate

            current_email = row["email"]
            next_email = str(current_email) if current_email is not None else None
            if email is not PROFILE_UNSET:
                if email is None:
                    next_email = None
                else:
                    candidate_email = str(email).strip()
                    next_email = candidate_email or None

            connection.execute(
                """
                UPDATE app_users
                SET display_name = ?, email = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    next_display_name,
                    next_email,
                    self._to_wire(now),
                    user_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(user_id)
        return self._row_to_user(updated)

    def mark_legal_accepted(self, user_id: str) -> UserRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise KeyError(user_id)

            accepted_at = (
                str(row["legal_accepted_at"])
                if row["legal_accepted_at"] is not None
                else self._to_wire(now)
            )
            connection.execute(
                """
                UPDATE app_users
                SET legal_accepted_at = ?, updated_at = ?, last_seen_at = ?
                WHERE user_id = ?
                """,
                (
                    accepted_at,
                    self._to_wire(now),
                    self._to_wire(now),
                    user_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(user_id)
        return self._row_to_user(updated)

    def set_user_preferred_platform(
        self,
        user_id: str,
        *,
        preferred_platform: str | None,
    ) -> UserRecord:
        now = self._utcnow()
        normalized_platform = preferred_platform.strip().lower() if preferred_platform is not None else None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise KeyError(user_id)

            connection.execute(
                """
                UPDATE app_users
                SET preferred_platform = ?, updated_at = ?, last_seen_at = ?
                WHERE user_id = ?
                """,
                (
                    normalized_platform,
                    self._to_wire(now),
                    self._to_wire(now),
                    user_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(user_id)
        return self._row_to_user(updated)

    def mark_trial_activated(self, user_id: str) -> UserRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise KeyError(user_id)

            activated_at = (
                str(row["trial_activated_at"])
                if row["trial_activated_at"] is not None
                else self._to_wire(now)
            )
            connection.execute(
                """
                UPDATE app_users
                SET trial_activated_at = ?, updated_at = ?, last_seen_at = ?
                WHERE user_id = ?
                """,
                (
                    activated_at,
                    self._to_wire(now),
                    self._to_wire(now),
                    user_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(user_id)
        return self._row_to_user(updated)

    def is_user_blocked(self, user_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT blocked FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return bool(row["blocked"]) if row is not None else False

    def search_users(self, query: str, *, limit: int = 10) -> list[UserRecord]:
        needle = f"%{query.strip().lower()}%"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM app_users
                WHERE lower(user_id) LIKE ?
                    OR lower(telegram_handle) LIKE ?
                    OR lower(display_name) LIKE ?
                    OR lower(coalesce(email, '')) LIKE ?
                    OR lower(coalesce(telegram_username, '')) LIKE ?
                ORDER BY blocked ASC, updated_at DESC
                LIMIT ?
                """,
                (needle, needle, needle, needle, needle, limit),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def list_admin_users(self) -> list[UserRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM app_users WHERE role = 'admin' AND telegram_user_id IS NOT NULL",
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def get_all_telegram_user_ids(self) -> list[int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT telegram_user_id FROM app_users WHERE telegram_user_id IS NOT NULL",
            ).fetchall()
        return [int(row["telegram_user_id"]) for row in rows]

    def get_paginated_users(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        query: str | None = None,
        role: str | None = None,
        blocked: bool | None = None,
        subscription_status: str | None = None,
        plan_id: str | None = None,
    ) -> tuple[list[UserRecord], int]:
        filters = []
        params = []
        if query:
            needle = f"%{query.strip().lower()}%"
            filters.append(
                "(lower(u.user_id) LIKE ? OR lower(u.telegram_handle) LIKE ? OR lower(u.display_name) LIKE ? OR lower(coalesce(u.email, '')) LIKE ? OR lower(coalesce(u.telegram_username, '')) LIKE ?)"
            )
            params.extend([needle] * 5)
        if role:
            filters.append("u.role = ?")
            params.append(role)
        if blocked is not None:
            filters.append("u.blocked = ?")
            params.append(int(blocked))
        if subscription_status:
            if subscription_status == "no_access":
                filters.append("coalesce(s.status, 'inactive') != 'active'")
            else:
                filters.append("coalesce(s.status, 'inactive') = ?")
                params.append(subscription_status)
        if plan_id:
            filters.append("s.plan_id = ?")
            params.append(plan_id)

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        with self._lock, self._connect() as connection:
            count_row = connection.execute(
                f"""
                SELECT count(*) as total
                FROM app_users u
                LEFT JOIN user_subscriptions s ON s.user_id = u.user_id
                {where_clause}
                """,
                tuple(params)
            ).fetchone()
            total = int(count_row["total"])

            page_params = [*params, limit, offset]
            rows = connection.execute(
                f"""
                SELECT u.*
                FROM app_users u
                LEFT JOIN user_subscriptions s ON s.user_id = u.user_id
                {where_clause}
                ORDER BY u.last_seen_at DESC, u.updated_at DESC
                LIMIT ? OFFSET ?
                """,
                tuple(page_params),
            ).fetchall()

        return [self._row_to_user(row) for row in rows], total

    def create_activation_code(
        self,
        *,
        user_id: str,
        code: str,
        purpose: str,
        expires_at: datetime,
    ) -> ActivationCodeRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            user = connection.execute(
                "SELECT user_id FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                raise KeyError(user_id)

            connection.execute(
                """
                UPDATE activation_codes
                SET status = 'expired'
                WHERE user_id = ?
                    AND status = 'active'
                """,
                (user_id,),
            )
            connection.execute(
                """
                INSERT INTO activation_codes(
                    code,
                    user_id,
                    purpose,
                    status,
                    issued_at,
                    expires_at,
                    consumed_at
                )
                VALUES (?, ?, ?, 'active', ?, ?, NULL)
                """,
                (
                    code,
                    user_id,
                    purpose,
                    self._to_wire(now),
                    self._to_wire(expires_at),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM activation_codes WHERE id = last_insert_rowid()",
            ).fetchone()
        if row is None:
            raise KeyError(code)
        return self._row_to_activation_code(row)

    def get_activation_code(self, code: str) -> ActivationCodeRecord | None:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM activation_codes
                WHERE code = ?
                ORDER BY issued_at DESC, id DESC
                LIMIT 1
                """,
                (code,),
            ).fetchone()
            if row is None:
                return None
            record = self._row_to_activation_code(row)
            if record.status == "active" and record.expires_at <= now:
                connection.execute(
                    """
                    UPDATE activation_codes
                    SET status = 'expired'
                    WHERE id = ?
                        AND status = 'active'
                    """,
                    (record.activation_code_id,),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM activation_codes WHERE id = ?",
                    (record.activation_code_id,),
                ).fetchone()
                if row is None:
                    return None
                record = self._row_to_activation_code(row)
        return record

    def consume_activation_code(self, activation_code_id: int) -> ActivationCodeRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE activation_codes
                SET status = 'used', consumed_at = ?
                WHERE id = ?
                    AND status = 'active'
                """,
                (
                    self._to_wire(now),
                    activation_code_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM activation_codes WHERE id = ?",
                (activation_code_id,),
            ).fetchone()
        if row is None:
            raise KeyError(activation_code_id)
        return self._row_to_activation_code(row)

    def get_admin_dashboard_stats(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            users_total = connection.execute("SELECT count(*) FROM app_users").fetchone()[0]
            devices_total = connection.execute("SELECT count(*) FROM devices").fetchone()[0]

            subs_active = connection.execute("SELECT count(*) FROM user_subscriptions WHERE status = 'active'").fetchone()[0]
            subs_expired = connection.execute("SELECT count(*) FROM user_subscriptions WHERE status IN ('expired', 'revoked')").fetchone()[0]

            orders_pending = connection.execute("SELECT count(*) FROM subscription_orders WHERE status = 'pending_payment'").fetchone()[0]
            orders_waiting = connection.execute("SELECT count(*) FROM subscription_orders WHERE status = 'waiting_confirmation'").fetchone()[0]

            revenue_row = connection.execute("SELECT sum(amount_rub) FROM subscription_orders WHERE status = 'confirmed'").fetchone()
            revenue_confirmed = int(revenue_row[0]) if revenue_row[0] is not None else 0

        return {
            "users_total": users_total,
            "devices_total": devices_total,
            "active_subscriptions": subs_active,
            "expired_or_revoked_subscriptions": subs_expired,
            "pending_payments": orders_pending,
            "awaiting_confirmation": orders_waiting,
            "confirmed_revenue_rub": revenue_confirmed,
        }

    def set_user_blocked(self, user_id: str, blocked: bool) -> UserRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise KeyError(user_id)
            connection.execute(
                """
                UPDATE app_users
                SET blocked = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (int(blocked), self._to_wire(now), user_id),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._row_to_user(updated)

    def delete_user(self, user_id: str) -> UserRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise KeyError(user_id)
            deleted = self._row_to_user(row)
            connection.execute(
                """
                DELETE FROM app_users
                WHERE user_id = ?
                """,
                (user_id,),
            )
            connection.commit()
        return deleted

    def list_devices(self, user_id: str) -> list[DeviceRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, device_id, platform, label, created_at, last_seen_at
                FROM devices
                WHERE user_id = ?
                ORDER BY last_seen_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            DeviceRecord(
                user_id=str(row["user_id"]),
                device_id=str(row["device_id"]),
                platform=str(row["platform"]),
                label=str(row["label"]),
                created_at=self._parse_dt(row["created_at"]),
                last_seen_at=self._parse_dt(row["last_seen_at"]),
            )
            for row in rows
        ]

    def get_device(self, *, user_id: str, device_id: str) -> DeviceRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, device_id, platform, label, created_at, last_seen_at
                FROM devices
                WHERE user_id = ?
                    AND device_id = ?
                LIMIT 1
                """,
                (user_id, device_id),
            ).fetchone()
        if row is None:
            return None
        return DeviceRecord(
            user_id=str(row["user_id"]),
            device_id=str(row["device_id"]),
            platform=str(row["platform"]),
            label=str(row["label"]),
            created_at=self._parse_dt(row["created_at"]),
            last_seen_at=self._parse_dt(row["last_seen_at"]),
        )

    def record_device(
        self,
        *,
        user_id: str,
        device_id: str,
        platform: str,
        label: str,
        push_token: str | None = None,
        push_provider: str = DEFAULT_PUSH_PROVIDER,
    ) -> None:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO devices(
                    user_id,
                    device_id,
                    platform,
                    label,
                    push_provider,
                    push_token,
                    created_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, device_id) DO UPDATE SET
                    platform = excluded.platform,
                    label = excluded.label,
                    push_provider = CASE
                        WHEN excluded.push_token IS NOT NULL AND excluded.push_token != ''
                        THEN excluded.push_provider
                        ELSE devices.push_provider
                    END,
                    push_token = CASE
                        WHEN excluded.push_token IS NOT NULL AND excluded.push_token != ''
                        THEN excluded.push_token
                        ELSE devices.push_token
                    END,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    user_id,
                    device_id,
                    platform,
                    label,
                    push_provider,
                    push_token,
                    self._to_wire(now),
                    self._to_wire(now),
                ),
            )
            connection.execute(
                """
                UPDATE app_users
                SET last_seen_at = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (self._to_wire(now), self._to_wire(now), user_id),
            )
            connection.commit()

    def register_push_token(
        self,
        *,
        user_id: str,
        device_id: str,
        platform: str,
        label: str,
        push_token: str,
        push_provider: str = DEFAULT_PUSH_PROVIDER,
    ) -> None:
        self.record_device(
            user_id=user_id,
            device_id=device_id,
            platform=platform,
            label=label,
            push_token=push_token,
            push_provider=push_provider,
        )

    def list_push_targets(self) -> list[DevicePushTarget]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.user_id, d.device_id, d.platform, d.push_provider, d.push_token
                FROM devices d
                INNER JOIN app_users u ON u.user_id = d.user_id
                WHERE u.blocked = 0
                    AND d.push_token IS NOT NULL
                    AND trim(d.push_token) != ''
                ORDER BY d.last_seen_at DESC
                """
            ).fetchall()
        return [
            DevicePushTarget(
                user_id=str(row["user_id"]),
                device_id=str(row["device_id"]),
                platform=str(row["platform"]),
                push_provider=str(row["push_provider"] or DEFAULT_PUSH_PROVIDER),
                push_token=str(row["push_token"]),
            )
            for row in rows
        ]

    def sync_subscription_plans(self) -> None:
        plans = (
            ("trial_3d", "Пробный период", "Бесплатный доступ на 3 дня", 0, 3, 0, 0, 1),
            ("monthly_1", "1 мес", "Базовый тариф на 30 дней", self._settings.plan_monthly_price_rub, 30, 1, 0, 0),
            ("quarterly_3", "3 мес", "Выгодно для регулярного использования", self._settings.plan_quarterly_price_rub, 90, 2, 1, 0),
            ("semiannual_6", "6 мес", "Оптимально для долгого использования", self._settings.plan_semiannual_price_rub, 180, 3, 0, 0),
            ("yearly_12", "12 мес", "Максимальная выгода на год", self._settings.plan_yearly_price_rub, 365, 4, 0, 0),
        )
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            for plan_id, title, subtitle, price_rub, duration_days, sort_order, is_recommended, is_hidden in plans:
                connection.execute(
                    """
                    INSERT INTO subscription_plans(
                        plan_id,
                        title,
                        subtitle,
                        price_rub,
                        duration_days,
                        sort_order,
                        is_recommended,
                        is_hidden,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id) DO UPDATE SET
                        title = excluded.title,
                        subtitle = excluded.subtitle,
                        price_rub = excluded.price_rub,
                        duration_days = excluded.duration_days,
                        sort_order = excluded.sort_order,
                        is_recommended = excluded.is_recommended,
                        is_hidden = excluded.is_hidden,
                        updated_at = excluded.updated_at
                    """,
                    (
                        plan_id,
                        title,
                        subtitle,
                        price_rub,
                        duration_days,
                        sort_order,
                        is_recommended,
                        is_hidden,
                        self._to_wire(now),
                    ),
                )
            connection.commit()

    def seed_default_promos(self) -> None:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO promo_codes(
                    code,
                    effect_type,
                    discount_percent,
                    duration_days,
                    max_redemptions,
                    is_active,
                    expires_at,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, 'free_subscription', NULL, 365, NULL, 1, NULL, ?, ?, ?)
                ON CONFLICT(code) DO NOTHING
                """,
                (
                    "POLKADOT",
                    "system_seed",
                    self._to_wire(now),
                    self._to_wire(now),
                ),
            )
            connection.commit()

    def create_promo_code(
        self,
        *,
        code: str,
        effect_type: str,
        discount_percent: int | None,
        duration_days: int | None,
        max_redemptions: int | None,
        expires_at: datetime | None,
        created_by: str,
    ) -> PromoCodeRecord:
        normalized_code = code.strip().upper()
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO promo_codes(
                    code,
                    effect_type,
                    discount_percent,
                    duration_days,
                    max_redemptions,
                    is_active,
                    expires_at,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    normalized_code,
                    effect_type,
                    discount_percent,
                    duration_days,
                    max_redemptions,
                    self._to_wire(expires_at) if expires_at is not None else None,
                    created_by,
                    self._to_wire(now),
                    self._to_wire(now),
                ),
            )
            connection.commit()
        return self.get_promo_code(normalized_code)

    def list_promo_codes(self) -> list[PromoCodeRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.*,
                    count(r.id) AS redemption_count
                FROM promo_codes p
                LEFT JOIN promo_redemptions r ON r.code = p.code
                GROUP BY p.code
                ORDER BY p.created_at DESC, p.code ASC
                """
            ).fetchall()
        return [self._row_to_promo_code(row) for row in rows]

    def get_promo_code(self, code: str) -> PromoCodeRecord:
        normalized_code = code.strip().upper()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    p.*,
                    count(r.id) AS redemption_count
                FROM promo_codes p
                LEFT JOIN promo_redemptions r ON r.code = p.code
                WHERE p.code = ?
                GROUP BY p.code
                """,
                (normalized_code,),
            ).fetchone()
        if row is None:
            raise KeyError(normalized_code)
        return self._row_to_promo_code(row)

    def set_promo_code_active(self, code: str, *, is_active: bool) -> PromoCodeRecord:
        normalized_code = code.strip().upper()
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE promo_codes
                SET is_active = ?,
                    updated_at = ?
                WHERE code = ?
                """,
                (
                    int(is_active),
                    self._to_wire(now),
                    normalized_code,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(normalized_code)
            connection.commit()
        return self.get_promo_code(normalized_code)

    def get_or_create_subscription_token(self, user_id: str) -> str:
        self._require_user_exists(user_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT token
                FROM subscription_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is not None:
                return str(row["token"])

            now = self._utcnow()
            for _ in range(10):
                token = token_urlsafe(24)
                try:
                    connection.execute(
                        """
                        INSERT INTO subscription_tokens(
                            token,
                            user_id,
                            created_at,
                            rotated_at,
                            last_used_at
                        )
                        VALUES (?, ?, ?, ?, NULL)
                        """,
                        (
                            token,
                            user_id,
                            self._to_wire(now),
                            self._to_wire(now),
                        ),
                    )
                    connection.commit()
                    return token
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("Could not create subscription token.")

    def rotate_subscription_token(self, user_id: str) -> str:
        self._require_user_exists(user_id)
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            for _ in range(10):
                token = token_urlsafe(24)
                try:
                    connection.execute(
                        """
                        INSERT INTO subscription_tokens(
                            token,
                            user_id,
                            created_at,
                            rotated_at,
                            last_used_at
                        )
                        VALUES (?, ?, ?, ?, NULL)
                        ON CONFLICT(user_id) DO UPDATE SET
                            token = excluded.token,
                            rotated_at = excluded.rotated_at,
                            last_used_at = NULL
                        """,
                        (
                            token,
                            user_id,
                            self._to_wire(now),
                            self._to_wire(now),
                        ),
                    )
                    connection.commit()
                    return token
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("Could not rotate subscription token.")

    def get_user_by_subscription_token(self, token: str) -> UserRecord | None:
        normalized = token.strip()
        if not normalized:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.*
                FROM subscription_tokens t
                INNER JOIN app_users u ON u.user_id = t.user_id
                WHERE t.token = ?
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return self._row_to_user(row) if row is not None else None

    def touch_subscription_token(self, token: str) -> None:
        normalized = token.strip()
        if not normalized:
            return
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE subscription_tokens
                SET last_used_at = ?
                WHERE token = ?
                """,
                (self._to_wire(now), normalized),
            )
            connection.commit()

    def record_subscription_token_device(
        self,
        *,
        user_id: str,
        token: str,
        fingerprint_hash: str,
        ip_hash: str,
        user_agent_hash: str,
        limit: int,
    ) -> bool:
        if limit <= 0:
            return True
        self._require_user_exists(user_id)
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT fingerprint_hash
                FROM subscription_token_devices
                WHERE user_id = ?
                    AND fingerprint_hash = ?
                LIMIT 1
                """,
                (user_id, fingerprint_hash),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE subscription_token_devices
                    SET token = ?,
                        last_seen_at = ?,
                        request_count = request_count + 1
                    WHERE user_id = ?
                        AND fingerprint_hash = ?
                    """,
                    (
                        token,
                        self._to_wire(now),
                        user_id,
                        fingerprint_hash,
                    ),
                )
                connection.commit()
                return True

            row = connection.execute(
                """
                SELECT count(*) AS total
                FROM subscription_token_devices
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if int(row["total"] or 0) >= limit:
                return False

            connection.execute(
                """
                INSERT INTO subscription_token_devices(
                    user_id,
                    token,
                    fingerprint_hash,
                    ip_hash,
                    user_agent_hash,
                    first_seen_at,
                    last_seen_at,
                    request_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    user_id,
                    token,
                    fingerprint_hash,
                    ip_hash,
                    user_agent_hash,
                    self._to_wire(now),
                    self._to_wire(now),
                ),
            )
            connection.commit()
        return True

    def reset_subscription_token_devices(self, user_id: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM subscription_token_devices
                WHERE user_id = ?
                """,
                (user_id,),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    def count_subscription_token_devices(self, user_id: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS total
                FROM subscription_token_devices
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return int(row["total"] or 0)

    def get_or_create_referral_token(self, user_id: str) -> str:
        self._require_user_exists(user_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT token
                FROM referral_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is not None:
                return str(row["token"])

            now = self._utcnow()
            for _ in range(10):
                token = token_urlsafe(12)
                try:
                    connection.execute(
                        """
                        INSERT INTO referral_tokens(token, user_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (token, user_id, self._to_wire(now)),
                    )
                    connection.commit()
                    return token
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("Could not create referral token.")

    def get_referral_owner(self, token: str) -> UserRecord | None:
        normalized = token.strip()
        if not normalized:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.*
                FROM referral_tokens r
                INNER JOIN app_users u ON u.user_id = r.user_id
                WHERE r.token = ?
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return self._row_to_user(row) if row is not None else None

    def record_referral_visit(
        self,
        *,
        referral_token: str,
        invited_telegram_user_id: int,
        invited_user_id: str,
    ) -> bool:
        owner = self.get_referral_owner(referral_token)
        if owner is None or owner.user_id == invited_user_id:
            return False
        if owner.telegram_user_id == invited_telegram_user_id:
            return False
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO referrals(
                        inviter_user_id,
                        invited_telegram_user_id,
                        invited_user_id,
                        referral_token,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        owner.user_id,
                        invited_telegram_user_id,
                        invited_user_id,
                        referral_token.strip(),
                        self._to_wire(now),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                return False
        return True

    def count_referrals(self, user_id: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT count(*) AS total
                FROM referrals
                WHERE inviter_user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return int(row["total"] or 0)

    def get_referral_by_invited_user(self, invited_user_id: str) -> ReferralRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM referrals
                WHERE invited_user_id = ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (invited_user_id,),
            ).fetchone()
        return self._row_to_referral(row) if row is not None else None

    def list_referrals_for_inviter(
        self,
        inviter_user_id: str,
        *,
        limit: int = 20,
    ) -> list[ReferralRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM referrals
                WHERE inviter_user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (inviter_user_id, limit),
            ).fetchall()
        return [self._row_to_referral(row) for row in rows]

    def create_referral_reward(
        self,
        *,
        inviter_user_id: str,
        invited_user_id: str,
        order_id: str | None,
        trigger_type: str,
        bonus_days: int,
    ) -> ReferralRewardRecord | None:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO referral_rewards(
                        inviter_user_id,
                        invited_user_id,
                        order_id,
                        trigger_type,
                        bonus_days,
                        granted_at,
                        revoked_at,
                        revoked_by,
                        revoke_reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        inviter_user_id,
                        invited_user_id,
                        order_id,
                        trigger_type,
                        bonus_days,
                        self._to_wire(now),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM referral_rewards
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_referral_reward(row) if row is not None else None

    def get_referral_reward(self, reward_id: int) -> ReferralRewardRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM referral_rewards
                WHERE id = ?
                """,
                (reward_id,),
            ).fetchone()
        if row is None:
            raise KeyError(reward_id)
        return self._row_to_referral_reward(row)

    def get_referral_reward_for_invited_user(
        self,
        invited_user_id: str,
    ) -> ReferralRewardRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM referral_rewards
                WHERE invited_user_id = ?
                LIMIT 1
                """,
                (invited_user_id,),
            ).fetchone()
        return self._row_to_referral_reward(row) if row is not None else None

    def list_referral_rewards(
        self,
        *,
        inviter_user_id: str | None = None,
        limit: int = 20,
    ) -> list[ReferralRewardRecord]:
        sql = "SELECT * FROM referral_rewards"
        params: list[object] = []
        if inviter_user_id is not None:
            sql += " WHERE inviter_user_id = ?"
            params.append(inviter_user_id)
        sql += " ORDER BY granted_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_referral_reward(row) for row in rows]

    def revoke_referral_reward(
        self,
        *,
        reward_id: int,
        revoked_by: str,
        reason: str,
    ) -> ReferralRewardRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE referral_rewards
                SET revoked_at = COALESCE(revoked_at, ?),
                    revoked_by = COALESCE(revoked_by, ?),
                    revoke_reason = COALESCE(revoke_reason, ?)
                WHERE id = ?
                """,
                (
                    self._to_wire(now),
                    revoked_by,
                    reason,
                    reward_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT *
                FROM referral_rewards
                WHERE id = ?
                """,
                (reward_id,),
            ).fetchone()
        if row is None:
            raise KeyError(reward_id)
        return self._row_to_referral_reward(row)

    def record_subscription_adjustment(
        self,
        *,
        user_id: str,
        delta_days: int,
        reason: str,
        actor: str,
        related_referral_reward_id: int | None = None,
    ) -> None:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subscription_adjustments(
                    user_id,
                    delta_days,
                    reason,
                    actor,
                    related_referral_reward_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    delta_days,
                    reason,
                    actor,
                    related_referral_reward_id,
                    self._to_wire(now),
                ),
            )
            connection.commit()

    def adjust_subscription_days(
        self,
        *,
        user_id: str,
        delta_days: int,
        reason: str,
        actor: str,
        related_referral_reward_id: int | None = None,
    ) -> UserSubscriptionRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM user_subscriptions
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO user_subscriptions(
                        user_id,
                        plan_id,
                        status,
                        started_at,
                        expires_at,
                        revoked_at,
                        revoked_reason,
                        granted_by,
                        updated_at
                    )
                    VALUES (?, NULL, 'revoked', NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        self._to_wire(now),
                        self._to_wire(now),
                        reason,
                        actor,
                        self._to_wire(now),
                    ),
                )
            else:
                current_expires_at = self._parse_dt(row["expires_at"])
                base_expires_at = current_expires_at or now
                new_expires_at = base_expires_at + timedelta(days=delta_days)
                if new_expires_at <= now:
                    connection.execute(
                        """
                        UPDATE user_subscriptions
                        SET status = 'revoked',
                            expires_at = ?,
                            revoked_at = ?,
                            revoked_reason = ?,
                            granted_by = ?,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                            self._to_wire(now),
                            self._to_wire(now),
                            reason,
                            actor,
                            self._to_wire(now),
                            user_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE user_subscriptions
                        SET status = 'active',
                            expires_at = ?,
                            revoked_at = NULL,
                            revoked_reason = NULL,
                            granted_by = ?,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                            self._to_wire(new_expires_at),
                            actor,
                            self._to_wire(now),
                            user_id,
                        ),
                    )
            connection.execute(
                """
                INSERT INTO subscription_adjustments(
                    user_id,
                    delta_days,
                    reason,
                    actor,
                    related_referral_reward_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    delta_days,
                    reason,
                    actor,
                    related_referral_reward_id,
                    self._to_wire(now),
                ),
            )
            connection.commit()
            updated = connection.execute(
                """
                SELECT *
                FROM user_subscriptions
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(user_id)
        return self._row_to_subscription(updated)

    def get_referral_summary(self, user_id: str) -> ReferralSummaryRecord:
        with self._lock, self._connect() as connection:
            invited_total = int(connection.execute(
                """
                SELECT count(*) AS total
                FROM referrals
                WHERE inviter_user_id = ?
                """,
                (user_id,),
            ).fetchone()["total"] or 0)
            paid_total = int(connection.execute(
                """
                SELECT count(DISTINCT r.invited_user_id) AS total
                FROM referrals r
                INNER JOIN subscription_orders o ON o.user_id = r.invited_user_id
                WHERE r.inviter_user_id = ?
                    AND o.status = 'confirmed'
                    AND o.amount_rub > 0
                """,
                (user_id,),
            ).fetchone()["total"] or 0)
            reward_row = connection.execute(
                """
                SELECT
                    count(*) AS reward_total,
                    sum(CASE WHEN revoked_at IS NULL THEN 1 ELSE 0 END) AS active_reward_total,
                    sum(CASE WHEN revoked_at IS NULL THEN bonus_days ELSE 0 END) AS bonus_days_active,
                    sum(CASE WHEN revoked_at IS NOT NULL THEN bonus_days ELSE 0 END) AS bonus_days_revoked
                FROM referral_rewards
                WHERE inviter_user_id = ?
                """,
                (user_id,),
            ).fetchone()
            pending_total = int(connection.execute(
                """
                SELECT count(*) AS total
                FROM referrals r
                LEFT JOIN referral_rewards rw
                    ON rw.inviter_user_id = r.inviter_user_id
                    AND rw.invited_user_id = r.invited_user_id
                WHERE r.inviter_user_id = ?
                    AND rw.id IS NULL
                """,
                (user_id,),
            ).fetchone()["total"] or 0)
        return ReferralSummaryRecord(
            user_id=user_id,
            invited_total=invited_total,
            paid_total=paid_total,
            reward_total=int(reward_row["reward_total"] or 0),
            active_reward_total=int(reward_row["active_reward_total"] or 0),
            bonus_days_active=int(reward_row["bonus_days_active"] or 0),
            bonus_days_revoked=int(reward_row["bonus_days_revoked"] or 0),
            pending_total=pending_total,
        )

    def list_referral_leaderboard(self, *, limit: int = 10) -> list[ReferralSummaryRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT inviter_user_id
                FROM referrals
                GROUP BY inviter_user_id
                ORDER BY count(*) DESC, max(created_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self.get_referral_summary(str(row["inviter_user_id"])) for row in rows]

    def get_promo_redemption(self, *, code: str, user_id: str) -> sqlite3.Row | None:
        normalized_code = code.strip().upper()
        with self._lock, self._connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM promo_redemptions
                WHERE code = ?
                    AND user_id = ?
                LIMIT 1
                """,
                (normalized_code, user_id),
            ).fetchone()

    def record_promo_redemption(
        self,
        *,
        code: str,
        user_id: str,
        order_id: str | None = None,
    ) -> None:
        normalized_code = code.strip().upper()
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO promo_redemptions(code, user_id, order_id, redeemed_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    normalized_code,
                    user_id,
                    order_id,
                    self._to_wire(now),
                ),
            )
            connection.commit()

    def publish_app_release(
        self,
        *,
        platform: str,
        version_name: str,
        version_code: int,
        download_url: str,
        sha256: str,
        release_notes: str,
        published_by: str,
    ) -> AppReleaseRecord:
        now = self._utcnow()
        release_id = hashlib.sha1(
            f"{platform}|{version_name}|{version_code}|{self._to_wire(now)}".encode("utf-8")
        ).hexdigest()[:16]
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE app_releases
                SET is_current = 0
                WHERE platform = ?
                """,
                (platform,),
            )
            connection.execute(
                """
                INSERT INTO app_releases(
                    release_id,
                    platform,
                    version_name,
                    version_code,
                    download_url,
                    sha256,
                    release_notes,
                    is_current,
                    published_by,
                    published_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    release_id,
                    platform,
                    version_name,
                    version_code,
                    download_url,
                    sha256,
                    release_notes,
                    published_by,
                    self._to_wire(now),
                ),
            )
            connection.commit()
        return self.get_current_app_release(platform)

    def get_current_app_release(self, platform: str) -> AppReleaseRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM app_releases
                WHERE platform = ?
                    AND is_current = 1
                ORDER BY version_code DESC, published_at DESC
                LIMIT 1
                """,
                (platform,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_app_release(row)

    def list_subscription_plans(self, *, include_hidden: bool = False) -> list[SubscriptionPlanRecord]:
        with self._lock, self._connect() as connection:
            if include_hidden:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM subscription_plans
                    ORDER BY sort_order ASC, duration_days ASC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM subscription_plans
                    WHERE is_hidden = 0
                    ORDER BY sort_order ASC, duration_days ASC
                    """
                ).fetchall()
        return [self._row_to_plan(row) for row in rows]

    def upsert_subscription_plan(
        self,
        *,
        plan_id: str,
        title: str,
        subtitle: str,
        price_rub: int,
        duration_days: int,
        sort_order: int = 999,
        is_recommended: bool = False,
        is_hidden: bool = True,
    ) -> SubscriptionPlanRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subscription_plans(
                    plan_id,
                    title,
                    subtitle,
                    price_rub,
                    duration_days,
                    sort_order,
                    is_recommended,
                    is_hidden,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    title = excluded.title,
                    subtitle = excluded.subtitle,
                    price_rub = excluded.price_rub,
                    duration_days = excluded.duration_days,
                    sort_order = excluded.sort_order,
                    is_recommended = excluded.is_recommended,
                    is_hidden = excluded.is_hidden,
                    updated_at = excluded.updated_at
                """,
                (
                    plan_id,
                    title,
                    subtitle,
                    price_rub,
                    duration_days,
                    sort_order,
                    int(is_recommended),
                    int(is_hidden),
                    self._to_wire(now),
                ),
            )
            connection.commit()
        return self.get_subscription_plan(plan_id)

    def get_subscription_plan(self, plan_id: str) -> SubscriptionPlanRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return self._row_to_plan(row)

    def get_user_subscription(self, user_id: str) -> UserSubscriptionRecord | None:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None

            expires_at = self._parse_dt(row["expires_at"])
            status = str(row["status"])
            if status == "active" and expires_at is not None and expires_at <= now:
                connection.execute(
                    """
                    UPDATE user_subscriptions
                    SET status = 'expired', updated_at = ?
                    WHERE user_id = ?
                    """,
                    (self._to_wire(now), user_id),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM user_subscriptions WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
        return self._row_to_subscription(row) if row is not None else None

    def activate_subscription(
        self,
        *,
        user_id: str,
        plan_id: str,
        duration_days: int,
        granted_by: str,
    ) -> UserSubscriptionRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM user_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            base_start = now
            if existing is not None:
                existing_status = str(existing["status"])
                existing_expires_at = self._parse_dt(existing["expires_at"])
                if existing_status == "active" and existing_expires_at is not None and existing_expires_at > now:
                    base_start = existing_expires_at
            expires_at = base_start + timedelta(days=max(duration_days, 1))
            connection.execute(
                """
                INSERT INTO user_subscriptions(
                    user_id,
                    plan_id,
                    status,
                    started_at,
                    expires_at,
                    revoked_at,
                    revoked_reason,
                    granted_by,
                    updated_at
                )
                VALUES (?, ?, 'active', ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    expires_at = excluded.expires_at,
                    revoked_at = excluded.revoked_at,
                    revoked_reason = excluded.revoked_reason,
                    granted_by = excluded.granted_by,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    plan_id,
                    self._to_wire(now),
                    self._to_wire(expires_at),
                    granted_by,
                    self._to_wire(now),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM user_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._row_to_subscription(row)

    def revoke_subscription(
        self,
        *,
        user_id: str,
        reason: str,
        granted_by: str,
    ) -> UserSubscriptionRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM user_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO user_subscriptions(
                        user_id,
                        plan_id,
                        status,
                        started_at,
                        expires_at,
                        revoked_at,
                        revoked_reason,
                        granted_by,
                        updated_at
                    )
                    VALUES (?, NULL, 'revoked', NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        self._to_wire(now),
                        self._to_wire(now),
                        reason,
                        granted_by,
                        self._to_wire(now),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE user_subscriptions
                    SET status = 'revoked',
                        expires_at = ?,
                        revoked_at = ?,
                        revoked_reason = ?,
                        granted_by = ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        self._to_wire(now),
                        self._to_wire(now),
                        reason,
                        granted_by,
                        self._to_wire(now),
                        user_id,
                    ),
                )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM user_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise KeyError(user_id)
        return self._row_to_subscription(row)

    def create_subscription_order(
        self,
        *,
        user_id: str,
        plan_id: str,
        amount_rub: int | None = None,
        original_amount_rub: int | None = None,
        discount_amount_rub: int | None = None,
        promo_code: str | None = None,
    ) -> SubscriptionOrderRecord:
        now = self._utcnow()
        plan = self.get_subscription_plan(plan_id)
        normalized_promo_code = promo_code.strip().upper() if promo_code is not None and promo_code.strip() else None
        effective_amount_rub = amount_rub if amount_rub is not None else plan.price_rub
        effective_original_amount_rub = (
            original_amount_rub if original_amount_rub is not None else plan.price_rub
        )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT *
                FROM subscription_orders
                WHERE user_id = ?
                    AND status IN ('pending_payment', 'waiting_confirmation')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if existing is not None:
                existing_order = self._row_to_order(existing)
                if existing_order.plan_id == plan_id and existing_order.promo_code == normalized_promo_code:
                    return existing_order
                raise ValueError(
                    "У вас уже есть незавершенный заказ. Отмените его перед созданием нового."
                )

            order_id = hashlib.sha1(
                f"{user_id}|{plan_id}|{normalized_promo_code or '-'}|{self._to_wire(now)}".encode("utf-8")
            ).hexdigest()[:12]
            connection.execute(
                """
                INSERT INTO subscription_orders(
                    order_id,
                    user_id,
                    plan_id,
                    amount_rub,
                    original_amount_rub,
                    discount_amount_rub,
                    promo_code,
                    duration_days,
                    status,
                    created_at,
                    payment_idempotence_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_payment', ?, ?)
                """,
                (
                    order_id,
                    user_id,
                    plan.plan_id,
                    effective_amount_rub,
                    effective_original_amount_rub,
                    discount_amount_rub,
                    normalized_promo_code,
                    plan.duration_days,
                    self._to_wire(now),
                    order_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return self._row_to_order(row)

    def get_subscription_order(self, order_id: str) -> SubscriptionOrderRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            raise KeyError(order_id)
        return self._row_to_order(row)

    def record_order_invoice_message(
        self,
        *,
        order_id: str,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> SubscriptionOrderRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            connection.execute(
                """
                UPDATE subscription_orders
                SET telegram_invoice_chat_id = ?,
                    telegram_invoice_message_id = ?,
                    telegram_invoice_sent_at = ?
                WHERE order_id = ?
                """,
                (
                    telegram_chat_id,
                    telegram_message_id,
                    self._to_wire(now),
                    order_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def clear_order_invoice_message(self, *, order_id: str) -> SubscriptionOrderRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            connection.execute(
                """
                UPDATE subscription_orders
                SET telegram_invoice_chat_id = NULL,
                    telegram_invoice_message_id = NULL,
                    telegram_invoice_sent_at = NULL
                WHERE order_id = ?
                """,
                (order_id,),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def record_order_provider_payment(
        self,
        *,
        order_id: str,
        payment_provider: str,
        provider_payment_id: str,
        payment_confirmation_url: str | None,
        provider_payment_status: str,
    ) -> SubscriptionOrderRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            connection.execute(
                """
                UPDATE subscription_orders
                SET payment_provider = ?,
                    provider_payment_id = ?,
                    payment_confirmation_url = ?,
                    provider_payment_status = ?
                WHERE order_id = ?
                """,
                (
                    payment_provider,
                    provider_payment_id,
                    payment_confirmation_url,
                    provider_payment_status,
                    order_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def update_order_provider_payment_status(
        self,
        *,
        order_id: str,
        provider_payment_status: str,
    ) -> SubscriptionOrderRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            connection.execute(
                """
                UPDATE subscription_orders
                SET provider_payment_status = ?
                WHERE order_id = ?
                """,
                (provider_payment_status, order_id),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def list_subscription_orders(
        self,
        *,
        user_id: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SubscriptionOrderRecord]:
        sql = "SELECT * FROM subscription_orders"
        clauses: list[str] = []
        params: list[object] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if statuses is not None:
            normalized = [status for status in statuses]
            if normalized:
                clauses.append(
                    "status IN (%s)" % ", ".join("?" for _ in normalized)
                )
                params.extend(normalized)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_order(row) for row in rows]

    def count_subscription_orders(
        self,
        *,
        user_id: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> int:
        sql = "SELECT count(*) AS total FROM subscription_orders"
        clauses: list[str] = []
        params: list[object] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if statuses is not None:
            normalized = [status for status in statuses]
            if normalized:
                clauses.append(
                    "status IN (%s)" % ", ".join("?" for _ in normalized)
                )
                params.extend(normalized)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock, self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["total"] or 0)

    def mark_order_paid(
        self,
        *,
        order_id: str,
        user_id: str,
    ) -> SubscriptionOrderRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            if str(row["user_id"]) != user_id:
                raise PermissionError("Order does not belong to this user.")
            if str(row["status"]) != "pending_payment":
                raise ValueError("Order cannot be marked as paid.")
            connection.execute(
                """
                UPDATE subscription_orders
                SET status = 'waiting_confirmation',
                    paid_marked_at = ?
                WHERE order_id = ?
                """,
                (self._to_wire(now), order_id),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def confirm_order(
        self,
        *,
        order_id: str,
        confirmed_by: str,
    ) -> SubscriptionOrderRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            if str(row["status"]) != "waiting_confirmation":
                raise ValueError("Order is not waiting confirmation.")
            connection.execute(
                """
                UPDATE subscription_orders
                SET status = 'confirmed',
                    confirmed_at = ?,
                    confirmed_by = ?
                WHERE order_id = ?
                """,
                (self._to_wire(now), confirmed_by, order_id),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def confirm_order_paid(
        self,
        *,
        order_id: str,
        confirmed_by: str,
    ) -> SubscriptionOrderRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)

            status = str(row["status"])
            if status == "confirmed":
                return self._row_to_order(row)
            if status not in OPEN_ORDER_STATUSES:
                raise ValueError("Order cannot be confirmed from payment event.")

            paid_marked_at = row["paid_marked_at"] or self._to_wire(now)
            connection.execute(
                """
                UPDATE subscription_orders
                SET status = 'confirmed',
                    paid_marked_at = ?,
                    confirmed_at = ?,
                    confirmed_by = ?
                WHERE order_id = ?
                """,
                (
                    paid_marked_at,
                    self._to_wire(now),
                    confirmed_by,
                    order_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def reject_order(
        self,
        *,
        order_id: str,
        confirmed_by: str,
    ) -> SubscriptionOrderRecord:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            if str(row["status"]) not in OPEN_ORDER_STATUSES:
                raise ValueError("Order cannot be rejected.")
            connection.execute(
                """
                UPDATE subscription_orders
                SET status = 'rejected',
                    confirmed_at = ?,
                    confirmed_by = ?
                WHERE order_id = ?
                """,
                (self._to_wire(now), confirmed_by, order_id),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def cancel_order(
        self,
        *,
        order_id: str,
        user_id: str,
    ) -> SubscriptionOrderRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            if str(row["user_id"]) != user_id:
                raise PermissionError("Order does not belong to this user.")
            if str(row["status"]) != "pending_payment":
                raise ValueError("Only pending orders can be cancelled.")
            connection.execute(
                """
                UPDATE subscription_orders
                SET status = 'canceled'
                WHERE order_id = ?
                """,
                (order_id,),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def cancel_order_by_provider(self, *, order_id: str, provider_payment_status: str) -> SubscriptionOrderRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            if str(row["status"]) == "confirmed":
                return self._row_to_order(row)
            if str(row["status"]) not in OPEN_ORDER_STATUSES:
                connection.execute(
                    """
                    UPDATE subscription_orders
                    SET provider_payment_status = ?
                    WHERE order_id = ?
                    """,
                    (provider_payment_status, order_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE subscription_orders
                    SET status = 'canceled',
                        provider_payment_status = ?
                    WHERE order_id = ?
                    """,
                    (provider_payment_status, order_id),
                )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM subscription_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(order_id)
        return self._row_to_order(updated)

    def create_source(
        self,
        *,
        kind: str,
        source_text: str,
        created_by: str,
        title: str | None = None,
    ) -> StoredSource:
        normalized_kind = kind.strip().lower()
        if normalized_kind not in SOURCE_KINDS - {"marzban_import"}:
            raise ValueError(f"Unsupported source kind '{kind}'.")
        body = source_text.strip()
        if not body:
            raise ValueError("Source content cannot be empty.")

        now = self._utcnow()
        source_id = hashlib.sha1(
            f"{normalized_kind}|{body}|{self._to_wire(now)}".encode("utf-8")
        ).hexdigest()[:10]
        resolved_title = title or self._default_title(normalized_kind, body)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO server_sources(
                    source_id,
                    kind,
                    title,
                    source_text,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    normalized_kind,
                    resolved_title,
                    body,
                    created_by,
                    self._to_wire(now),
                    self._to_wire(now),
                ),
            )
            connection.commit()
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> StoredSource:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                self._source_select_sql() + " WHERE s.source_id = ? GROUP BY s.source_id",
                (source_id,),
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return self._row_to_source(row)

    def replace_source_entries(
        self,
        *,
        source_id: str,
        entries: Iterable[CatalogEntryPayload],
        last_error: str | None = None,
    ) -> StoredSource:
        now = self._utcnow()
        payloads = list(entries)
        with self._lock, self._connect() as connection:
            source_row = connection.execute(
                "SELECT kind FROM server_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if source_row is None:
                raise KeyError(source_id)

            existing_states = {
                str(row["fingerprint"]): str(row["publish_state"])
                for row in connection.execute(
                    """
                    SELECT fingerprint, publish_state
                    FROM server_entries
                    WHERE source_id = ?
                    """,
                    (source_id,),
                ).fetchall()
            }
            connection.execute(
                "DELETE FROM server_entries WHERE source_id = ?",
                (source_id,),
            )
            for payload in payloads:
                publish_state = existing_states.get(payload.fingerprint, "published")
                location = payload.location.model_dump(mode="json")
                connection.execute(
                    """
                    INSERT INTO server_entries(
                        entry_id,
                        source_id,
                        fingerprint,
                        identity_key,
                        location_id,
                        publish_state,
                        protocol,
                        host,
                        port,
                        network,
                        security_mode,
                        location_json,
                        config_variants_json,
                        share_uri,
                        display_override,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        hashlib.sha1(
                            f"{source_id}|{payload.fingerprint}".encode("utf-8")
                        ).hexdigest()[:12],
                        source_id,
                        payload.fingerprint,
                        payload.identity_key,
                        payload.location.id,
                        publish_state,
                        payload.location.protocol,
                        payload.location.host,
                        payload.location.port,
                        payload.location.network,
                        payload.location.security_mode,
                        json.dumps(location, ensure_ascii=False),
                        json.dumps(payload.config_variants, ensure_ascii=False),
                        payload.share_uri,
                        self._to_wire(now),
                        self._to_wire(now),
                    ),
                )

            connection.execute(
                """
                UPDATE server_sources
                SET updated_at = ?, last_synced_at = ?, last_error = ?
                WHERE source_id = ?
                """,
                (
                    self._to_wire(now),
                    self._to_wire(now),
                    last_error,
                    source_id,
                ),
            )
            connection.commit()
        return self.get_source(source_id)

    def mark_source_sync_error(self, source_id: str, error: str) -> StoredSource:
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE server_sources
                SET updated_at = ?, last_error = ?
                WHERE source_id = ?
                """,
                (self._to_wire(now), error, source_id),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(source_id)
        return self.get_source(source_id)

    def update_source_title(self, source_id: str, title: str) -> StoredSource:
        normalized = " ".join(title.strip().split())
        if not normalized:
            raise ValueError("Source title cannot be empty.")
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE server_sources
                SET title = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (normalized[:120], self._to_wire(now), source_id),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(source_id)
        return self.get_source(source_id)

    def list_sources(self, *, limit: int = 20) -> list[StoredSource]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                self._source_select_sql()
                + """
                GROUP BY s.source_id
                ORDER BY coalesce(s.last_synced_at, s.updated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_source(row) for row in rows]

    def delete_source(self, source_id: str) -> StoredSource:
        deleted = self.get_source(source_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM server_sources
                WHERE source_id = ?
                """,
                (source_id,),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(source_id)
        return deleted

    def list_entries(
        self,
        *,
        include_hidden: bool = True,
        limit: int = 20,
        offset: int = 0,
        source_id: str | None = None,
    ) -> list[StoredCatalogEntry]:
        sql = self._entry_select_sql()
        params: list[object] = []
        filters: list[str] = []
        if not include_hidden:
            filters.append("e.publish_state = 'published'")
        if source_id is not None:
            filters.append("e.source_id = ?")
            params.append(source_id)
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY e.updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def list_published_entries(self) -> list[StoredCatalogEntry]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                self._entry_select_sql()
                + " WHERE e.publish_state = 'published' ORDER BY e.updated_at DESC"
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def get_entry(self, entry_id: str) -> StoredCatalogEntry:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                self._entry_select_sql() + " WHERE e.entry_id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise KeyError(entry_id)
        return self._row_to_entry(row)

    def delete_entry(self, entry_id: str) -> StoredCatalogEntry:
        deleted = self.get_entry(entry_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM server_entries
                WHERE entry_id = ?
                """,
                (entry_id,),
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(entry_id)
        return deleted

    def set_entry_publish_state(
        self,
        entry_id: str,
        state: str,
    ) -> StoredCatalogEntry:
        normalized = state.strip().lower()
        if normalized not in PUBLISH_STATES:
            raise ValueError(f"Unsupported publish state '{state}'.")
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE server_entries
                SET publish_state = ?, updated_at = ?
                WHERE entry_id = ?
                """,
                (normalized, self._to_wire(now), entry_id),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(entry_id)
            row = connection.execute(
                self._entry_select_sql() + " WHERE e.entry_id = ?",
                (entry_id,),
            ).fetchone()
        return self._row_to_entry(row)

    def update_entry_display_name(
        self,
        entry_id: str,
        display_name: str,
    ) -> StoredCatalogEntry:
        normalized = " ".join(display_name.strip().split())
        if not normalized:
            raise ValueError("Server display name cannot be empty.")
        now = self._utcnow()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                self._entry_select_sql() + " WHERE e.entry_id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise KeyError(entry_id)
            location_payload = json.loads(str(row["location_json"]))
            if not isinstance(location_payload, dict):
                raise ValueError("Server location payload is invalid.")
            location_payload["display_name"] = normalized[:120]
            cursor = connection.execute(
                """
                UPDATE server_entries
                SET display_override = ?,
                    location_json = ?,
                    updated_at = ?
                WHERE entry_id = ?
                """,
                (
                    normalized[:120],
                    json.dumps(location_payload, ensure_ascii=False),
                    self._to_wire(now),
                    entry_id,
                ),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(entry_id)
            updated = connection.execute(
                self._entry_select_sql() + " WHERE e.entry_id = ?",
                (entry_id,),
            ).fetchone()
        return self._row_to_entry(updated)

    def counts(self) -> dict[str, int | str | None]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM server_sources) AS sources,
                    (SELECT count(*) FROM server_entries WHERE publish_state = 'published') AS published_entries,
                    (SELECT count(*) FROM server_entries WHERE publish_state = 'hidden') AS hidden_entries,
                    (SELECT count(*) FROM server_entries WHERE publish_state = 'disabled') AS disabled_entries,
                    (SELECT max(last_synced_at) FROM server_sources) AS last_synced_at
                """
            ).fetchone()
        return {
            "sources": int(row["sources"] or 0),
            "published_entries": int(row["published_entries"] or 0),
            "hidden_entries": int(row["hidden_entries"] or 0),
            "disabled_entries": int(row["disabled_entries"] or 0),
            "last_synced_at": str(row["last_synced_at"]) if row["last_synced_at"] else None,
        }

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_ui_sessions (
                    chat_id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    photo_token TEXT,
                    file_id TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_users (
                    user_id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER,
                    telegram_username TEXT,
                    telegram_linked_at TEXT,
                    telegram_handle TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    email TEXT,
                    preferred_platform TEXT,
                    trial_activated_at TEXT,
                    legal_accepted_at TEXT,
                    role TEXT NOT NULL DEFAULT 'user',
                    blocked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_app_users_lookup
                ON app_users(telegram_user_id, telegram_handle, display_name);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_app_users_telegram_user_id_unique
                ON app_users(telegram_user_id)
                WHERE telegram_user_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    label TEXT NOT NULL,
                    push_provider TEXT,
                    push_token TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    UNIQUE(user_id, device_id)
                );

                CREATE TABLE IF NOT EXISTS subscription_plans (
                    plan_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    subtitle TEXT NOT NULL,
                    price_rub INTEGER NOT NULL,
                    duration_days INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL,
                    is_recommended INTEGER NOT NULL DEFAULT 0,
                    is_hidden INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_subscriptions (
                    user_id TEXT PRIMARY KEY,
                    plan_id TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    expires_at TEXT,
                    revoked_at TEXT,
                    revoked_reason TEXT,
                    granted_by TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(plan_id) REFERENCES subscription_plans(plan_id)
                );

                CREATE INDEX IF NOT EXISTS idx_user_subscriptions_status
                ON user_subscriptions(status, expires_at);

                CREATE TABLE IF NOT EXISTS subscription_orders (
                    order_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    original_amount_rub INTEGER,
                    discount_amount_rub INTEGER,
                    promo_code TEXT,
                    duration_days INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    paid_marked_at TEXT,
                    confirmed_at TEXT,
                    confirmed_by TEXT,
                    telegram_invoice_chat_id INTEGER,
                    telegram_invoice_message_id INTEGER,
                    telegram_invoice_sent_at TEXT,
                    payment_provider TEXT,
                    provider_payment_id TEXT,
                    payment_confirmation_url TEXT,
                    payment_idempotence_key TEXT,
                    provider_payment_status TEXT,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(plan_id) REFERENCES subscription_plans(plan_id)
                );

                CREATE INDEX IF NOT EXISTS idx_subscription_orders_lookup
                ON subscription_orders(user_id, status, created_at);

                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    effect_type TEXT NOT NULL,
                    discount_percent INTEGER,
                    duration_days INTEGER,
                    max_redemptions INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promo_redemptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    order_id TEXT,
                    redeemed_at TEXT NOT NULL,
                    FOREIGN KEY(code) REFERENCES promo_codes(code) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(order_id) REFERENCES subscription_orders(order_id) ON DELETE SET NULL,
                    UNIQUE(code, user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_promo_redemptions_lookup
                ON promo_redemptions(code, redeemed_at);

                CREATE TABLE IF NOT EXISTS subscription_tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    rotated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_subscription_tokens_user
                ON subscription_tokens(user_id);

                CREATE TABLE IF NOT EXISTS subscription_token_devices (
                    user_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    fingerprint_hash TEXT NOT NULL,
                    ip_hash TEXT NOT NULL,
                    user_agent_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(user_id, fingerprint_hash),
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_subscription_token_devices_user
                ON subscription_token_devices(user_id, last_seen_at);

                CREATE TABLE IF NOT EXISTS referral_tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inviter_user_id TEXT NOT NULL,
                    invited_telegram_user_id INTEGER NOT NULL,
                    invited_user_id TEXT NOT NULL,
                    referral_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(inviter_user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    UNIQUE(inviter_user_id, invited_telegram_user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_referrals_inviter
                ON referrals(inviter_user_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_referrals_invited
                ON referrals(invited_user_id);

                CREATE TABLE IF NOT EXISTS referral_rewards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inviter_user_id TEXT NOT NULL,
                    invited_user_id TEXT NOT NULL,
                    order_id TEXT,
                    trigger_type TEXT NOT NULL,
                    bonus_days INTEGER NOT NULL,
                    granted_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoked_by TEXT,
                    revoke_reason TEXT,
                    FOREIGN KEY(inviter_user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(invited_user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(order_id) REFERENCES subscription_orders(order_id) ON DELETE SET NULL,
                    UNIQUE(inviter_user_id, invited_user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_referral_rewards_inviter
                ON referral_rewards(inviter_user_id, granted_at);

                CREATE TABLE IF NOT EXISTS subscription_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    delta_days INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    related_referral_reward_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY(related_referral_reward_id) REFERENCES referral_rewards(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_subscription_adjustments_user
                ON subscription_adjustments(user_id, created_at);

                CREATE TABLE IF NOT EXISTS app_releases (
                    release_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    version_name TEXT NOT NULL,
                    version_code INTEGER NOT NULL,
                    download_url TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    release_notes TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_app_releases_platform_current
                ON app_releases(platform, is_current, version_code DESC);

                CREATE TABLE IF NOT EXISTS activation_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES app_users(user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_activation_codes_lookup
                ON activation_codes(code, status, expires_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_activation_codes_active_code_unique
                ON activation_codes(code)
                WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS server_sources (
                    source_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_synced_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS server_entries (
                    entry_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    identity_key TEXT NOT NULL,
                    location_id TEXT NOT NULL,
                    publish_state TEXT NOT NULL DEFAULT 'published',
                    protocol TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    network TEXT NOT NULL,
                    security_mode TEXT NOT NULL,
                    location_json TEXT NOT NULL,
                    config_variants_json TEXT NOT NULL,
                    share_uri TEXT,
                    display_override TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES server_sources(source_id) ON DELETE CASCADE,
                    UNIQUE(source_id, fingerprint)
                );

                CREATE INDEX IF NOT EXISTS idx_server_entries_publish_state
                ON server_entries(publish_state, identity_key);
                """
            )
            existing_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(app_users)").fetchall()
            }
            required_columns = {
                "telegram_username": "ALTER TABLE app_users ADD COLUMN telegram_username TEXT",
                "telegram_linked_at": "ALTER TABLE app_users ADD COLUMN telegram_linked_at TEXT",
                "preferred_platform": "ALTER TABLE app_users ADD COLUMN preferred_platform TEXT",
                "trial_activated_at": "ALTER TABLE app_users ADD COLUMN trial_activated_at TEXT",
                "legal_accepted_at": "ALTER TABLE app_users ADD COLUMN legal_accepted_at TEXT",
                "role": "ALTER TABLE app_users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
            }
            for column_name, sql in required_columns.items():
                if column_name not in existing_columns:
                    connection.execute(sql)
            existing_plan_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(subscription_plans)").fetchall()
            }
            required_plan_columns = {
                "is_hidden": "ALTER TABLE subscription_plans ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0",
            }
            for column_name, sql in required_plan_columns.items():
                if column_name not in existing_plan_columns:
                    connection.execute(sql)
            existing_order_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(subscription_orders)").fetchall()
            }
            required_order_columns = {
                "telegram_invoice_chat_id": (
                    "ALTER TABLE subscription_orders ADD COLUMN telegram_invoice_chat_id INTEGER"
                ),
                "telegram_invoice_message_id": (
                    "ALTER TABLE subscription_orders ADD COLUMN telegram_invoice_message_id INTEGER"
                ),
                "telegram_invoice_sent_at": (
                    "ALTER TABLE subscription_orders ADD COLUMN telegram_invoice_sent_at TEXT"
                ),
                "original_amount_rub": (
                    "ALTER TABLE subscription_orders ADD COLUMN original_amount_rub INTEGER"
                ),
                "discount_amount_rub": (
                    "ALTER TABLE subscription_orders ADD COLUMN discount_amount_rub INTEGER"
                ),
                "promo_code": (
                    "ALTER TABLE subscription_orders ADD COLUMN promo_code TEXT"
                ),
                "payment_provider": (
                    "ALTER TABLE subscription_orders ADD COLUMN payment_provider TEXT"
                ),
                "provider_payment_id": (
                    "ALTER TABLE subscription_orders ADD COLUMN provider_payment_id TEXT"
                ),
                "payment_confirmation_url": (
                    "ALTER TABLE subscription_orders ADD COLUMN payment_confirmation_url TEXT"
                ),
                "payment_idempotence_key": (
                    "ALTER TABLE subscription_orders ADD COLUMN payment_idempotence_key TEXT"
                ),
                "provider_payment_status": (
                    "ALTER TABLE subscription_orders ADD COLUMN provider_payment_status TEXT"
                ),
            }
            for column_name, sql in required_order_columns.items():
                if column_name not in existing_order_columns:
                    connection.execute(sql)
            existing_entry_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(server_entries)").fetchall()
            }
            required_entry_columns = {
                "share_uri": "ALTER TABLE server_entries ADD COLUMN share_uri TEXT",
                "display_override": "ALTER TABLE server_entries ADD COLUMN display_override TEXT",
            }
            for column_name, sql in required_entry_columns.items():
                if column_name not in existing_entry_columns:
                    connection.execute(sql)
            connection.commit()

    def get_bot_ui_session(
        self,
        chat_id: int,
    ) -> tuple[int, str, str | None, str | None] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT message_id, kind, photo_token, file_id
                FROM bot_ui_sessions
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            int(row["message_id"]),
            str(row["kind"]),
            row["photo_token"],
            row["file_id"],
        )

    def set_bot_ui_session(
        self,
        *,
        chat_id: int,
        message_id: int,
        kind: str,
        photo_token: str | None = None,
        file_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_ui_sessions(
                    chat_id, message_id, kind, photo_token, file_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    kind = excluded.kind,
                    photo_token = excluded.photo_token,
                    file_id = excluded.file_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, message_id, kind, photo_token, file_id, now),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _source_select_sql() -> str:
        return """
            SELECT
                s.*,
                count(e.entry_id) AS entry_count,
                sum(CASE WHEN e.publish_state = 'published' THEN 1 ELSE 0 END) AS published_count,
                sum(CASE WHEN e.publish_state = 'hidden' THEN 1 ELSE 0 END) AS hidden_count,
                sum(CASE WHEN e.publish_state = 'disabled' THEN 1 ELSE 0 END) AS disabled_count
            FROM server_sources s
            LEFT JOIN server_entries e ON e.source_id = s.source_id
        """

    @staticmethod
    def _entry_select_sql() -> str:
        return """
            SELECT
                e.*,
                s.kind AS source_kind,
                s.title AS source_title
            FROM server_entries e
            INNER JOIN server_sources s ON s.source_id = e.source_id
        """

    @classmethod
    def _row_to_source(cls, row: sqlite3.Row) -> StoredSource:
        return StoredSource(
            source_id=str(row["source_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            source_text=str(row["source_text"]),
            created_by=str(row["created_by"]),
            created_at=cls._parse_dt(row["created_at"]),
            updated_at=cls._parse_dt(row["updated_at"]),
            last_synced_at=cls._parse_dt(row["last_synced_at"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            entry_count=int(row["entry_count"] or 0),
            published_count=int(row["published_count"] or 0),
            hidden_count=int(row["hidden_count"] or 0),
            disabled_count=int(row["disabled_count"] or 0),
        )

    @classmethod
    def _row_to_entry(cls, row: sqlite3.Row) -> StoredCatalogEntry:
        row_keys = set(row.keys())
        return StoredCatalogEntry(
            entry_id=str(row["entry_id"]),
            source_id=str(row["source_id"]),
            source_kind=str(row["source_kind"]),
            source_title=str(row["source_title"]),
            fingerprint=str(row["fingerprint"]),
            identity_key=str(row["identity_key"]),
            publish_state=str(row["publish_state"]),
            location=VpnLocation.model_validate(json.loads(str(row["location_json"]))),
            config_variants={
                str(key): str(value)
                for key, value in json.loads(str(row["config_variants_json"])).items()
            },
            share_uri=(
                str(row["share_uri"])
                if "share_uri" in row_keys and row["share_uri"] is not None
                else None
            ),
            display_override=(
                str(row["display_override"])
                if "display_override" in row_keys and row["display_override"] is not None
                else None
            ),
            created_at=cls._parse_dt(row["created_at"]),
            updated_at=cls._parse_dt(row["updated_at"]),
        )

    @classmethod
    def _row_to_user(cls, row: sqlite3.Row) -> UserRecord:
        telegram_user_id = row["telegram_user_id"]
        return UserRecord(
            user_id=str(row["user_id"]),
            telegram_user_id=int(telegram_user_id) if telegram_user_id is not None else None,
            telegram_username=str(row["telegram_username"]) if row["telegram_username"] is not None else None,
            telegram_linked_at=cls._parse_dt(row["telegram_linked_at"]),
            telegram_handle=str(row["telegram_handle"]),
            display_name=str(row["display_name"]),
            email=str(row["email"]) if row["email"] is not None else None,
            preferred_platform=str(row["preferred_platform"]) if row["preferred_platform"] is not None else None,
            trial_activated_at=cls._parse_dt(row["trial_activated_at"]),
            legal_accepted_at=cls._parse_dt(row["legal_accepted_at"]),
            role=str(row["role"]),
            blocked=bool(row["blocked"]),
            created_at=cls._parse_dt(row["created_at"]),
            updated_at=cls._parse_dt(row["updated_at"]),
            last_seen_at=cls._parse_dt(row["last_seen_at"]),
        )

    @classmethod
    def _row_to_plan(cls, row: sqlite3.Row) -> SubscriptionPlanRecord:
        row_keys = set(row.keys())
        return SubscriptionPlanRecord(
            plan_id=str(row["plan_id"]),
            title=str(row["title"]),
            subtitle=str(row["subtitle"]),
            price_rub=int(row["price_rub"]),
            duration_days=int(row["duration_days"]),
            sort_order=int(row["sort_order"]),
            is_recommended=bool(row["is_recommended"]),
            is_hidden=bool(row["is_hidden"]) if "is_hidden" in row_keys else False,
        )

    @classmethod
    def _row_to_subscription(cls, row: sqlite3.Row) -> UserSubscriptionRecord:
        return UserSubscriptionRecord(
            user_id=str(row["user_id"]),
            plan_id=str(row["plan_id"]) if row["plan_id"] is not None else None,
            status=str(row["status"]),
            started_at=cls._parse_dt(row["started_at"]),
            expires_at=cls._parse_dt(row["expires_at"]),
            revoked_at=cls._parse_dt(row["revoked_at"]),
            revoked_reason=str(row["revoked_reason"]) if row["revoked_reason"] is not None else None,
            granted_by=str(row["granted_by"]) if row["granted_by"] is not None else None,
            updated_at=cls._parse_dt(row["updated_at"]),
        )

    @classmethod
    def _row_to_order(cls, row: sqlite3.Row) -> SubscriptionOrderRecord:
        row_keys = set(row.keys())
        telegram_invoice_chat_id = (
            int(row["telegram_invoice_chat_id"])
            if "telegram_invoice_chat_id" in row_keys and row["telegram_invoice_chat_id"] is not None
            else None
        )
        telegram_invoice_message_id = (
            int(row["telegram_invoice_message_id"])
            if "telegram_invoice_message_id" in row_keys and row["telegram_invoice_message_id"] is not None
            else None
        )
        telegram_invoice_sent_at = (
            cls._parse_dt(row["telegram_invoice_sent_at"])
            if "telegram_invoice_sent_at" in row_keys
            else None
        )
        return SubscriptionOrderRecord(
            order_id=str(row["order_id"]),
            user_id=str(row["user_id"]),
            plan_id=str(row["plan_id"]),
            amount_rub=int(row["amount_rub"]),
            original_amount_rub=(
                int(row["original_amount_rub"])
                if "original_amount_rub" in row_keys and row["original_amount_rub"] is not None
                else None
            ),
            discount_amount_rub=(
                int(row["discount_amount_rub"])
                if "discount_amount_rub" in row_keys and row["discount_amount_rub"] is not None
                else None
            ),
            promo_code=(
                str(row["promo_code"])
                if "promo_code" in row_keys and row["promo_code"] is not None
                else None
            ),
            duration_days=int(row["duration_days"]),
            status=str(row["status"]),
            created_at=cls._parse_dt(row["created_at"]),
            paid_marked_at=cls._parse_dt(row["paid_marked_at"]),
            confirmed_at=cls._parse_dt(row["confirmed_at"]),
            confirmed_by=str(row["confirmed_by"]) if row["confirmed_by"] is not None else None,
            telegram_invoice_chat_id=telegram_invoice_chat_id,
            telegram_invoice_message_id=telegram_invoice_message_id,
            telegram_invoice_sent_at=telegram_invoice_sent_at,
            payment_provider=(
                str(row["payment_provider"])
                if "payment_provider" in row_keys and row["payment_provider"] is not None
                else None
            ),
            provider_payment_id=(
                str(row["provider_payment_id"])
                if "provider_payment_id" in row_keys and row["provider_payment_id"] is not None
                else None
            ),
            payment_confirmation_url=(
                str(row["payment_confirmation_url"])
                if "payment_confirmation_url" in row_keys and row["payment_confirmation_url"] is not None
                else None
            ),
            payment_idempotence_key=(
                str(row["payment_idempotence_key"])
                if "payment_idempotence_key" in row_keys and row["payment_idempotence_key"] is not None
                else None
            ),
            provider_payment_status=(
                str(row["provider_payment_status"])
                if "provider_payment_status" in row_keys and row["provider_payment_status"] is not None
                else None
            ),
        )

    @classmethod
    def _row_to_activation_code(cls, row: sqlite3.Row) -> ActivationCodeRecord:
        return ActivationCodeRecord(
            activation_code_id=int(row["id"]),
            code=str(row["code"]),
            user_id=str(row["user_id"]),
            purpose=str(row["purpose"]),
            status=str(row["status"]),
            issued_at=cls._parse_dt(row["issued_at"]),
            expires_at=cls._parse_dt(row["expires_at"]),
            consumed_at=cls._parse_dt(row["consumed_at"]),
        )

    @classmethod
    def _row_to_promo_code(cls, row: sqlite3.Row) -> PromoCodeRecord:
        return PromoCodeRecord(
            code=str(row["code"]),
            effect_type=str(row["effect_type"]),
            discount_percent=int(row["discount_percent"]) if row["discount_percent"] is not None else None,
            duration_days=int(row["duration_days"]) if row["duration_days"] is not None else None,
            max_redemptions=int(row["max_redemptions"]) if row["max_redemptions"] is not None else None,
            redemption_count=int(row["redemption_count"] or 0),
            is_active=bool(row["is_active"]),
            expires_at=cls._parse_dt(row["expires_at"]),
            created_by=str(row["created_by"]),
            created_at=cls._parse_dt(row["created_at"]),
            updated_at=cls._parse_dt(row["updated_at"]),
        )

    @classmethod
    def _row_to_referral(cls, row: sqlite3.Row) -> ReferralRecord:
        return ReferralRecord(
            referral_id=int(row["id"]),
            inviter_user_id=str(row["inviter_user_id"]),
            invited_telegram_user_id=int(row["invited_telegram_user_id"]),
            invited_user_id=str(row["invited_user_id"]),
            referral_token=str(row["referral_token"]),
            created_at=cls._parse_dt(row["created_at"]),
        )

    @classmethod
    def _row_to_referral_reward(cls, row: sqlite3.Row) -> ReferralRewardRecord:
        return ReferralRewardRecord(
            reward_id=int(row["id"]),
            inviter_user_id=str(row["inviter_user_id"]),
            invited_user_id=str(row["invited_user_id"]),
            order_id=str(row["order_id"]) if row["order_id"] is not None else None,
            trigger_type=str(row["trigger_type"]),
            bonus_days=int(row["bonus_days"]),
            granted_at=cls._parse_dt(row["granted_at"]),
            revoked_at=cls._parse_dt(row["revoked_at"]),
            revoked_by=str(row["revoked_by"]) if row["revoked_by"] is not None else None,
            revoke_reason=str(row["revoke_reason"]) if row["revoke_reason"] is not None else None,
        )

    @classmethod
    def _row_to_app_release(cls, row: sqlite3.Row) -> AppReleaseRecord:
        return AppReleaseRecord(
            release_id=str(row["release_id"]),
            platform=str(row["platform"]),
            version_name=str(row["version_name"]),
            version_code=int(row["version_code"]),
            download_url=str(row["download_url"]),
            sha256=str(row["sha256"]),
            release_notes=str(row["release_notes"] or ""),
            is_current=bool(row["is_current"]),
            published_by=str(row["published_by"]),
            published_at=cls._parse_dt(row["published_at"]),
        )

    @staticmethod
    def _default_title(kind: str, source_text: str) -> str:
        if kind == "subscription_url":
            parsed = urlparse(source_text)
            if parsed.netloc:
                return parsed.netloc
            return source_text[:48]
        first_line = next(
            (line.strip() for line in source_text.splitlines() if line.strip()),
            "raw-links",
        )
        return first_line[:48]

    @staticmethod
    def _hash_secret(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _to_wire(value: datetime) -> str:
        return value.astimezone(UTC).replace(microsecond=0).isoformat()

    @staticmethod
    def _parse_dt(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        return datetime.fromisoformat(str(value)).astimezone(UTC)
