from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import choice
from threading import Lock
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.schemas.contracts import (
    AccountSummary,
    ActivationCodeExchangeRequest,
    AccountProfile,
    AppReleaseInfo,
    AppReleasePublishRequest,
    AppSession,
    AdminPromoCodeItem,
    BootstrapResponse,
    DeviceBinding,
    FeatureSettings,
    Plan,
    PromoApplyResult,
    PromoEffectType,
    PromoGrantedSubscription,
    PromoAuthExchangeRequest,
    PromoCreateRequest,
    SubscriptionOrder,
    SubscriptionState,
    SubscriptionStatus,
    ProfileUpdateRequest,
    SubscriptionOrderStatus,
    TelegramExchangeRequest,
    VpnConfigResponse,
    VpnLocation,
)
from app.services.auth_tokens import SessionClaims, issue_app_token
from app.services.catalog_importer import ExternalCatalogImporter, ImportResult
from app.services.catalog_store import (
    CatalogStore,
    DeviceRecord,
    ReferralRewardRecord,
    ReferralSummaryRecord,
    StoredCatalogEntry,
    StoredSource,
    ActivationCodeRecord,
    AppReleaseRecord,
    PromoCodeRecord,
    SubscriptionOrderRecord,
    SubscriptionPlanRecord,
    TelegramIdentityConflictError,
    UserRecord,
    UserSubscriptionRecord,
)
from app.services.marzban import (
    AVAILABLE_ROUTING_PROFILES,
    DEFAULT_ROUTING_PROFILE,
    InventorySnapshot,
    MarzbanClient,
    MarzbanError,
    build_location_identity,
    normalize_routing_profile,
    stale_copy,
)
from app.services.push_notifications import PushDispatchResult, PushNotifier
from app.services.yookassa import YooKassaClient, YooKassaError


class UserBlockedError(PermissionError):
    pass


class SubscriptionRequiredError(PermissionError):
    pass


class ActivationCodeError(ValueError):
    pass


class ActivationCodeThrottledError(ActivationCodeError):
    pass


class TrialAlreadyActivatedError(ActivationCodeError):
    pass


class PromoCodeError(ValueError):
    pass


@dataclass(slots=True)
class CachedInventory:
    snapshot: InventorySnapshot
    expires_at: datetime


@dataclass(slots=True)
class CatalogSyncSummary:
    source: StoredSource
    imported_count: int
    skipped_messages: list[str]
    delivered_push_count: int


@dataclass(slots=True)
class CatalogStatusSummary:
    marzban_locations: int
    external_sources: int
    published_entries: int
    hidden_entries: int
    disabled_entries: int
    last_synced_at: datetime | None


@dataclass(slots=True)
class PublicSubscriptionExport:
    body: str
    title: str
    user: UserRecord
    subscription: SubscriptionState


class SpeedyFacadeService:
    TRIAL_PLAN_ID = "trial_3d"
    REFERRAL_BONUS_PLAN_ID = "referral_bonus_3"
    REFERRAL_BONUS_DAYS = 3
    REFERRAL_START_BONUS_LIMIT = 5
    ACTIVATION_CODE_TTL_MINUTES = 30
    ACTIVATION_CODE_MAX_FAILURES = 5
    ACTIVATION_CODE_FAILURE_WINDOW = timedelta(minutes=10)
    ACTIVATION_CODE_REPLAY_WINDOW = timedelta(minutes=2)

    def __init__(
        self,
        settings: Settings,
        marzban_client: MarzbanClient | None = None,
        store: CatalogStore | None = None,
        importer: ExternalCatalogImporter | None = None,
        push_notifier: PushNotifier | None = None,
        yookassa_client: YooKassaClient | None = None,
    ) -> None:
        self._settings = settings
        self._marzban = marzban_client or MarzbanClient(settings)
        self._store = store or CatalogStore(settings)
        self._importer = importer or ExternalCatalogImporter(
            settings,
            marzban_client=self._marzban,
        )
        self._push_notifier = push_notifier or PushNotifier(settings, self._store)
        self._yookassa = yookassa_client or YooKassaClient(settings)
        self._inventory_cache: dict[str, CachedInventory] = {}
        self._activation_failures: dict[str, list[datetime]] = {}
        self._lock = Lock()

    @property
    def store(self) -> CatalogStore:
        return self._store

    def exchange_auth(self, payload: TelegramExchangeRequest) -> AppSession:
        claims = self._claims_from_exchange(payload)
        self._ensure_user_allowed(claims)
        session = self._build_session(
            claims,
            link_telegram=True,
            telegram_username=self._telegram_username(payload),
        )
        subscription_record = self._store.get_user_subscription(claims.user_id)
        if self._subscription_allows_vpn(subscription_record):
            try:
                self._sync_marzban_access(claims, subscription_record)
            except MarzbanError:
                counts = self._store.counts()
                if not counts.get("published_entries"):
                    raise MarzbanError("VPN Backend unreachable and no external servers available.")
        return session

    def exchange_promo_auth(self, payload: PromoAuthExchangeRequest) -> AppSession:
        user = self._resolve_promo_auth_user(payload)
        existing_redemption = self._store.get_promo_redemption(
            code=payload.code,
            user_id=user.user_id,
        )
        if existing_redemption is None:
            promo = self._require_available_promo(
                payload.code,
                user_id=user.user_id,
                effect_type=PromoEffectType.free_subscription,
            )
            if promo.duration_days is None:
                raise PromoCodeError("Промокод настроен некорректно.")
            self._grant_free_promo_subscription(
                user=user,
                promo=promo,
                code=promo.code,
            )
        claims = self._claims_for_device(
            user,
            device_id=payload.device_id,
            device_label=payload.device_label,
            platform=payload.platform,
        )
        return self._build_session(claims)

    def apply_promo(self, claims: SessionClaims, *, code: str) -> PromoApplyResult:
        self.assert_user_allowed(claims)
        promo = self._require_available_promo(code, user_id=claims.user_id)
        if promo.effect_type == PromoEffectType.free_subscription.value:
            user = self._require_user(claims.user_id)
            granted = self._grant_free_promo_subscription(
                user=user,
                promo=promo,
                code=promo.code,
            )
            plan = self._store.get_subscription_plan(granted.plan_id or "trial_3d")
            return PromoApplyResult(
                effect_type=PromoEffectType.free_subscription,
                message=f"Промокод {promo.code} активирован. Доступ уже открыт.",
                promo_code=promo.code,
                granted_subscription=PromoGrantedSubscription(
                    plan_id=plan.plan_id,
                    plan_title=plan.title,
                    duration_days=plan.duration_days,
                    expires_at=granted.expires_at,
                ),
            )

        discount_percent = promo.discount_percent or 0
        discounted_prices = {
            plan.plan_id: self._discounted_amount(plan.price_rub, discount_percent)
            for plan in self._store.list_subscription_plans()
            if not plan.is_hidden
        }
        return PromoApplyResult(
            effect_type=PromoEffectType.discount_percent,
            message=f"Промокод {promo.code} применится к следующему заказу.",
            promo_code=promo.code,
            discount_percent=discount_percent,
            discounted_prices_by_plan=discounted_prices,
        )

    def list_promo_codes(self) -> list[AdminPromoCodeItem]:
        return [self._promo_to_contract(promo) for promo in self._store.list_promo_codes()]

    def create_promo_code(self, payload: PromoCreateRequest, *, created_by: str) -> AdminPromoCodeItem:
        try:
            promo = self._store.create_promo_code(
                code=payload.code,
                effect_type=payload.effect_type.value,
                discount_percent=payload.discount_percent,
                duration_days=payload.duration_days,
                max_redemptions=payload.max_redemptions,
                expires_at=payload.expires_at,
                created_by=created_by,
            )
        except sqlite3.IntegrityError as exc:
            raise PromoCodeError("Промокод уже существует.") from exc
        return self._promo_to_contract(promo)

    def deactivate_promo_code(self, code: str) -> AdminPromoCodeItem:
        return self._promo_to_contract(
            self._store.set_promo_code_active(code, is_active=False)
        )

    def get_current_app_release_info(self, *, platform: str = "android") -> AppReleaseInfo | None:
        record = self._store.get_current_app_release(platform)
        if record is None:
            return None
        return self._app_release_to_contract(record)

    def publish_android_release(
        self,
        payload: AppReleasePublishRequest,
        *,
        published_by: str,
        download_url: str,
    ) -> AppReleaseInfo:
        apk_path = Path(self._settings.telegram_android_apk_path).expanduser()
        if not self._settings.telegram_android_apk_path.strip() or not apk_path.is_file():
            raise ValueError("Android APK is not available on the server.")
        current = self._store.get_current_app_release("android")
        if current is not None and payload.version_code <= current.version_code:
            raise ValueError("Version code must be greater than the current Android release.")
        sha256 = self._sha256_for_file(apk_path)
        published = self._store.publish_app_release(
            platform="android",
            version_name=payload.version_name.strip(),
            version_code=payload.version_code,
            download_url=download_url,
            sha256=sha256,
            release_notes=payload.release_notes.strip(),
            published_by=published_by,
        )
        self._push_notifier.notify_app_update(
            title="Доступно обновление MeFast VPN",
            body=f"Установите версию {payload.version_name}, чтобы получить последние улучшения.",
            version_name=payload.version_name.strip(),
            version_code=payload.version_code,
            download_url=download_url,
            sha256=sha256,
            release_notes=payload.release_notes.strip(),
        )
        return self._app_release_to_contract(published)

    def assert_user_allowed(self, claims: SessionClaims) -> None:
        self._store.touch_user_activity(
            user_id=claims.user_id,
            telegram_handle=claims.telegram_handle,
            display_name=claims.display_name,
            email=claims.email,
            role=claims.role,
        )
        if self._store.is_user_blocked(claims.user_id):
            raise UserBlockedError("This account is blocked.")
        self._assert_device_limit(
            user_id=claims.user_id,
            device_id=claims.device_id,
        )
        self._store.record_device(
            user_id=claims.user_id,
            device_id=claims.device_id,
            platform=claims.platform,
            label=claims.device_label,
        )

    def link_telegram_auth(self, relink_user_id: str, payload: TelegramExchangeRequest) -> AppSession:
        user = self._require_user(relink_user_id)
        if user.blocked:
            raise UserBlockedError("This account is blocked.")
            
        telegram_data = payload.telegram_auth_payload
        telegram_handle = (
            telegram_data.get("username")
            or telegram_data.get("telegram_handle")
            or ""
        ).strip()
        if telegram_handle and not telegram_handle.startswith("@"):
            telegram_handle = f"@{telegram_handle}"

        telegram_id_str = telegram_data.get("id")
        if not telegram_id_str:
            raise ValueError("Telegram ID missing in auth payload.")
            
        updated_user = self._store.link_telegram_identity(
            user_id=relink_user_id,
            telegram_user_id=int(telegram_id_str),
            telegram_username=telegram_data.get("username"),
            telegram_handle=telegram_handle,
        )
        
        dummy_claims = SessionClaims(
            user_id=relink_user_id,
            role=updated_user.role,
            telegram_user_id=updated_user.telegram_user_id,
            telegram_linked=updated_user.telegram_user_id is not None,
            marzban_username=self._marzban_username(relink_user_id),
            telegram_handle=updated_user.telegram_handle,
            display_name=updated_user.display_name,
            email=updated_user.email,
            device_id=payload.device_id,
            device_label=payload.device_label,
            platform=payload.platform,
            is_premium=self._subscription_allows_vpn(
                self._store.get_user_subscription(relink_user_id)
            ),
        )
        return self._build_session(
            dummy_claims,
            link_telegram=True,
            telegram_username=telegram_data.get("username"),
        )

    def register_push_token(
        self,
        claims: SessionClaims,
        *,
        device_id: str,
        platform: str,
        push_token: str,
        provider: str,
    ) -> DeviceBinding:
        self.assert_user_allowed(claims)
        if not push_token.strip():
            raise ValueError("Push token cannot be empty.")
        self._store.register_push_token(
            user_id=claims.user_id,
            device_id=device_id,
            platform=platform,
            label=claims.device_label,
            push_token=push_token.strip(),
            push_provider=provider.strip().lower() or "fcm",
        )
        now = datetime.now(UTC)
        return DeviceBinding(
            device_id=device_id,
            platform=platform,
            label=claims.device_label,
            created_at=now,
            last_seen_at=now,
        )

    def build_bootstrap(self, claims: SessionClaims) -> BootstrapResponse:
        self.assert_user_allowed(claims)
        session = self._build_session(claims)
        summary = self.get_account_summary(claims)
        subscription = summary.subscription
        android_update = (
            self.get_current_app_release_info(platform="android")
            if claims.platform.strip().lower() == "android"
            else None
        )
        if not subscription.can_use_vpn:
            return BootstrapResponse(
                session=session,
                account=summary.account,
                subscription=subscription,
                devices=summary.devices,
                orders=summary.orders,
                locations=[],
                selected_location_id="",
                feature_settings=FeatureSettings(),
                inventory_stale=False,
                android_update=android_update,
            )
        snapshot = self._inventory_for(claims)
        return BootstrapResponse(
            session=session,
            account=summary.account,
            subscription=subscription,
            devices=summary.devices,
            orders=summary.orders,
            locations=snapshot.locations,
            selected_location_id=snapshot.default_location_id,
            feature_settings=FeatureSettings(),
            inventory_stale=snapshot.stale,
            android_update=android_update,
        )

    def get_locations(
        self,
        claims: SessionClaims,
        *,
        force_refresh: bool = False,
    ) -> list[VpnLocation]:
        self.assert_user_allowed(claims)
        if not self._has_vpn_access(claims):
            return []
        return self._inventory_for(claims, force_refresh=force_refresh).locations

    def select_location(self, claims: SessionClaims, location_id: str) -> VpnLocation:
        self.assert_user_allowed(claims)
        if not self._has_vpn_access(claims):
            raise SubscriptionRequiredError("Subscription is required for VPN access.")
        snapshot = self._inventory_for(claims)
        for location in snapshot.locations:
            if location.id == location_id:
                return location
        raise KeyError(location_id)

    def get_profile(self, claims: SessionClaims) -> AccountProfile:
        self.assert_user_allowed(claims)
        return self.get_account_summary(claims).account

    def update_profile(
        self,
        claims: SessionClaims,
        *,
        display_name: object,
        email: object,
    ) -> AccountProfile:
        self.assert_user_allowed(claims)
        user = self._store.update_user_profile(
            claims.user_id,
            display_name=display_name,
            email=email,
        )
        return AccountProfile(
            user_id=user.user_id,
            role=user.role,
            telegram_user_id=user.telegram_user_id,
            telegram_linked=user.telegram_user_id is not None,
            display_name=user.display_name,
            telegram_handle=user.telegram_handle,
            email=user.email,
            current_ip_address="Недоступен",
            device_label=claims.device_label,
            device_id=claims.device_id,
        )

    def get_subscription(self, claims: SessionClaims) -> SubscriptionState:
        self.assert_user_allowed(claims)
        return self._build_subscription_state(claims)

    def get_plans(self) -> list[Plan]:
        return [self._plan_to_contract(plan) for plan in self._store.list_subscription_plans()]

    def update_plan_price(self, plan_id: str, new_price_rub: int) -> SubscriptionPlanRecord:
        return self._store.update_plan_price(plan_id, new_price_rub)

    def list_orders(self, claims: SessionClaims, *, limit: int = 10) -> list[SubscriptionOrder]:
        self.assert_user_allowed(claims)
        user = self._require_user(claims.user_id)
        return [
            self._order_to_contract(order, user=user)
            for order in self._store.list_subscription_orders(user_id=claims.user_id, limit=limit)
        ]

    def get_account_summary(self, claims: SessionClaims) -> AccountSummary:
        self.assert_user_allowed(claims)
        user = self._require_user(claims.user_id)
        subscription = self._build_subscription_state(claims)
        devices = self._store.list_devices(user.user_id)
        orders = self.list_orders(claims)
        return self._build_account_summary(
            user=user,
            subscription=subscription,
            devices=devices,
            orders=orders,
            fallback_device_id=claims.device_id,
            fallback_device_label=claims.device_label,
            claims=claims,
        )

    def get_user_account_summary_by_id(self, user_id: str) -> AccountSummary:
        user = self._require_user(user_id)
        claims = self._claims_for_user(user)
        subscription = self._build_subscription_state(claims)
        devices = self._store.list_devices(user_id)
        orders = self.list_user_orders(user_id, limit=10)
        return self._build_account_summary(
            user=user,
            subscription=subscription,
            devices=devices,
            orders=orders,
            fallback_device_id=devices[0].device_id if devices else "—",
            fallback_device_label=devices[0].label if devices else "Не привязано",
            claims=claims,
        )

    def list_users_page(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        view: str = "all",
    ) -> tuple[list[UserRecord], int]:
        filters: dict[str, object] = {}
        if view == "active":
            filters["subscription_status"] = "active"
        elif view == "no_access":
            filters["subscription_status"] = "no_access"
        elif view == "blocked":
            filters["blocked"] = True
        return self._store.get_paginated_users(
            offset=offset,
            limit=limit,
            **filters,
        )

    def create_order(
        self,
        claims: SessionClaims,
        *,
        plan_id: str,
        promo_code: str | None = None,
    ) -> SubscriptionOrder:
        self.assert_user_allowed(claims)
        normalized_promo_code = promo_code.strip().upper() if promo_code is not None and promo_code.strip() else None
        existing_orders = self._store.list_subscription_orders(
            user_id=claims.user_id,
            statuses=("pending_payment", "waiting_confirmation"),
            limit=1,
        )
        if existing_orders:
            existing = existing_orders[0]
            if existing.plan_id == plan_id and existing.promo_code == normalized_promo_code:
                user = self._require_user(claims.user_id)
                existing = self._ensure_yookassa_payment_for_order(existing, user=user)
                return self._order_to_contract(existing, user=user)
        plan = self._store.get_subscription_plan(plan_id)
        if plan.is_hidden:
            raise ValueError("Этот тариф нельзя оформить вручную.")
        amount_rub = plan.price_rub
        discount_amount_rub: int | None = None
        if normalized_promo_code is not None:
            promo = self._require_available_promo(
                normalized_promo_code,
                user_id=claims.user_id,
                effect_type=PromoEffectType.discount_percent,
            )
            discount_amount_rub = self._discount_amount(
                plan.price_rub,
                promo.discount_percent or 0,
            )
            amount_rub = self._discounted_amount(
                plan.price_rub,
                promo.discount_percent or 0,
            )
        order = self._store.create_subscription_order(
            user_id=claims.user_id,
            plan_id=plan_id,
            amount_rub=amount_rub,
            original_amount_rub=plan.price_rub,
            discount_amount_rub=discount_amount_rub,
            promo_code=normalized_promo_code,
        )
        if (
            normalized_promo_code is not None
            and self._store.get_promo_redemption(
                code=normalized_promo_code,
                user_id=claims.user_id,
            ) is None
        ):
            self._store.record_promo_redemption(
                code=normalized_promo_code,
                user_id=claims.user_id,
                order_id=order.order_id,
            )
        user = self._require_user(claims.user_id)
        order = self._ensure_yookassa_payment_for_order(order, user=user)
        return self._order_to_contract(order, user=user)

    def cancel_order(self, claims: SessionClaims, *, order_id: str) -> SubscriptionOrder:
        self.assert_user_allowed(claims)
        order = self._store.cancel_order(order_id=order_id, user_id=claims.user_id)
        return self._order_to_contract(order, user=self._require_user(claims.user_id))

    def mark_order_paid(self, claims: SessionClaims, *, order_id: str) -> SubscriptionOrder:
        self.assert_user_allowed(claims)
        order = self._store.mark_order_paid(order_id=order_id, user_id=claims.user_id)
        user = self._require_user(claims.user_id)
        self._push_notifier.notify_subscription_update(
            user_id=claims.user_id,
            title="Оплата отправлена на проверку",
            body=f"Заказ {order.order_id} ожидает подтверждения администратором.",
            event_type="subscription_updated",
            reason="order_waiting_confirmation",
        )
        return self._order_to_contract(order, user=user)

    def telegram_payments_enabled(self) -> bool:
        return bool(
            self._settings.telegram_payments_provider_token.strip()
            and self._settings.telegram_bot_username.strip()
        )

    def yookassa_payments_enabled(self) -> bool:
        return bool(
            self._settings.yookassa_shop_id.strip()
            and self._settings.yookassa_secret_key.strip()
        )

    def online_payments_enabled(self) -> bool:
        return self.yookassa_payments_enabled()

    def check_yookassa_order(self, *, order_id: str) -> tuple[SubscriptionOrder, bool]:
        record = self._store.get_subscription_order(order_id)
        user = self._require_user(record.user_id)
        updated, activated = self._sync_yookassa_order_record(record, user=user)
        return self._order_to_contract(updated, user=user), activated

    def process_pending_yookassa_orders(self, *, limit: int = 30) -> list[SubscriptionOrder]:
        if not self.yookassa_payments_enabled():
            return []
        confirmed: list[SubscriptionOrder] = []
        pending = self._store.list_subscription_orders(
            statuses=("pending_payment", "waiting_confirmation"),
            limit=limit,
        )
        for record in pending:
            if record.payment_provider != "yookassa" or not record.provider_payment_id:
                continue
            user = self._require_user(record.user_id)
            try:
                updated, activated = self._sync_yookassa_order_record(record, user=user)
            except YooKassaError:
                continue
            if activated:
                confirmed.append(self._order_to_contract(updated, user=user))
        return confirmed

    def validate_telegram_payment(
        self,
        *,
        telegram_user_id: int,
        order_id: str,
        total_amount: int,
        currency: str,
    ) -> SubscriptionOrder:
        user = self._store.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            raise PermissionError("Telegram аккаунт не привязан к профилю MeFast VPN.")

        order = self._store.get_subscription_order(order_id)
        if order.user_id != user.user_id:
            raise PermissionError("Этот счет принадлежит другому пользователю.")
        if order.status != "pending_payment":
            raise ValueError("Счет уже обработан или больше недоступен.")
        if currency.upper() != "RUB":
            raise ValueError("Поддерживается только валюта RUB.")

        expected_total = order.amount_rub * 100
        if total_amount != expected_total:
            raise ValueError("Сумма счета изменилась. Создайте заказ заново.")

        return self._order_to_contract(order, user=user)

    def complete_telegram_order(
        self,
        *,
        telegram_user_id: int,
        order_id: str,
    ) -> SubscriptionOrder:
        user = self._store.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            raise PermissionError("Telegram аккаунт не привязан к профилю MeFast VPN.")

        existing_order = self._store.get_subscription_order(order_id)
        if existing_order.user_id != user.user_id:
            raise PermissionError("Этот платеж не относится к вашему аккаунту.")
        if existing_order.status == "confirmed":
            return self._order_to_contract(existing_order, user=user)
        if existing_order.status not in {"pending_payment", "waiting_confirmation"}:
            raise ValueError("Заказ нельзя подтвердить по платежному событию.")

        order = self._store.confirm_order_paid(
            order_id=order_id,
            confirmed_by="telegram_payment",
        )
        self.grant_subscription(
            user_id=user.user_id,
            plan_id=order.plan_id,
            granted_by="telegram_payment",
        )
        self.grant_referral_reward_for_order(order.order_id)
        return self._order_to_contract(order, user=user)

    def _ensure_yookassa_payment_for_order(
        self,
        order: SubscriptionOrderRecord,
        *,
        user: UserRecord,
    ) -> SubscriptionOrderRecord:
        if not self.yookassa_payments_enabled():
            return order
        if order.status != "pending_payment":
            return order
        if order.payment_provider == "yookassa" and order.payment_confirmation_url:
            return order

        idempotence_key = order.payment_idempotence_key or order.order_id
        payment = self._yookassa.create_payment(
            amount_rub=order.amount_rub,
            order_id=order.order_id,
            description=f"{self._service_name()} • {self._store.get_subscription_plan(order.plan_id).title}",
            return_url=f"{self._public_base_url()}/pay/return/{order.order_id}",
            idempotence_key=idempotence_key,
            metadata={
                "user_id": user.user_id,
                "telegram_id": str(user.telegram_user_id or ""),
                "plan_id": order.plan_id,
            },
        )
        return self._store.record_order_provider_payment(
            order_id=order.order_id,
            payment_provider="yookassa",
            provider_payment_id=payment.payment_id,
            payment_confirmation_url=payment.confirmation_url,
            provider_payment_status=payment.status,
        )

    def _sync_yookassa_order_record(
        self,
        record: SubscriptionOrderRecord,
        *,
        user: UserRecord,
    ) -> tuple[SubscriptionOrderRecord, bool]:
        if record.payment_provider != "yookassa" or not record.provider_payment_id:
            return record, False
        if record.status == "confirmed":
            return record, False

        payment = self._yookassa.get_payment(record.provider_payment_id)
        if payment.status == "succeeded" and payment.paid:
            updated = self._store.confirm_order_paid(
                order_id=record.order_id,
                confirmed_by="yookassa",
            )
            updated = self._store.update_order_provider_payment_status(
                order_id=record.order_id,
                provider_payment_status=payment.status,
            )
            self.grant_subscription(
                user_id=user.user_id,
                plan_id=record.plan_id,
                granted_by="yookassa",
            )
            self.grant_referral_reward_for_order(updated.order_id)
            return updated, True
        if payment.status == "canceled":
            updated = self._store.cancel_order_by_provider(
                order_id=record.order_id,
                provider_payment_status=payment.status,
            )
            return updated, False
        updated = self._store.update_order_provider_payment_status(
            order_id=record.order_id,
            provider_payment_status=payment.status,
        )
        return updated, False

    def get_vpn_config(
        self,
        claims: SessionClaims,
        location_id: str,
        routing_profile: str | None = None,
    ) -> VpnConfigResponse:
        self.assert_user_allowed(claims)
        if not self._has_vpn_access(claims):
            raise SubscriptionRequiredError("Subscription is required for VPN access.")
        snapshot = self._inventory_for(claims)
        profile = normalize_routing_profile(routing_profile)
        config_profiles = snapshot.config_by_location_id.get(location_id)
        if config_profiles is None:
            raise KeyError(location_id)
        config_json = config_profiles.get(profile)
        if config_json is None:
            raise KeyError(location_id)

        location = self.select_location(claims, location_id)
        
        protocol = location.protocol.lower()
        if protocol in ("wireguard", "wg"):
            engine = "wireguard"
        elif protocol in ("amneziawg", "awg", "amnezia"):
            engine = "amnezia"
        elif protocol in ("openvpn", "ovpn"):
            engine = "openvpn"
        elif protocol in ("hysteria", "hysteria2"):
            engine = "sing-box"
        else:
            engine = "xray"

        return VpnConfigResponse(
            server_id=location.id,
            engine=engine,
            protocol=location.protocol,
            config_json=config_json,
            updated_at=snapshot.fetched_at,
            routing_profile=profile,
            available_routing_profiles=list(snapshot.available_routing_profiles),
        )

    def add_external_source(
        self,
        *,
        kind: str,
        source_text: str,
        created_by: str,
        title: str | None = None,
    ) -> CatalogSyncSummary:
        source = self._store.create_source(
            kind=kind,
            source_text=source_text,
            created_by=created_by,
            title=title,
        )
        return self.sync_external_source(source.source_id)

    def sync_external_source(self, source_id: str) -> CatalogSyncSummary:
        source = self._store.get_source(source_id)
        try:
            result = self._importer.import_source(
                kind=source.kind,
                source_text=source.source_text,
            )
        except Exception as exc:
            self._store.mark_source_sync_error(source_id, str(exc))
            raise

        updated_source = self._store.replace_source_entries(
            source_id=source.source_id,
            entries=result.entries,
            last_error=None,
        )
        self._clear_inventory_cache()
        push_result = self._push_notifier.notify_catalog_refresh(reason="catalog_sync")
        return CatalogSyncSummary(
            source=updated_source,
            imported_count=len(result.entries),
            skipped_messages=result.skipped_messages,
            delivered_push_count=push_result.delivered_devices,
        )

    def list_external_sources(self, *, limit: int = 20) -> list[StoredSource]:
        return self._store.list_sources(limit=limit)

    def get_external_source(self, source_id: str) -> StoredSource:
        return self._store.get_source(source_id)

    def update_external_source_title(self, source_id: str, title: str) -> StoredSource:
        return self._store.update_source_title(source_id, title)

    def delete_external_source(self, source_id: str) -> StoredSource:
        deleted = self._store.delete_source(source_id)
        self._clear_inventory_cache()
        self._push_notifier.notify_catalog_refresh(reason="catalog_source_deleted")
        return deleted

    def list_catalog_entries(
        self,
        *,
        include_hidden: bool = True,
        limit: int = 20,
        offset: int = 0,
        source_id: str | None = None,
    ) -> list[StoredCatalogEntry]:
        return self._store.list_entries(
            include_hidden=include_hidden,
            limit=limit,
            offset=offset,
            source_id=source_id,
        )

    def get_catalog_entry(self, entry_id: str) -> StoredCatalogEntry:
        return self._store.get_entry(entry_id)

    def delete_catalog_entry(self, entry_id: str) -> StoredCatalogEntry:
        deleted = self._store.delete_entry(entry_id)
        self._clear_inventory_cache()
        self._push_notifier.notify_catalog_refresh(reason="catalog_entry_deleted")
        return deleted

    def set_catalog_entry_state(
        self,
        entry_id: str,
        *,
        state: str,
    ) -> StoredCatalogEntry:
        updated = self._store.set_entry_publish_state(entry_id, state)
        self._clear_inventory_cache()
        self._push_notifier.notify_catalog_refresh(reason=f"catalog_{state}")
        return updated

    def update_catalog_entry_display_name(
        self,
        entry_id: str,
        *,
        display_name: str,
    ) -> StoredCatalogEntry:
        updated = self._store.update_entry_display_name(entry_id, display_name)
        self._clear_inventory_cache()
        self._push_notifier.notify_catalog_refresh(reason="catalog_entry_renamed")
        return updated

    def search_users(self, query: str, *, limit: int = 10) -> list[UserRecord]:
        return self._store.search_users(query, limit=limit)

    def get_user(self, user_id: str) -> UserRecord:
        return self._require_user(user_id)

    def register_telegram_onboarding(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        platform: str,
    ) -> UserRecord:
        normalized_platform = self._normalize_delivery_platform(platform)
        linked_user = self._store.get_user_by_telegram_id(telegram_user_id)
        telegram_handle = (username or f"telegram:{telegram_user_id}").strip()
        if not telegram_handle.startswith("@") and not telegram_handle.startswith("telegram:"):
            telegram_handle = f"@{telegram_handle}"

        user_id = linked_user.user_id if linked_user is not None else self._stable_identifier(str(telegram_user_id))
        user = self._store.upsert_user(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            telegram_username=username,
            telegram_handle=linked_user.telegram_handle if linked_user is not None else telegram_handle,
            display_name=linked_user.display_name if linked_user is not None else display_name.strip(),
            email=linked_user.email if linked_user is not None else None,
            role=linked_user.role if linked_user is not None else "user",
        )
        user = self._store.set_user_preferred_platform(
            user.user_id,
            preferred_platform=normalized_platform,
        )
        self._store.record_device(
            user_id=user.user_id,
            device_id=f"telegram-onboarding-{normalized_platform}",
            platform=normalized_platform,
            label=self._telegram_device_label(normalized_platform),
        )
        return self._require_user(user.user_id)

    def update_user_delivery_platform(
        self,
        *,
        user_id: str,
        platform: str,
    ) -> UserRecord:
        normalized_platform = self._normalize_delivery_platform(platform)
        user = self._require_user(user_id)
        updated = self._store.set_user_preferred_platform(
            user.user_id,
            preferred_platform=normalized_platform,
        )
        self._store.record_device(
            user_id=user.user_id,
            device_id=f"telegram-onboarding-{normalized_platform}",
            platform=normalized_platform,
            label=self._telegram_device_label(normalized_platform),
        )
        return updated

    def get_user_delivery_platform(self, user_id: str) -> str:
        user = self._require_user(user_id)
        if user.preferred_platform is not None:
            return self._normalize_delivery_platform(user.preferred_platform)
        for device in self._store.list_devices(user_id):
            try:
                return self._normalize_delivery_platform(device.platform)
            except ValueError:
                continue
        return "subscription"

    def get_user_subscription_url(self, user_id: str) -> str:
        user = self._require_user(user_id)
        subscription = self._store.get_user_subscription(user_id)
        if not self._subscription_allows_vpn(subscription):
            raise SubscriptionRequiredError("Активная подписка нужна для получения ключа.")
        claims = self._claims_for_user(user)
        self._sync_marzban_access(claims, subscription)
        if not hasattr(self._marzban, "get_subscription_url"):
            raise MarzbanError("Current Marzban client cannot provide subscription URL.")
        return self._marzban.get_subscription_url(
            username=claims.marzban_username,
            display_name=user.display_name,
        )

    def get_public_subscription_url(
        self,
        user_id: str,
        *,
        base_url: str | None = None,
    ) -> str:
        self._require_user(user_id)
        token = self._store.get_or_create_subscription_token(user_id)
        return f"{self._public_base_url(base_url)}/s/{token}"

    def get_public_happ_install_url(
        self,
        user_id: str,
        *,
        base_url: str | None = None,
    ) -> str:
        self._require_user(user_id)
        token = self._store.get_or_create_subscription_token(user_id)
        return f"{self._public_base_url(base_url)}/happ/{token}"

    def rotate_public_subscription_url(
        self,
        user_id: str,
        *,
        base_url: str | None = None,
    ) -> str:
        self._require_user(user_id)
        token = self._store.rotate_subscription_token(user_id)
        return f"{self._public_base_url(base_url)}/s/{token}"

    def get_referral_url(self, user_id: str) -> str:
        self._require_user(user_id)
        token = self._store.get_or_create_referral_token(user_id)
        bot_username = self._settings.telegram_bot_username.strip()
        if not bot_username:
            return f"ref_{token}"
        return f"https://t.me/{bot_username}?start=ref_{token}"

    def get_referral_count(self, user_id: str) -> int:
        self._require_user(user_id)
        return self._store.count_referrals(user_id)

    def record_referral_start(
        self,
        *,
        referral_token: str,
        telegram_user_id: int,
    ) -> bool:
        invited_user_id = self._stable_identifier(str(telegram_user_id))
        owner = self._store.get_referral_owner(referral_token)
        if owner is None:
            return False
        recorded = self._store.record_referral_visit(
            referral_token=referral_token,
            invited_telegram_user_id=telegram_user_id,
            invited_user_id=invited_user_id,
        )
        if not recorded:
            return False
        invited_count = self._store.count_referrals(owner.user_id)
        if invited_count <= self.REFERRAL_START_BONUS_LIMIT:
            self._grant_referral_reward(
                inviter_user_id=owner.user_id,
                invited_user_id=invited_user_id,
                order_id=None,
                trigger_type="start",
            )
        return True

    def grant_referral_reward_for_order(
        self,
        order_id: str,
    ) -> ReferralRewardRecord | None:
        order = self._store.get_subscription_order(order_id)
        if order.status != "confirmed" or order.amount_rub <= 0:
            return None
        referral = self._store.get_referral_by_invited_user(order.user_id)
        if referral is None:
            return None
        return self._grant_referral_reward(
            inviter_user_id=referral.inviter_user_id,
            invited_user_id=referral.invited_user_id,
            order_id=order.order_id,
            trigger_type="paid",
        )

    def list_referral_leaderboard(self, *, limit: int = 10) -> list[ReferralSummaryRecord]:
        return self._store.list_referral_leaderboard(limit=limit)

    def get_referral_summary(self, user_id: str) -> ReferralSummaryRecord:
        self._require_user(user_id)
        return self._store.get_referral_summary(user_id)

    def list_referral_rewards(
        self,
        *,
        inviter_user_id: str | None = None,
        limit: int = 20,
    ) -> list[ReferralRewardRecord]:
        return self._store.list_referral_rewards(
            inviter_user_id=inviter_user_id,
            limit=limit,
        )

    def adjust_subscription_days(
        self,
        *,
        user_id: str,
        delta_days: int,
        reason: str,
        actor: str,
        related_referral_reward_id: int | None = None,
    ) -> UserSubscriptionRecord:
        if delta_days > 0:
            self._ensure_referral_bonus_plan(duration_days=delta_days)
            subscription = self._store.activate_subscription(
                user_id=user_id,
                plan_id=self.REFERRAL_BONUS_PLAN_ID,
                duration_days=delta_days,
                granted_by=actor,
            )
            self._store.record_subscription_adjustment(
                user_id=user_id,
                delta_days=delta_days,
                reason=reason,
                actor=actor,
                related_referral_reward_id=related_referral_reward_id,
            )
        else:
            subscription = self._store.adjust_subscription_days(
                user_id=user_id,
                delta_days=delta_days,
                reason=reason,
                actor=actor,
                related_referral_reward_id=related_referral_reward_id,
            )
        user = self._require_user(user_id)
        self._sync_marzban_access(self._claims_for_user(user), subscription)
        self._clear_inventory_cache_for(user_id)
        return subscription

    def revoke_referral_reward(
        self,
        *,
        reward_id: int,
        actor: str,
    ) -> ReferralRewardRecord:
        reward = self._store.get_referral_reward(reward_id)
        if reward.revoked_at is not None:
            return reward
        revoked = self._store.revoke_referral_reward(
            reward_id=reward_id,
            revoked_by=actor,
            reason="admin_revoke",
        )
        self.adjust_subscription_days(
            user_id=revoked.inviter_user_id,
            delta_days=-revoked.bonus_days,
            reason="referral_reward_revoked",
            actor=actor,
            related_referral_reward_id=revoked.reward_id,
        )
        return revoked

    def reset_subscription_devices(self, user_id: str) -> int:
        self._require_user(user_id)
        return self._store.reset_subscription_token_devices(user_id)

    def build_public_subscription_export(
        self,
        token: str,
        *,
        client_host: str | None = None,
        user_agent: str | None = None,
    ) -> PublicSubscriptionExport:
        user = self._store.get_user_by_subscription_token(token)
        if user is None:
            raise KeyError(token)
        if user.blocked:
            raise UserBlockedError("This account is blocked.")

        claims = self._claims_for_user(user)
        subscription = self._build_subscription_state(claims)
        if not subscription.can_use_vpn:
            self._store.touch_subscription_token(token)
            return self._placeholder_subscription_export(
                user=user,
                subscription=subscription,
                reason="expired",
            )

        lines = self._subscription_share_lines(user, subscription)
        if not lines:
            raise MarzbanError("В подписке пока нет доступных серверов.")

        self._store.touch_subscription_token(token)
        title = self._subscription_profile_title(user)
        return PublicSubscriptionExport(
            body="\n".join(lines) + "\n",
            title=title,
            user=user,
            subscription=subscription,
        )

    def build_happ_deep_link(
        self,
        token: str,
        *,
        request_base_url: str | None = None,
    ) -> str:
        export = self.build_public_subscription_export(token)
        base_url = self._public_base_url(request_base_url)
        subscription_url = f"{base_url}/s/{token}"
        install_code = self._ensure_happ_install_code(export.user)
        if install_code is not None:
            subscription_url = self._with_happ_install_fragment(
                subscription_url,
                install_code=install_code,
            )
        if not self._settings.happ_crypto_links_enabled:
            return subscription_url
        try:
            return self._encrypt_happ_link(subscription_url)
        except MarzbanError:
            return subscription_url

    def public_subscription_headers(
        self,
        export: PublicSubscriptionExport,
        *,
        request_base_url: str | None = None,
    ) -> dict[str, str]:
        expires_at = export.subscription.expires_at
        expire_ts = int(expires_at.timestamp()) if expires_at is not None else 0
        upload = export.subscription.used_traffic_bytes
        download = export.subscription.lifetime_used_traffic_bytes
        total = export.subscription.traffic_limit_bytes or 0
        return {
            "content-type": "text/plain; charset=utf-8",
            "content-disposition": f'attachment; filename="{export.title}"',
            "profile-title": export.title,
            "subscription-userinfo": (
                f"upload={upload}; download={download}; total={total}; expire={expire_ts}"
            ),
            "profile-web-page-url": self._public_logo_url(request_base_url),
            "support-url": self._settings.support_url,
            "profile-update-interval": "12",
        }

    def can_activate_trial(self, user_id: str) -> bool:
        user = self._require_user(user_id)
        return user.trial_activated_at is None

    def activate_trial(self, *, user_id: str) -> UserSubscriptionRecord:
        user = self._require_user(user_id)
        if user.trial_activated_at is not None:
            raise TrialAlreadyActivatedError("Пробный период уже был активирован.")
        subscription = self._store.get_user_subscription(user_id)
        if self._subscription_allows_vpn(subscription):
            raise TrialAlreadyActivatedError("У вас уже есть активный доступ.")
        granted = self.grant_subscription(
            user_id=user_id,
            plan_id=self.TRIAL_PLAN_ID,
            granted_by="telegram_trial",
        )
        self._store.mark_trial_activated(user_id)
        return granted

    def issue_activation_code(
        self,
        *,
        user_id: str,
        purpose: str = "device_access",
    ) -> ActivationCodeRecord:
        user = self._require_user(user_id)
        subscription = self._store.get_user_subscription(user_id)
        if not self._subscription_allows_vpn(subscription):
            raise SubscriptionRequiredError("Активная подписка нужна, чтобы получить код для приложения.")

        expires_at = datetime.now(UTC) + timedelta(minutes=self.ACTIVATION_CODE_TTL_MINUTES)
        for _ in range(100):
            code = "".join(choice("0123456789") for _ in range(4))
            existing = self._store.get_activation_code(code)
            if existing is not None and existing.status == "active":
                continue
            try:
                return self._store.create_activation_code(
                    user_id=user.user_id,
                    code=code,
                    purpose=purpose,
                    expires_at=expires_at,
                )
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Не удалось сгенерировать код активации. Попробуйте еще раз.")

    def exchange_activation_code(self, payload: ActivationCodeExchangeRequest) -> AppSession:
        self._ensure_activation_not_throttled(payload.device_id)
        record = self._store.get_activation_code(payload.code)
        if record is None:
            self._register_activation_failure(payload.device_id)
            raise ActivationCodeError("Код не найден. Проверьте 4 цифры и попробуйте снова.")
        if record.status == "used":
            replay_session = self._restore_recent_activation_session(record, payload)
            if replay_session is not None:
                self._clear_activation_failures(payload.device_id)
                return replay_session
            self._register_activation_failure(payload.device_id)
            raise ActivationCodeError("Этот код уже использован. Получите новый код в Telegram-боте.")
        if record.status == "expired":
            self._register_activation_failure(payload.device_id)
            raise ActivationCodeError("Срок действия кода истек. Получите новый код в Telegram-боте.")
        user = self._require_user(record.user_id)
        if user.blocked:
            raise UserBlockedError("This account is blocked.")

        self._store.consume_activation_code(record.activation_code_id)
        self._clear_activation_failures(payload.device_id)

        if payload.platform.strip().lower() in {"android", "ios"}:
            self._store.set_user_preferred_platform(
                user.user_id,
                preferred_platform=payload.platform.strip().lower(),
            )
            user = self._require_user(user.user_id)

        claims = self._claims_for_device(
            user,
            device_id=payload.device_id,
            device_label=payload.device_label,
            platform=payload.platform,
        )
        return self._build_session(claims)

    def _restore_recent_activation_session(
        self,
        record: ActivationCodeRecord,
        payload: ActivationCodeExchangeRequest,
    ) -> AppSession | None:
        if record.consumed_at is None:
            return None
        if record.consumed_at + self.ACTIVATION_CODE_REPLAY_WINDOW < datetime.now(UTC):
            return None
        existing_device = self._store.get_device(
            user_id=record.user_id,
            device_id=payload.device_id,
        )
        if existing_device is None:
            return None

        user = self._require_user(record.user_id)
        if user.blocked:
            raise UserBlockedError("This account is blocked.")

        if payload.platform.strip().lower() in {"android", "ios"}:
            self._store.set_user_preferred_platform(
                user.user_id,
                preferred_platform=payload.platform.strip().lower(),
            )
            user = self._require_user(user.user_id)

        claims = self._claims_for_device(
            user,
            device_id=payload.device_id,
            device_label=payload.device_label,
            platform=payload.platform,
        )
        return self._build_session(claims)

    def set_user_blocked(self, user_id: str, blocked: bool) -> UserRecord:
        return self._store.set_user_blocked(user_id, blocked)

    def delete_user(self, user_id: str) -> UserRecord:
        user = self._require_user(user_id)
        try:
            delete_remote_user = getattr(self._marzban, "delete_user", None)
            if callable(delete_remote_user):
                delete_remote_user(self._marzban_username(user.user_id), ignore_missing=True)
        except MarzbanError:
            pass
        return self._store.delete_user(user_id)

    def list_user_orders(self, user_id: str, *, limit: int = 10) -> list[SubscriptionOrder]:
        user = self._require_user(user_id)
        return [
            self._order_to_contract(order, user=user)
            for order in self._store.list_subscription_orders(user_id=user_id, limit=limit)
        ]

    def get_user_profile_by_id(self, user_id: str) -> AccountProfile:
        return self.get_user_account_summary_by_id(user_id).account

    def get_user_subscription_by_id(self, user_id: str) -> SubscriptionState:
        return self._build_subscription_state(self._claims_for_user(self._require_user(user_id)))

    def list_user_devices(self, user_id: str) -> list[DeviceRecord]:
        return self._store.list_devices(user_id)

    def list_orders_for_review(self, *, limit: int = 10) -> list[SubscriptionOrder]:
        return [
            self._order_to_contract(order)
            for order in self._store.list_subscription_orders(
                statuses=("waiting_confirmation", "pending_payment"),
                limit=limit,
            )
        ]

    def get_order(self, order_id: str) -> SubscriptionOrder:
        return self._order_to_contract(self._store.get_subscription_order(order_id))

    def grant_subscription(
        self,
        *,
        user_id: str,
        plan_id: str,
        granted_by: str,
    ) -> UserSubscriptionRecord:
        user = self._require_user(user_id)
        plan = self._store.get_subscription_plan(plan_id)
        subscription = self._store.activate_subscription(
            user_id=user_id,
            plan_id=plan.plan_id,
            duration_days=plan.duration_days,
            granted_by=granted_by,
        )
        claims = self._claims_for_user(user)
        self._sync_marzban_access(claims, subscription)
        self._clear_inventory_cache_for(user_id)
        self._push_notifier.notify_subscription_update(
            user_id=user_id,
            title="Подписка активирована",
            body=f"Тариф «{plan.title}» активен до {subscription.expires_at:%d.%m.%Y}.",
            event_type="subscription_updated",
            reason="subscription_granted",
        )
        return subscription

    def revoke_subscription(
        self,
        *,
        user_id: str,
        reason: str,
        granted_by: str,
    ) -> UserSubscriptionRecord:
        user = self._require_user(user_id)
        subscription = self._store.revoke_subscription(
            user_id=user_id,
            reason=reason,
            granted_by=granted_by,
        )
        claims = self._claims_for_user(user)
        self._sync_marzban_access(claims, subscription)
        self._clear_inventory_cache_for(user_id)
        self._push_notifier.notify_subscription_update(
            user_id=user_id,
            title="Подписка отключена",
            body="Оплатите подписку, чтобы снова увидеть список серверов.",
            event_type="subscription_revoked",
            reason=reason,
        )
        return subscription

    def approve_order(
        self,
        *,
        order_id: str,
        admin_actor: str,
    ) -> SubscriptionOrder:
        order = self._store.confirm_order(order_id=order_id, confirmed_by=admin_actor)
        self.grant_subscription(
            user_id=order.user_id,
            plan_id=order.plan_id,
            granted_by=admin_actor,
        )
        self.grant_referral_reward_for_order(order.order_id)
        user = self._require_user(order.user_id)
        return self._order_to_contract(order, user=user)

    def reject_order(
        self,
        *,
        order_id: str,
        admin_actor: str,
    ) -> SubscriptionOrder:
        order = self._store.reject_order(order_id=order_id, confirmed_by=admin_actor)
        self._push_notifier.notify_subscription_update(
            user_id=order.user_id,
            title="Заказ отклонен",
            body="Администратор отклонил заказ. Создайте новый заказ или свяжитесь с поддержкой.",
            event_type="subscription_updated",
            reason="order_rejected",
        )
        return self._order_to_contract(order)

    def get_catalog_status(self) -> CatalogStatusSummary:
        counts = self._store.counts()
        last_synced_at = counts["last_synced_at"]
        marzban_locations = 0
        try:
            snapshot = self._marzban.build_inventory(
                username=self._marzban_username("catalog_admin_status"),
                display_name="Catalog Admin Status",
            )
            marzban_locations = len(snapshot.locations)
        except MarzbanError:
            marzban_locations = 0
        return CatalogStatusSummary(
            marzban_locations=marzban_locations,
            external_sources=int(counts["sources"]),
            published_entries=int(counts["published_entries"]),
            hidden_entries=int(counts["hidden_entries"]),
            disabled_entries=int(counts["disabled_entries"]),
            last_synced_at=(
                datetime.fromisoformat(str(last_synced_at)).astimezone(UTC)
                if isinstance(last_synced_at, str)
                else None
            ),
        )

    def ping_all_locations(self, claims: SessionClaims) -> list[VpnLocation]:
        self.assert_user_allowed(claims)
        if not self._has_vpn_access(claims):
            self._clear_inventory_cache_for(claims.user_id)
            return []
        snapshot = self._inventory_for(claims, force_refresh=True)
        refreshed_snapshot = self._reprobe_snapshot(snapshot)
        with self._lock:
            self._inventory_cache[claims.user_id] = CachedInventory(
                snapshot=refreshed_snapshot,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=self._settings.marzban_inventory_ttl_seconds),
            )
        return refreshed_snapshot.locations

    def _inventory_for(
        self,
        claims: SessionClaims,
        *,
        force_refresh: bool = False,
    ) -> InventorySnapshot:
        now = datetime.now(UTC)
        external_entries = self._store.list_published_entries()
        subscription = self._store.get_user_subscription(claims.user_id)
        with self._lock:
            cached = self._inventory_cache.get(claims.user_id)
            if not force_refresh and cached is not None and cached.expires_at > now:
                return cached.snapshot

        try:
            self._sync_marzban_access(claims, subscription)
            marzban_snapshot = self._marzban.build_inventory(
                username=claims.marzban_username,
                display_name=self._require_user(claims.user_id).display_name,
            )
            snapshot = self._merge_inventory(marzban_snapshot, external_entries)
        except MarzbanError:
            with self._lock:
                cached = self._inventory_cache.get(claims.user_id)
                if cached is not None:
                    stale_snapshot = stale_copy(cached.snapshot)
                    self._inventory_cache[claims.user_id] = CachedInventory(
                        snapshot=stale_snapshot,
                        expires_at=now + timedelta(seconds=15),
                    )
                    return stale_snapshot
            if external_entries:
                snapshot = self._snapshot_from_external_entries(external_entries, stale=True)
            else:
                raise

        with self._lock:
            self._inventory_cache[claims.user_id] = CachedInventory(
                snapshot=snapshot,
                expires_at=now
                + timedelta(seconds=self._settings.marzban_inventory_ttl_seconds),
            )
        return snapshot

    def _merge_inventory(
        self,
        primary: InventorySnapshot,
        external_entries: list[StoredCatalogEntry],
    ) -> InventorySnapshot:
        if not external_entries:
            return primary

        seen_identities = {
            build_location_identity(
                protocol=location.protocol,
                host=location.host,
                port=location.port,
                network=location.network,
                security_mode=location.security_mode,
            )
            for location in primary.locations
        }
        locations = list(primary.locations)
        config_by_location_id = {
            location_id: dict(configs)
            for location_id, configs in primary.config_by_location_id.items()
        }
        fetched_at = primary.fetched_at
        for entry in external_entries:
            if entry.identity_key in seen_identities:
                continue
            locations.append(
                entry.location.model_copy(
                    update={"recommended": not locations},
                )
            )
            config_by_location_id[entry.location.id] = dict(entry.config_variants)
            fetched_at = max(fetched_at, entry.updated_at)
            seen_identities.add(entry.identity_key)

        return InventorySnapshot(
            locations=locations,
            config_by_location_id=config_by_location_id,
            default_location_id=primary.default_location_id if primary.locations else locations[0].id,
            fetched_at=fetched_at,
            stale=primary.stale,
            server_ip=primary.server_ip or locations[0].host,
            user_status=primary.user_status,
            user_expire=primary.user_expire,
            default_routing_profile=primary.default_routing_profile,
            available_routing_profiles=primary.available_routing_profiles,
        )

    def _snapshot_from_external_entries(
        self,
        entries: list[StoredCatalogEntry],
        *,
        stale: bool,
    ) -> InventorySnapshot:
        if not entries:
            raise MarzbanError("No published catalog entries are available.")

        locations: list[VpnLocation] = []
        config_by_location_id: dict[str, dict[str, str]] = {}
        fetched_at = entries[0].updated_at
        for index, entry in enumerate(entries):
            locations.append(
                entry.location.model_copy(
                    update={
                        "recommended": index == 0,
                        "stale": stale,
                    }
                )
            )
            config_by_location_id[entry.location.id] = dict(entry.config_variants)
            fetched_at = max(fetched_at, entry.updated_at)

        return InventorySnapshot(
            locations=locations,
            config_by_location_id=config_by_location_id,
            default_location_id=locations[0].id,
            fetched_at=fetched_at,
            stale=stale,
            server_ip=locations[0].host,
            user_status="active",
            user_expire=None,
            default_routing_profile=DEFAULT_ROUTING_PROFILE,
            available_routing_profiles=AVAILABLE_ROUTING_PROFILES,
        )

    def _reprobe_snapshot(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        now = datetime.now(UTC)
        unique_endpoints = {
            (location.host, location.port)
            for location in snapshot.locations
        }
        endpoint_cache: dict[tuple[str, int], tuple[bool, int]] = {}
        max_workers = min(64, len(unique_endpoints)) or 1

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._marzban.probe_endpoint, host, port): (host, port)
                for host, port in unique_endpoints
            }
            for future in as_completed(futures):
                endpoint = futures[future]
                try:
                    endpoint_cache[endpoint] = future.result()
                except Exception:
                    endpoint_cache[endpoint] = (False, 0)

        refreshed_locations: list[VpnLocation] = []

        for location in snapshot.locations:
            endpoint = (location.host, location.port)
            online, latency_ms = endpoint_cache[endpoint]
            refreshed_locations.append(
                location.model_copy(
                    update={
                        "online": online,
                        "latency_ms": latency_ms,
                        "last_synced_at": now,
                    }
                )
            )

        return InventorySnapshot(
            locations=refreshed_locations,
            config_by_location_id={
                location_id: dict(configs)
                for location_id, configs in snapshot.config_by_location_id.items()
            },
            default_location_id=snapshot.default_location_id,
            fetched_at=now,
            stale=snapshot.stale,
            server_ip=snapshot.server_ip,
            user_status=snapshot.user_status,
            user_expire=snapshot.user_expire,
            default_routing_profile=snapshot.default_routing_profile,
            available_routing_profiles=snapshot.available_routing_profiles,
        )

    def _ensure_user_allowed(self, claims: SessionClaims) -> None:
        record = self._store.get_user(claims.user_id)
        if record is not None and record.blocked:
            raise UserBlockedError("This account is blocked.")

    def _claims_from_exchange(self, payload: TelegramExchangeRequest) -> SessionClaims:
        telegram_data = payload.telegram_auth_payload
        telegram_user_id = self._telegram_user_id(payload)
        if telegram_user_id is None:
            raise MarzbanError("Telegram account confirmation is required.")
        display_name = (
            telegram_data.get("display_name")
            or telegram_data.get("first_name")
            or payload.device_label
        ).strip()
        telegram_handle = (
            telegram_data.get("username")
            or telegram_data.get("telegram_handle")
            or f"telegram:{telegram_user_id}"
        ).strip()
        if not telegram_handle.startswith("@") and telegram_handle.startswith("telegram:") is False:
            telegram_handle = f"@{telegram_handle}"

        email = telegram_data.get("email")
        linked_user = self._store.get_user_by_telegram_id(telegram_user_id)
        user_id = linked_user.user_id if linked_user is not None else self._stable_identifier(str(telegram_user_id))
        marzban_username = self._marzban_username(user_id)
        return SessionClaims(
            user_id=user_id,
            role=linked_user.role if linked_user is not None else "user",
            telegram_user_id=telegram_user_id,
            telegram_linked=True,
            marzban_username=marzban_username,
            telegram_handle=linked_user.telegram_handle if linked_user is not None else telegram_handle,
            display_name=linked_user.display_name if linked_user is not None else display_name,
            email=linked_user.email if linked_user is not None else email,
            device_id=payload.device_id,
            device_label=payload.device_label,
            platform=payload.platform,
            is_premium=self._subscription_allows_vpn(
                self._store.get_user_subscription(user_id)
            ),
        )

    def _build_session(
        self,
        claims: SessionClaims,
        *,
        link_telegram: bool = False,
        telegram_username: str | None = None,
    ) -> AppSession:
        now = datetime.now(UTC)
        user = self._store.touch_user_activity(
            user_id=claims.user_id,
            telegram_handle=claims.telegram_handle,
            display_name=claims.display_name,
            email=claims.email,
            role=claims.role,
        )
        if link_telegram and claims.telegram_user_id is not None:
            user = self._store.link_telegram_identity(
                user_id=claims.user_id,
                telegram_user_id=claims.telegram_user_id,
                telegram_username=telegram_username,
                telegram_handle=claims.telegram_handle,
            )
        if user.blocked:
            raise UserBlockedError("This account is blocked.")
        self._assert_device_limit(
            user_id=claims.user_id,
            device_id=claims.device_id,
        )
        self._store.record_device(
            user_id=claims.user_id,
            device_id=claims.device_id,
            platform=claims.platform,
            label=claims.device_label,
        )
        device_binding = DeviceBinding(
            device_id=claims.device_id,
            platform=claims.platform,
            label=claims.device_label,
            created_at=now,
            last_seen_at=now,
        )
        session_claims = SessionClaims(
            user_id=claims.user_id,
            role=user.role,
            telegram_user_id=user.telegram_user_id,
            telegram_linked=user.telegram_user_id is not None,
            marzban_username=self._marzban_username(claims.user_id),
            telegram_handle=user.telegram_handle,
            display_name=user.display_name,
            email=user.email,
            device_id=claims.device_id,
            device_label=claims.device_label,
            platform=claims.platform,
            is_premium=self._subscription_allows_vpn(
                self._store.get_user_subscription(claims.user_id)
            ),
        )
        auth_token = issue_app_token(self._settings, session_claims)
        return AppSession(
            user_id=claims.user_id,
            role=user.role,
            telegram_user_id=user.telegram_user_id,
            telegram_linked=user.telegram_user_id is not None,
            telegram_handle=user.telegram_handle,
            display_name=user.display_name,
            email=user.email,
            auth_token=auth_token,
            is_premium=session_claims.is_premium,
            expires_at=now + timedelta(days=self._settings.session_ttl_days),
            device_binding=device_binding,
        )

    def _build_subscription_state(self, claims: SessionClaims) -> SubscriptionState:
        record = self._store.get_user_subscription(claims.user_id)
        plan = None
        if record is not None and record.plan_id is not None:
            try:
                plan = self._store.get_subscription_plan(record.plan_id)
            except KeyError:
                plan = None

        used_traffic = 0
        lifetime_used_traffic = 0
        traffic_limit = None
        try:
            user = self._marzban.get_user(claims.marzban_username)
            used_traffic = int(user.get("used_traffic", 0) or 0)
            lifetime_used_traffic = int(user.get("lifetime_used_traffic", 0) or 0)
            raw_limit = user.get("data_limit")
            if isinstance(raw_limit, int) and raw_limit > 0:
                traffic_limit = raw_limit
        except Exception:
            used_traffic = 0
            lifetime_used_traffic = 0
            traffic_limit = None

        status = self._subscription_status_from_record(record)
        expires_at = record.expires_at if record is not None else None
        days_remaining = 0
        if expires_at is not None:
            days_remaining = max((expires_at - datetime.now(UTC)).days, 0)

        return SubscriptionState(
            status=status,
            current_plan_id=plan.plan_id if plan is not None else None,
            plan_title=plan.title if plan is not None else None,
            expires_at=expires_at,
            auto_renew=False,
            days_remaining=days_remaining,
            can_use_vpn=status == SubscriptionStatus.active,
            access_message=self._subscription_access_message(status),
            used_traffic_bytes=used_traffic,
            lifetime_used_traffic_bytes=lifetime_used_traffic,
            traffic_limit_bytes=traffic_limit,
        )

    def _build_account_summary(
        self,
        *,
        user: UserRecord,
        subscription: SubscriptionState,
        devices: list[DeviceRecord],
        orders: list[SubscriptionOrder],
        fallback_device_id: str,
        fallback_device_label: str,
        claims: SessionClaims,
    ) -> AccountSummary:
        primary_device = devices[0] if devices else None
        current_ip_address = "Недоступен"
        if subscription.can_use_vpn:
            try:
                snapshot = self._inventory_for(claims)
                current_ip_address = snapshot.server_ip or (
                    snapshot.locations[0].host if snapshot.locations else "Недоступен"
                )
            except Exception:
                current_ip_address = "Недоступен"
        account = AccountProfile(
            user_id=user.user_id,
            role=user.role,
            telegram_user_id=user.telegram_user_id,
            telegram_linked=user.telegram_user_id is not None,
            display_name=user.display_name,
            telegram_handle=user.telegram_handle,
            email=user.email,
            current_ip_address=current_ip_address,
            device_label=primary_device.label if primary_device is not None else fallback_device_label,
            device_id=primary_device.device_id if primary_device is not None else fallback_device_id,
        )
        device_bindings = [
            DeviceBinding(
                device_id=device.device_id,
                platform=device.platform,
                label=device.label,
                created_at=device.created_at,
                last_seen_at=device.last_seen_at,
            )
            for device in devices
        ]
        return AccountSummary(
            account=account,
            subscription=subscription,
            devices=device_bindings,
            orders=orders,
        )

    def _clear_inventory_cache(self) -> None:
        with self._lock:
            self._inventory_cache.clear()

    def _clear_inventory_cache_for(self, user_id: str) -> None:
        with self._lock:
            self._inventory_cache.pop(user_id, None)

    def _promo_to_contract(self, promo: PromoCodeRecord) -> AdminPromoCodeItem:
        return AdminPromoCodeItem(
            code=promo.code,
            effect_type=PromoEffectType(promo.effect_type),
            discount_percent=promo.discount_percent,
            duration_days=promo.duration_days,
            max_redemptions=promo.max_redemptions,
            redemption_count=promo.redemption_count,
            is_active=promo.is_active,
            expires_at=promo.expires_at,
            created_by=promo.created_by,
            created_at=promo.created_at,
            updated_at=promo.updated_at,
        )

    def _app_release_to_contract(self, record: AppReleaseRecord) -> AppReleaseInfo:
        return AppReleaseInfo(
            version_name=record.version_name,
            version_code=record.version_code,
            download_url=record.download_url,
            sha256=record.sha256,
            release_notes=record.release_notes,
            published_at=record.published_at,
        )

    def _resolve_promo_auth_user(self, payload: PromoAuthExchangeRequest) -> UserRecord:
        user_id = self._stable_identifier(f"guest:{payload.device_id}")
        user = self._store.upsert_user(
            user_id=user_id,
            telegram_user_id=None,
            telegram_handle=f"guest:{user_id[:8]}",
            display_name=f"Guest {payload.device_id[-4:]}",
            email=None,
            role="user",
        )
        if payload.platform.strip().lower() in {"android", "ios"}:
            user = self._store.set_user_preferred_platform(
                user.user_id,
                preferred_platform=payload.platform.strip().lower(),
            )
        if user.blocked:
            raise UserBlockedError("This account is blocked.")
        return user

    def _require_available_promo(
        self,
        code: str,
        *,
        user_id: str,
        effect_type: PromoEffectType | None = None,
    ) -> PromoCodeRecord:
        normalized_code = code.strip().upper()
        try:
            promo = self._store.get_promo_code(normalized_code)
        except KeyError as exc:
            raise PromoCodeError("Промокод не найден.") from exc
        if not promo.is_active:
            raise PromoCodeError("Промокод отключен.")
        if promo.expires_at is not None and promo.expires_at <= datetime.now(UTC):
            raise PromoCodeError("Срок действия промокода истек.")
        if promo.max_redemptions is not None and promo.redemption_count >= promo.max_redemptions:
            raise PromoCodeError("Лимит использований этого промокода исчерпан.")
        if effect_type is not None and promo.effect_type != effect_type.value:
            raise PromoCodeError("Этот промокод нельзя использовать в выбранном сценарии.")
        if self._store.get_promo_redemption(code=normalized_code, user_id=user_id) is not None:
            raise PromoCodeError("Этот промокод уже использован для вашего аккаунта.")
        return promo

    def _ensure_promo_plan(self, *, duration_days: int) -> SubscriptionPlanRecord:
        return self._store.upsert_subscription_plan(
            plan_id=f"promo_free_{duration_days}",
            title="Промо подписка",
            subtitle=f"Бесплатный доступ на {duration_days} дней",
            price_rub=0,
            duration_days=duration_days,
            sort_order=999,
            is_recommended=False,
            is_hidden=True,
        )

    def _ensure_referral_bonus_plan(self, *, duration_days: int | None = None) -> SubscriptionPlanRecord:
        days = duration_days or self.REFERRAL_BONUS_DAYS
        return self._store.upsert_subscription_plan(
            plan_id=self.REFERRAL_BONUS_PLAN_ID,
            title="Реферальный бонус",
            subtitle=f"Бонус за приглашение на {days} дня",
            price_rub=0,
            duration_days=days,
            sort_order=998,
            is_recommended=False,
            is_hidden=True,
        )

    def _grant_referral_reward(
        self,
        *,
        inviter_user_id: str,
        invited_user_id: str,
        order_id: str | None,
        trigger_type: str,
    ) -> ReferralRewardRecord | None:
        if inviter_user_id == invited_user_id:
            return None
        reward = self._store.create_referral_reward(
            inviter_user_id=inviter_user_id,
            invited_user_id=invited_user_id,
            order_id=order_id,
            trigger_type=trigger_type,
            bonus_days=self.REFERRAL_BONUS_DAYS,
        )
        if reward is None:
            return None
        self.adjust_subscription_days(
            user_id=inviter_user_id,
            delta_days=reward.bonus_days,
            reason=f"referral_{trigger_type}",
            actor="referral_system",
            related_referral_reward_id=reward.reward_id,
        )
        return reward

    def _grant_free_promo_subscription(
        self,
        *,
        user: UserRecord,
        promo: PromoCodeRecord,
        code: str,
    ) -> UserSubscriptionRecord:
        if promo.duration_days is None:
            raise PromoCodeError("Промокод настроен некорректно.")
        promo_plan = self._ensure_promo_plan(duration_days=promo.duration_days)
        self._store.record_promo_redemption(code=code, user_id=user.user_id)
        subscription = self._store.activate_subscription(
            user_id=user.user_id,
            plan_id=promo_plan.plan_id,
            duration_days=promo.duration_days,
            granted_by=f"promo:{code.lower()}",
        )
        claims = self._claims_for_user(user)
        self._sync_marzban_access(claims, subscription)
        self._clear_inventory_cache_for(user.user_id)
        self._push_notifier.notify_subscription_update(
            user_id=user.user_id,
            title="Промокод активирован",
            body=f"Доступ открыт до {subscription.expires_at:%d.%m.%Y}.",
            event_type="subscription_updated",
            reason="promo_granted",
        )
        return subscription

    @staticmethod
    def _discount_amount(price_rub: int, discount_percent: int) -> int:
        return int(round(price_rub * discount_percent / 100))

    def _discounted_amount(self, price_rub: int, discount_percent: int) -> int:
        return max(price_rub - self._discount_amount(price_rub, discount_percent), 0)

    @staticmethod
    def _sha256_for_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file_obj:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _subscription_status_from_record(
        self,
        record: UserSubscriptionRecord | None,
    ) -> SubscriptionStatus:
        if record is None:
            return SubscriptionStatus.inactive
        raw_status = record.status.strip().lower()
        if raw_status == "active" and record.expires_at is not None and record.expires_at <= datetime.now(UTC):
            return SubscriptionStatus.expired
        return {
            "active": SubscriptionStatus.active,
            "revoked": SubscriptionStatus.revoked,
            "expired": SubscriptionStatus.expired,
        }.get(raw_status, SubscriptionStatus.inactive)

    @staticmethod
    def _subscription_access_message(status: SubscriptionStatus) -> str:
        return {
            SubscriptionStatus.active: "Подписка активна.",
            SubscriptionStatus.revoked: "Подписка отключена. Оплатите подписку, чтобы вернуть доступ к серверам.",
            SubscriptionStatus.expired: "Подписка закончилась. Оплатите подписку, чтобы вернуть доступ к серверам.",
            SubscriptionStatus.inactive: "У вас нет активной подписки. Оплатите подписку, чтобы открыть список серверов.",
        }[status]

    def _plan_to_contract(self, plan: SubscriptionPlanRecord) -> Plan:
        if plan.duration_days >= 365:
            billing_period = "за 12 месяцев"
        elif plan.duration_days >= 180:
            billing_period = "за 6 месяцев"
        elif plan.duration_days >= 90:
            billing_period = "за 3 месяца"
        else:
            billing_period = "за 1 месяц"
        return Plan(
            id=plan.plan_id,
            title=plan.title,
            subtitle=plan.subtitle,
            price_label=f"{plan.price_rub} ₽",
            billing_period=billing_period,
            duration_days=plan.duration_days,
            price_rub=plan.price_rub,
            features=self._plan_features(plan.plan_id),
            is_recommended=plan.is_recommended,
        )

    def _plan_features(self, plan_id: str) -> list[str]:
        payment_feature = (
            "Автоактивация сразу после оплаты по ссылке YooKassa"
            if self.online_payments_enabled()
            else "Ручная активация после подтверждения оплаты"
        )
        features = {
            "trial_3d": [
                "Бесплатный доступ на 3 дня",
                "Одноразовый код для входа в приложение",
                "Выдача доступа прямо в Telegram-боте",
            ],
            "monthly_1": [
                "Доступ ко всем серверам",
                payment_feature,
                "Синхронизация кабинета в приложении и боте",
            ],
            "quarterly_3": [
                "Доступ ко всем серверам",
                payment_feature,
                "Удобнее для постоянного использования",
                "Приоритетный рекомендуемый тариф",
            ],
            "semiannual_6": [
                "Доступ ко всем серверам",
                payment_feature,
                "Баланс цены и длительности",
                "Подходит для долгого использования",
            ],
            "yearly_12": [
                "Доступ ко всем серверам",
                payment_feature,
                "Максимальная выгода на долгий срок",
                "Полная синхронизация доступа и трафика",
            ],
        }
        return features.get(plan_id, ["Доступ к VPN-серверам"])

    def _order_to_contract(
        self,
        order: SubscriptionOrderRecord,
        *,
        user: UserRecord | None = None,
    ) -> SubscriptionOrder:
        plan = self._store.get_subscription_plan(order.plan_id)
        return SubscriptionOrder(
            order_id=order.order_id,
            plan_id=order.plan_id,
            plan_title=plan.title,
            amount_rub=order.amount_rub,
            original_amount_rub=order.original_amount_rub,
            discount_amount_rub=order.discount_amount_rub,
            promo_code=order.promo_code,
            duration_days=order.duration_days,
            status=SubscriptionOrderStatus(order.status),
            payment_instructions=self._payment_instructions(user=user),
            created_at=order.created_at,
            paid_marked_at=order.paid_marked_at,
            confirmed_at=order.confirmed_at,
            confirmed_by=order.confirmed_by,
            status_message=self._order_status_message(order.status),
            payment_provider=order.payment_provider,
            payment_url=order.payment_confirmation_url,
            provider_payment_id=order.provider_payment_id,
            provider_payment_status=order.provider_payment_status,
        )

    def _order_status_message(self, status: str) -> str:
        if self.online_payments_enabled():
            return {
                "pending_payment": "Ссылка на оплату создана. После оплаты подписка включится автоматически.",
                "waiting_confirmation": "Платеж получен и ожидает финальной проверки.",
                "confirmed": "Оплата подтверждена, подписка уже активна.",
                "rejected": "Заказ отклонен.",
                "canceled": "Заказ отменен.",
            }.get(status, "Статус заказа обновлен.")
        return {
            "pending_payment": "Ожидает оплаты.",
            "waiting_confirmation": "Ожидает подтверждения администратором.",
            "confirmed": "Оплата подтверждена.",
            "rejected": "Заказ отклонен.",
            "canceled": "Заказ отменен.",
        }.get(status, "Статус заказа обновлен.")

    def _payment_instructions(self, *, user: UserRecord | None) -> str:
        if not self.online_payments_enabled():
            return self._settings.payment_instructions

        bot_username = self._settings.telegram_bot_username.strip()
        bot_label = f"@{bot_username}" if bot_username else "вашем Telegram-боте"
        if user is not None and user.telegram_user_id is None:
            return (
                f"Оплата проходит в {bot_label}. Сначала привяжите Telegram к аккаунту, "
                "затем создайте заказ заново."
            )
        return (
            f"Ссылка на оплату отправлена в {bot_label}. Оплатите заказ на странице YooKassa, "
            "и подписка включится автоматически."
        )

    def _require_user(self, user_id: str) -> UserRecord:
        user = self._store.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        return user

    def _claims_for_user(self, user: UserRecord) -> SessionClaims:
        devices = self._store.list_devices(user.user_id)
        primary_device = devices[0] if devices else None
        resolved_platform = self.get_user_delivery_platform(user.user_id)
        return SessionClaims(
            user_id=user.user_id,
            role=user.role,
            telegram_user_id=user.telegram_user_id,
            telegram_linked=user.telegram_user_id is not None,
            marzban_username=self._marzban_username(user.user_id),
            telegram_handle=user.telegram_handle,
            display_name=user.display_name,
            email=user.email,
            device_id=primary_device.device_id if primary_device is not None else f"telegram-{resolved_platform}",
            device_label=primary_device.label if primary_device is not None else self._telegram_device_label(resolved_platform),
            platform=primary_device.platform if primary_device is not None else resolved_platform,
            is_premium=self._subscription_allows_vpn(
                self._store.get_user_subscription(user.user_id)
            ),
        )

    def _claims_for_device(
        self,
        user: UserRecord,
        *,
        device_id: str,
        device_label: str,
        platform: str,
    ) -> SessionClaims:
        return SessionClaims(
            user_id=user.user_id,
            role=user.role,
            telegram_user_id=user.telegram_user_id,
            telegram_linked=user.telegram_user_id is not None,
            marzban_username=self._marzban_username(user.user_id),
            telegram_handle=user.telegram_handle,
            display_name=user.display_name,
            email=user.email,
            device_id=device_id,
            device_label=device_label,
            platform=platform,
            is_premium=self._subscription_allows_vpn(
                self._store.get_user_subscription(user.user_id)
            ),
        )

    def _placeholder_subscription_export(
        self,
        *,
        user: UserRecord,
        subscription: SubscriptionState,
        reason: str,
    ) -> PublicSubscriptionExport:
        return PublicSubscriptionExport(
            body="\n".join(self._placeholder_subscription_lines(reason)) + "\n",
            title=self._subscription_profile_title(user),
            user=user,
            subscription=subscription,
        )

    def _placeholder_subscription_lines(self, reason: str) -> list[str]:
        bot_username = self._settings.telegram_bot_username.strip()
        bot_label = f"@{bot_username}" if bot_username else self._service_name()
        labels = [
            "🔒 Подписка истекла",
            "💳 Обновите подписку",
            f"🤖 {bot_label}",
        ]
        return [
            self._placeholder_vless_uri(index=index, label=label)
            for index, label in enumerate(labels, start=1)
        ]

    @staticmethod
    def _placeholder_vless_uri(*, index: int, label: str) -> str:
        user_id = f"00000000-0000-4000-8000-{index:012d}"
        fragment = quote(label, safe="")
        return (
            f"vless://{user_id}@127.0.0.1:9"
            f"?type=tcp&security=none&encryption=none#{fragment}"
        )

    def _subscription_share_lines(
        self,
        user: UserRecord,
        subscription: SubscriptionState,
    ) -> list[str]:
        published_entries = self._store.list_published_entries()
        lines: list[str] = []
        
        # 1. Select and build auto-connect profiles
        wifi_candidates = [
            e for e in published_entries 
            if e.share_uri and e.location and getattr(e.location, "online", False)
            and getattr(e.location, "latency_ms", 9999) > 0
        ]
        wifi_candidates.sort(key=lambda x: x.location.latency_ms)
        best_wifi = wifi_candidates[0] if wifi_candidates else None

        lte_candidates = [
            e for e in published_entries 
            if e.share_uri and e.location and getattr(e.location, "online", False)
            and getattr(e.location, "protocol", "").lower() in ("vless", "shadowsocks", "ss")
            and getattr(e.location, "latency_ms", 9999) > 0
        ]
        
        def lte_priority(e):
            proto = getattr(e.location, "protocol", "").lower()
            sec = getattr(e.location, "security_mode", "").lower()
            net = getattr(e.location, "network", "").lower()
            if proto == "vless" and sec == "reality":
                if net == "tcp":
                    return 1
                return 2
            if proto in ("shadowsocks", "ss"):
                return 3
            if proto == "vless":
                return 4
            return 5

        if lte_candidates:
            lte_candidates.sort(key=lambda x: (lte_priority(x), x.location.latency_ms))
            best_lte = lte_candidates[0]
        else:
            best_lte = None

        if best_wifi:
            wifi_name = f"📶 🇪🇺 Автоподключение Wi-Fi"
            lines.append(self._with_uri_fragment(best_wifi.share_uri, wifi_name))

        if best_lte:
            lte_name = f"📱 🇪🇺 Автоподключение LTE"
            lines.append(self._with_uri_fragment(best_lte.share_uri, lte_name))

        # 2. Append regular profiles
        seen_identities: set[str] = set()
        for index, entry in enumerate(published_entries, start=1):
            if not entry.share_uri:
                continue
            if entry.identity_key in seen_identities:
                continue
            pretty_name = self._subscription_server_name(entry, index=index)
            lines.append(self._with_uri_fragment(entry.share_uri, pretty_name))
            seen_identities.add(entry.identity_key)

        if lines:
            return lines

        try:
            get_subscription_text = getattr(self._marzban, "get_subscription_text")
            subscription_text = get_subscription_text(
                username=self._marzban_username(user.user_id),
                display_name=user.display_name,
            )
            proxied_lines = [
                line.strip()
                for line in subscription_text.splitlines()
                if line.strip()
            ]
            if proxied_lines:
                return proxied_lines
        except Exception:
            pass

        try:
            return [self.get_user_subscription_url(user.user_id)]
        except Exception:
            return []

    def _subscription_profile_title(self, user: UserRecord) -> str:
        return self._service_name()

    @staticmethod
    def _subscription_server_name(entry: StoredCatalogEntry, *, index: int) -> str:
        if entry.display_override and entry.display_override.strip():
            return entry.display_override.strip()
        location = entry.location
        flag = location.flag_emoji.strip() or "🌐"
        place_parts = [
            value.strip()
            for value in (location.country, location.city)
            if value and value.strip() and value.strip().lower() != "global"
        ]
        place = " · ".join(dict.fromkeys(place_parts))
        name = entry.display_override or location.display_name or place or location.host
        protocol = location.protocol.upper()
        if place and place.lower() not in name.lower():
            return f"{flag} {place} · {name} · {protocol}"
        return f"{flag} {name} · {protocol}"

    @staticmethod
    def _with_uri_fragment(uri: str, fragment: str) -> str:
        normalized = uri.strip()
        if not normalized:
            return normalized
        try:
            parts = urlsplit(normalized)
            return urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    parts.query,
                    quote(fragment, safe=""),
                )
            )
        except Exception:
            separator = "&" if "#" in normalized else "#"
            return f"{normalized}{separator}{quote(fragment, safe='')}"

    def _public_base_url(self, override: str | None = None) -> str:
        candidates = [
            override,
            self._settings.public_base_url,
            self._settings.admin_web_url,
            self._settings.telegram_android_download_url,
            "https://example.com",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            value = str(candidate).strip()
            if not value:
                continue
            parsed = urlsplit(value)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return "https://example.com"

    def _public_logo_url(self, request_base_url: str | None = None) -> str:
        return f"{self._public_base_url(request_base_url)}/assets/logo.png"

    def _service_name(self) -> str:
        name = self._settings.vpn_service_name.strip()
        return name[:25] if name else "MeFast VPN"

    def _ensure_happ_install_code(self, user: UserRecord) -> str | None:
        return None

    @staticmethod
    def _happ_install_code(user_id: str) -> str:
        return hashlib.sha256(
            f"happ-install:{user_id}".encode("utf-8")
        ).hexdigest()[:12]

    def _with_happ_install_fragment(self, subscription_url: str, *, install_code: str) -> str:
        title = quote(self._service_name(), safe="")
        code = quote(install_code, safe="")
        parts = urlsplit(subscription_url)
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                parts.query,
                f"{title}?installid={code}",
            )
        )

    def _encrypt_happ_link(self, subscription_url: str) -> str:
        try:
            response = httpx.post(
                self._settings.happ_crypto_api_url,
                json={"url": subscription_url},
                timeout=self._settings.external_source_timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MarzbanError("Не удалось подготовить deep link для Happ.") from exc

        raw_text = response.text.strip()
        try:
            payload = response.json()
        except ValueError:
            payload = None

        candidates: list[str] = []
        if isinstance(payload, dict):
            for key in ("encrypted_link", "url", "link", "result", "deeplink", "deep_link"):
                value = payload.get(key)
                if isinstance(value, str):
                    candidates.append(value.strip())
            for value in payload.values():
                if isinstance(value, str):
                    candidates.append(value.strip())
        candidates.append(raw_text)
        for candidate in candidates:
            if candidate.lower().startswith("happ://"):
                return candidate
        raise MarzbanError("Happ crypto API did not return a deep link.")

    @staticmethod
    def _normalize_delivery_platform(platform: str) -> str:
        normalized = platform.strip().lower()
        aliases = {
            "android": "android",
            "apk": "android",
            "ios": "ios",
            "iphone": "ios",
            "ipad": "ios",
            "subscription": "subscription",
            "telegram": "subscription",
            "bot": "subscription",
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError("Поддерживается Telegram subscription-ссылка.")

    def _assert_device_limit(self, *, user_id: str, device_id: str) -> None:
        return

    @staticmethod
    def _is_counted_device_id(device_id: str) -> bool:
        normalized = device_id.strip().lower()
        return bool(normalized) and not normalized.startswith("telegram-onboarding-")

    @staticmethod
    def _telegram_device_label(platform: str) -> str:
        if platform == "subscription":
            return "Telegram Bot"
        if platform == "ios":
            return "Telegram iPhone/iPad"
        return "Telegram Android"

    def _subscription_allows_vpn(self, record: UserSubscriptionRecord | None) -> bool:
        return self._subscription_status_from_record(record) == SubscriptionStatus.active

    def _has_vpn_access(self, claims: SessionClaims) -> bool:
        return self._subscription_allows_vpn(self._store.get_user_subscription(claims.user_id))

    def _sync_marzban_access(
        self,
        claims: SessionClaims,
        subscription: UserSubscriptionRecord | None,
    ) -> None:
        if not hasattr(self._marzban, "sync_user_access"):
            return
        self._marzban.sync_user_access(
            username=claims.marzban_username,
            display_name=self._require_user(claims.user_id).display_name,
            is_active=self._subscription_allows_vpn(subscription),
            expires_at=subscription.expires_at if subscription is not None else None,
        )

    def _ensure_activation_not_throttled(self, device_id: str) -> None:
        now = datetime.now(UTC)
        with self._lock:
            attempts = [
                ts
                for ts in self._activation_failures.get(device_id, [])
                if ts + self.ACTIVATION_CODE_FAILURE_WINDOW > now
            ]
            self._activation_failures[device_id] = attempts
            if len(attempts) >= self.ACTIVATION_CODE_MAX_FAILURES:
                raise ActivationCodeThrottledError(
                    "Слишком много неверных попыток. Подождите 10 минут и запросите новый код."
                )

    def _register_activation_failure(self, device_id: str) -> None:
        now = datetime.now(UTC)
        with self._lock:
            attempts = [
                ts
                for ts in self._activation_failures.get(device_id, [])
                if ts + self.ACTIVATION_CODE_FAILURE_WINDOW > now
            ]
            attempts.append(now)
            self._activation_failures[device_id] = attempts

    def _clear_activation_failures(self, device_id: str) -> None:
        with self._lock:
            self._activation_failures.pop(device_id, None)

    @staticmethod
    def _stable_identifier(value: str) -> str:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
        return f"user_{digest}"

    def _marzban_username(self, seed: str) -> str:
        prefix = self._settings.marzban_user_prefix.strip().lower() or "speedy"
        normalized = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", seed).strip("._-").lower()
        candidate = f"{prefix}_{normalized}" if normalized else prefix
        if 3 <= len(candidate) <= 32:
            return candidate

        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
        return f"{prefix}_{digest}"[:32]

    @staticmethod
    def _telegram_user_id(payload: TelegramExchangeRequest) -> int | None:
        raw = payload.telegram_auth_payload.get("id")
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        return None

    @staticmethod
    def _telegram_username(payload: TelegramExchangeRequest) -> str | None:
        raw = payload.telegram_auth_payload.get("username")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None
