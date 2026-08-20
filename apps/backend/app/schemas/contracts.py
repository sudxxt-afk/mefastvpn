from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator


class SubscriptionStatus(str, Enum):
    active = "active"
    inactive = "inactive"
    revoked = "revoked"
    expired = "expired"


class PromoEffectType(str, Enum):
    discount_percent = "discount_percent"
    free_subscription = "free_subscription"


class DeviceBinding(BaseModel):
    device_id: str
    platform: str
    label: str
    created_at: datetime
    last_seen_at: datetime


class AppSession(BaseModel):
    user_id: str
    role: str = "user"
    telegram_user_id: int | None = None
    telegram_linked: bool = False
    telegram_handle: str
    display_name: str
    email: EmailStr | None = None
    auth_token: str
    is_premium: bool = False
    expires_at: datetime | None = None
    device_binding: DeviceBinding


class FeatureSettings(BaseModel):
    wifi_protection: bool = True
    domain_fronting: bool = False
    dedicated_ip: bool = False
    random_port: bool = False
    content_blocker: bool = True


class VpnLocation(BaseModel):
    id: str
    display_name: str
    country: str
    city: str
    flag_emoji: str = "🌐"
    host: str
    port: int
    protocol: str
    network: str
    security_mode: str
    node_name: str
    online: bool = True
    stale: bool = False
    latency_ms: int = 0
    recommended: bool = False
    premium_only: bool = False
    last_synced_at: datetime


class SubscriptionState(BaseModel):
    status: SubscriptionStatus
    current_plan_id: str | None = None
    plan_title: str | None = None
    expires_at: datetime | None = None
    auto_renew: bool = False
    days_remaining: int = 0
    can_use_vpn: bool = False
    access_message: str | None = None
    used_traffic_bytes: int = 0
    lifetime_used_traffic_bytes: int = 0
    traffic_limit_bytes: int | None = None


class Plan(BaseModel):
    id: str
    title: str
    subtitle: str
    price_label: str
    billing_period: str
    duration_days: int
    price_rub: int
    features: list[str]
    is_recommended: bool = False


class AccountProfile(BaseModel):
    user_id: str
    role: str = "user"
    telegram_user_id: int | None = None
    telegram_linked: bool = False
    display_name: str
    telegram_handle: str
    email: EmailStr | None = None
    current_ip_address: str
    device_label: str
    device_id: str


class AccountSummary(BaseModel):
    account: AccountProfile
    subscription: SubscriptionState
    devices: list[DeviceBinding]
    orders: list["SubscriptionOrder"]


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=100)
    email: EmailStr | None = None


class BootstrapResponse(BaseModel):
    session: AppSession
    account: AccountProfile
    subscription: SubscriptionState
    devices: list[DeviceBinding]
    orders: list["SubscriptionOrder"]
    locations: list[VpnLocation]
    selected_location_id: str
    feature_settings: FeatureSettings
    inventory_stale: bool = False
    android_update: AppReleaseInfo | None = None


class AdminDashboardStats(BaseModel):
    users_total: int
    devices_total: int
    active_subscriptions: int
    expired_or_revoked_subscriptions: int
    pending_payments: int
    awaiting_confirmation: int
    confirmed_revenue_rub: int


class AdminUserListItem(BaseModel):
    user_id: str
    role: str = "user"
    blocked: bool = False
    telegram_user_id: int | None = None
    telegram_linked: bool = False
    telegram_handle: str
    telegram_username: str | None = None
    display_name: str
    email: EmailStr | None = None
    current_plan_id: str | None = None
    plan_title: str | None = None
    subscription_status: SubscriptionStatus = SubscriptionStatus.inactive
    last_seen_at: datetime | None = None
    telegram_linked_at: datetime | None = None


class AdminUserListResponse(BaseModel):
    users: list[AdminUserListItem]
    total_count: int


class AdminUserDetailResponse(BaseModel):
    user: AdminUserListItem
    summary: AccountSummary


class AdminOrdersResponse(BaseModel):
    orders: list["SubscriptionOrder"]
    total_count: int


class AdminCatalogStatus(BaseModel):
    marzban_locations: int
    external_sources: int
    published_entries: int
    hidden_entries: int
    disabled_entries: int
    last_synced_at: datetime | None = None


class TelegramLinkConfirmRequest(BaseModel):
    flow_id: str


class SubscriptionGrantRequest(BaseModel):
    plan_id: str


class SubscriptionRevokeRequest(BaseModel):
    reason: str = "admin_revoked"


class TelegramStartRequest(BaseModel):
    device_id: str
    device_label: str
    platform: str


class TelegramAuthFlowResponse(BaseModel):
    flow_id: str
    auth_url: HttpUrl
    one_time_code: str
    bot_username: str
    instructions: str
    status_check_after_ms: int = 2000
    expires_at: datetime


class TelegramAuthStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    expired = "expired"


class TelegramAuthStatusResponse(BaseModel):
    flow_id: str
    status: TelegramAuthStatus
    session: AppSession | None = None
    verified_at: datetime | None = None
    expires_at: datetime


class TelegramExchangeRequest(BaseModel):
    telegram_auth_payload: dict[str, str] = Field(default_factory=dict)
    device_id: str = Field(min_length=1, max_length=256)
    device_label: str = Field(min_length=1, max_length=256)
    platform: str = Field(min_length=1, max_length=64)

    @field_validator("telegram_auth_payload")
    @classmethod
    def _require_telegram_id(cls, v: dict[str, str]) -> dict[str, str]:
        if not v or not v.get("id", "").strip():
            raise ValueError(
                "telegram_auth_payload must contain a valid 'id' field."
            )
        return v


class ActivationCodeExchangeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=4)
    device_id: str = Field(min_length=1, max_length=256)
    device_label: str = Field(min_length=1, max_length=256)
    platform: str = Field(min_length=1, max_length=64)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) != 4 or not normalized.isdigit():
            raise ValueError("Activation code must contain exactly 4 digits.")
        return normalized


class DeviceBindRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=1, max_length=256)
    device_label: str = Field(min_length=1, max_length=256)
    platform: str = Field(min_length=1, max_length=64)


class PushTokenRegisterRequest(BaseModel):
    device_id: str
    platform: str
    push_token: str
    provider: str = "fcm"


class LocationSelectRequest(BaseModel):
    location_id: str


class SubscriptionOrderStatus(str, Enum):
    pending_payment = "pending_payment"
    waiting_confirmation = "waiting_confirmation"
    confirmed = "confirmed"
    canceled = "canceled"
    rejected = "rejected"


class SubscriptionOrder(BaseModel):
    order_id: str
    plan_id: str
    plan_title: str
    amount_rub: int
    original_amount_rub: int | None = None
    discount_amount_rub: int | None = None
    promo_code: str | None = None
    duration_days: int
    status: SubscriptionOrderStatus
    payment_instructions: str
    created_at: datetime
    paid_marked_at: datetime | None = None
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None
    status_message: str | None = None
    payment_provider: str | None = None
    payment_url: str | None = None
    provider_payment_id: str | None = None
    provider_payment_status: str | None = None


class SubscriptionOrderCreateRequest(BaseModel):
    plan_id: str
    promo_code: str | None = None


class SubscriptionOrderMarkPaidRequest(BaseModel):
    order_id: str


class VpnConfigResponse(BaseModel):
    server_id: str
    engine: str
    protocol: str
    config_json: str
    updated_at: datetime
    routing_profile: str = "balanced"
    available_routing_profiles: list[str] = Field(default_factory=list)


class AppReleaseInfo(BaseModel):
    version_name: str
    version_code: int
    download_url: str
    sha256: str
    release_notes: str = ""
    published_at: datetime


class PromoGrantedSubscription(BaseModel):
    plan_id: str
    plan_title: str
    duration_days: int
    expires_at: datetime | None = None


class PromoApplyRequest(BaseModel):
    code: str = Field(min_length=3, max_length=64)


class PromoAuthExchangeRequest(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    device_id: str = Field(min_length=1, max_length=256)
    device_label: str = Field(min_length=1, max_length=256)
    platform: str = Field(min_length=1, max_length=64)


class PromoApplyResult(BaseModel):
    effect_type: PromoEffectType
    message: str
    promo_code: str
    discount_percent: int | None = None
    discounted_prices_by_plan: dict[str, int] = Field(default_factory=dict)
    granted_subscription: PromoGrantedSubscription | None = None


class AdminPromoCodeItem(BaseModel):
    code: str
    effect_type: PromoEffectType
    discount_percent: int | None = None
    duration_days: int | None = None
    max_redemptions: int | None = None
    redemption_count: int = 0
    is_active: bool = True
    expires_at: datetime | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class AdminPromosResponse(BaseModel):
    promos: list[AdminPromoCodeItem]


class PromoCreateRequest(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    effect_type: PromoEffectType
    discount_percent: int | None = Field(default=None, ge=1, le=99)
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    max_redemptions: int | None = Field(default=None, ge=1, le=1_000_000)
    expires_at: datetime | None = None

    @field_validator("code")
    @classmethod
    def _normalize_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("Promo code is required.")
        return normalized

    @field_validator("duration_days")
    @classmethod
    def _validate_duration_days(cls, value: int | None, info) -> int | None:
        if info.data.get("effect_type") == PromoEffectType.free_subscription and value is None:
            raise ValueError("Free subscription promos must define duration_days.")
        return value

    @field_validator("discount_percent")
    @classmethod
    def _validate_discount_percent(cls, value: int | None, info) -> int | None:
        if info.data.get("effect_type") == PromoEffectType.discount_percent and value is None:
            raise ValueError("Discount promos must define discount_percent.")
        return value


class AppReleasePublishRequest(BaseModel):
    version_name: str = Field(min_length=1, max_length=64)
    version_code: int = Field(ge=1, le=2_147_483_647)
    release_notes: str = Field(default="", max_length=4000)


# ──────────────────────────────────────────────
#  Admin / Debug schemas (Swagger testing)
# ──────────────────────────────────────────────

class MarzbanUserInfo(BaseModel):
    username: str
    status: str
    data_limit: int | None = None
    data_limit_reset_strategy: str | None = None
    expire: int | None = None
    used_traffic: int = 0
    lifetime_used_traffic: int = 0
    created_at: str | None = None
    online_at: str | None = None
    sub_updated_at: str | None = None
    subscription_url: str | None = None
    links: list[str] = Field(default_factory=list)
    note: str | None = None
    on_hold_expire_duration: int | None = None
    on_hold_timeout: str | None = None
    proxies: dict = Field(default_factory=dict)
    inbounds: dict = Field(default_factory=dict)


class MarzbanInboundInfo(BaseModel):
    tag: str
    protocol: str
    port: int
    network: str = "tcp"
    security: str = "none"
    settings_preview: str = ""


class SystemStatsResponse(BaseModel):
    version: str = ""
    mem_total: int = 0
    mem_used: int = 0
    cpu_cores: int = 0
    cpu_usage: float = 0.0
    total_user: int = 0
    users_active: int = 0
    incoming_bandwidth: int = 0
    outgoing_bandwidth: int = 0
    incoming_bandwidth_speed: int = 0
    outgoing_bandwidth_speed: int = 0
    raw: dict = Field(default_factory=dict)


class CreateMarzbanUserRequest(BaseModel):
    username: str
    note: str = "created-via-swagger"

    model_config = {"json_schema_extra": {
        "examples": [{"username": "test_user_swagger", "note": "Created via Swagger UI"}]
    }}


class QuickTestResponse(BaseModel):
    step_auth: str
    user_id: str
    auth_token: str
    step_bootstrap: str
    locations_count: int
    locations: list[VpnLocation]
    step_vpn_config: str
    vpn_config_server_id: str
    vpn_config_protocol: str
    vpn_config_engine: str
    step_profile: str
    profile_display_name: str
    profile_ip: str
    step_subscription: str
    subscription_status: str
    all_ok: bool


class InventoryRefreshResponse(BaseModel):
    locations: list[VpnLocation]
    config_ids: list[str]
    default_location_id: str
    server_ip: str | None = None
    user_status: str
    stale: bool = False
    fetched_at: datetime
