import logging
import re

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SECRET = "speedy-dev-secret-change-me-32chars"
_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPEEDY_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    app_name: str = "MeFast VPN Facade"
    vpn_service_name: str = "MeFast VPN"
    api_prefix: str = "/v1"
    environment: str = "development"
    app_auth_secret: str = _DEFAULT_SECRET
    allow_guest_auth: bool = True
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:8081"]

    @model_validator(mode="after")
    def _validate_secret(self) -> "Settings":
        if self.environment != "development" and self.app_auth_secret == _DEFAULT_SECRET:
            raise ValueError(
                "SPEEDY_APP_AUTH_SECRET must be set to a strong random value "
                "in non-development environments. Never use the default secret."
            )
        if self.app_auth_secret == _DEFAULT_SECRET:
            _logger.warning(
                "Using default dev secret — DO NOT deploy to production."
            )
        return self
    support_url: str = "https://t.me/speedyvpn_support"
    legal_user_agreement_url: str = "https://telegra.ph/Polzovatelskoe-soglashenie-04-01-19"
    legal_privacy_policy_url: str = "https://telegra.ph/Politika-konfidencialnosti-04-01-26"
    public_base_url: str = ""
    admin_web_url: str = ""
    admin_bot_password: str = ""
    admin_web_password: str = ""
    payment_instructions: str = (
        "Оплатите подписку переводом на указанные реквизиты и затем нажмите "
        "кнопку «Я оплатил»."
    )
    plan_monthly_price_rub: int = 149
    plan_quarterly_price_rub: int = 349
    plan_semiannual_price_rub: int = 659
    plan_yearly_price_rub: int = 1199
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_auth_timeout_seconds: int = 180
    telegram_payments_provider_token: str = ""
    telegram_payments_require_email: bool = True
    telegram_payments_send_email_to_provider: bool = True
    telegram_payments_receipt_enabled: bool = False
    telegram_payments_receipt_vat_code: int = 1
    telegram_payments_receipt_subject: str = "service"
    telegram_payments_receipt_mode: str = "full_payment"
    telegram_payments_receipt_tax_system_code: int | None = None
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_api_base_url: str = "https://api.yookassa.ru/v3"
    yookassa_timeout_seconds: float = 15.0
    yookassa_pending_check_interval_seconds: float = 60.0
    telegram_android_apk_path: str = ""
    telegram_android_apk_file_id: str = ""
    telegram_android_apk_caption: str = "MeFast VPN для Android"
    telegram_android_apk_filename: str = "speedy-vpn-android.apk"
    telegram_android_download_url: str = ""
    service_logo_path: str = "logo.png"
    happ_download_url: str = "https://www.happ.su/main"
    happ_install_device_limit: int = 0
    happ_api_base_url: str = "https://api.happ-proxy.com"
    happ_provider_code: str = ""
    happ_auth_key: str = ""
    happ_crypto_links_enabled: bool = True
    happ_crypto_api_url: str = "https://crypto.happ.su/api-v2.php"
    max_devices_per_subscription: int = 0
    session_ttl_days: int = 30
    pally_webhook_secret: str = ""
    xui_base_url: str = "https://xui.example.invalid"
    xui_username: str = ""
    xui_password: str = ""
    marzban_base_url: str = "https://127.0.0.1:8000"
    marzban_admin_token: str = ""
    marzban_admin_username: str = ""
    marzban_admin_password: str = ""
    marzban_verify_ssl: bool = False
    marzban_timeout_seconds: float = 15.0
    marzban_inventory_ttl_seconds: int = 60
    marzban_subscription_client_type: str = "v2ray-json"
    marzban_user_prefix: str = "speedy"
    storage_path: str = "./speedy_vpn_data.sqlite3"
    telegram_bot_poll_interval_seconds: float = 3.0
    external_source_timeout_seconds: float = 20.0
    catalog_allowed_hosts: str = ""
    fcm_server_key: str = ""

    @property
    def catalog_allowed_host_set(self) -> frozenset[str]:
        """Hosts allowed in the server catalog; empty means unrestricted."""
        raw = self.catalog_allowed_hosts.strip()
        if not raw:
            return frozenset()
        return frozenset(
            part.strip().lower()
            for part in re.split(r"[,\s]+", raw)
            if part.strip()
        )


settings = Settings()
