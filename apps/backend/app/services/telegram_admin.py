from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from threading import Lock

from app.config import Settings
from app.schemas.contracts import (
    Plan,
    PromoApplyResult,
    PromoEffectType,
    SubscriptionOrder,
    SubscriptionState,
    SubscriptionStatus,
)
from app.services.facade import (
    PromoCodeError,
    SpeedyFacadeService,
    SubscriptionRequiredError,
    TrialAlreadyActivatedError,
)
from app.services.marzban import MarzbanError


@dataclass(slots=True)
class PendingBotAction:
    action: str
    created_at: datetime
    params: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BotResponse:
    text: str
    reply_markup: dict[str, object] | None = None
    edit_message: bool = False
    callback_notice: str | None = None
    photo: str | None = None
    parse_mode: str | None = None


class TelegramAdminService:
    def __init__(self, settings: Settings, facade: SpeedyFacadeService) -> None:
        self._settings = settings
        self._facade = facade
        self._pending_actions: dict[int, PendingBotAction] = {}
        self._active_promo_codes: dict[int, str] = {}
        self._pending_start_referrals: dict[int, str] = {}
        self._lock = Lock()
        self._bot_gateway: object | None = None  # set later via set_bot_gateway
        self._last_payment_check_at: datetime | None = None

    def set_bot_gateway(self, gateway: object) -> None:
        """Called by TelegramAuthService after the gateway is created."""
        self._bot_gateway = gateway

    @property
    def ui_store(self):
        """Persistent storage for per-chat UI card sessions."""
        return self._facade.store

    # ──────────────────────────────────────────────
    #  Proactive Telegram notifications
    # ──────────────────────────────────────────────

    def notify_user_new_order(self, *, telegram_user_id: int | None, order: "SubscriptionOrder") -> None:
        """Send order details or invoice to user immediately after order creation."""
        if self._bot_gateway is None or telegram_user_id is None:
            return
        if self._payments_enabled():
            try:
                self._send_order_payment_link(telegram_user_id=telegram_user_id, order=order)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            text = (
                f"\U0001f6d2 Заказ создан\n\n"
                f"\U0001f4e6 Тариф: {order.plan_title}\n"
                f"\U0001f4b0 Сумма: {order.amount_rub} \u20bd\n\n"
                f"\U0001f4cb Инструкция по оплате:\n{order.payment_instructions}\n\n"
                f"После перевода нажмите \u00ab\u2705 Я оплатил\u00bb."
            )
            markup = self._keyboard([
                [
                    self._callback_button("\u2705 Я оплатил", f"order:paid:{order.order_id}"),
                    self._callback_button("\u274c Отменить", f"order:cancel:{order.order_id}"),
                ],
            ])
            self._bot_gateway.send_message(  # type: ignore[union-attr]
                chat_id=telegram_user_id,
                text=text,
                reply_markup=markup,
            )
        except Exception:  # noqa: BLE001
            pass

    def validate_pre_checkout_query(
        self,
        *,
        telegram_user_id: int,
        invoice_payload: str,
        total_amount: int,
        currency: str,
    ) -> SubscriptionOrder:
        return self._facade.validate_telegram_payment(
            telegram_user_id=telegram_user_id,
            order_id=invoice_payload,
            total_amount=total_amount,
            currency=currency,
        )

    def complete_successful_payment(
        self,
        *,
        telegram_user_id: int,
        payment_payload: dict[str, object],
    ) -> SubscriptionOrder:
        invoice_payload = payment_payload.get("invoice_payload")
        if not isinstance(invoice_payload, str) or not invoice_payload.strip():
            raise ValueError("В уведомлении об оплате нет номера заказа.")

        order = self._facade.complete_telegram_order(
            telegram_user_id=telegram_user_id,
            order_id=invoice_payload.strip(),
        )
        self.notify_user_order_approved(order_id=order.order_id, order=order)
        self.deliver_order_access(order_id=order.order_id)
        return order

    def notify_admins_order_paid(self, *, order: "SubscriptionOrder", user: object) -> None:
        """Send payment notification to all admins in Telegram."""
        if self._bot_gateway is None:
            return
        user_display = getattr(user, "display_name", "Неизвестно")
        user_handle = getattr(user, "telegram_handle", "")
        text = (
            f"💳 Пользователь оплатил заказ!\n"
            f"Пользователь: {user_display} {user_handle}\n"
            f"Тариф: {order.plan_title}\n"
            f"Сумма: {order.amount_rub} RUB\n"
            f"Заказ: {order.order_id}"
        )
        markup = self._keyboard([
            [
                self._callback_button("Подтвердить", f"admin:approve:{order.order_id}"),
                self._callback_button("Отклонить", f"admin:reject:{order.order_id}"),
            ],
        ])
        for admin_tg_id in self._get_admin_telegram_ids():
            try:
                self._bot_gateway.send_message(  # type: ignore[union-attr]
                    chat_id=admin_tg_id,
                    text=text,
                    reply_markup=markup,
                )
            except Exception:  # noqa: BLE001
                pass

    def notify_user_order_approved(self, *, order_id: str, order: "SubscriptionOrder") -> None:
        """Send 'thank you' message to user after admin approves."""
        if self._bot_gateway is None:
            return
        try:
            rec = self._facade.store.get_subscription_order(order_id)
            user = self._facade.store.get_user(rec.user_id)
            if user is None or user.telegram_user_id is None:
                return
            next_step = "Ниже отправлю кнопку для подключения VPN в Happ."
            self._bot_gateway.send_message(  # type: ignore[union-attr]
                chat_id=user.telegram_user_id,
                text=(
                    f"✅ Спасибо за покупку!\n"
                    f"Тариф «{order.plan_title}» активирован.\n"
                    f"{next_step}"
                ),
                reply_markup=self._keyboard(
                    [[self._callback_button("Мой кабинет", "menu:home")]],
                ),
            )
        except Exception:  # noqa: BLE001
            pass

    def notify_user_order_rejected(self, *, order_id: str) -> None:
        """Notify user that their order was rejected."""
        if self._bot_gateway is None:
            return
        try:
            rec = self._facade.store.get_subscription_order(order_id)
            user = self._facade.store.get_user(rec.user_id)
            if user is None or user.telegram_user_id is None:
                return
            self._bot_gateway.send_message(  # type: ignore[union-attr]
                chat_id=user.telegram_user_id,
                text=(
                    "❌ Ваш заказ отклонён администратором.\n"
                    "Если вы уже перевели деньги, свяжитесь с поддержкой.\n"
                    "Вы можете создать новый заказ в разделе «Подписка»."
                ),
                reply_markup=self._keyboard(
                    [
                        [self._callback_button("Подписка", "menu:subscription")],
                        [self._url_button("Поддержка", self._settings.support_url)],
                    ],
                ),
            )
        except Exception:  # noqa: BLE001
            pass

    def _payments_enabled(self) -> bool:
        return self._facade.yookassa_payments_enabled()

    def process_pending_payment_checks(self) -> None:
        if not self._payments_enabled():
            return
        now = datetime.now(UTC)
        if (
            self._last_payment_check_at is not None
            and (now - self._last_payment_check_at).total_seconds()
            < self._settings.yookassa_pending_check_interval_seconds
        ):
            return
        self._last_payment_check_at = now
        for order in self._facade.process_pending_yookassa_orders():
            self.notify_user_order_approved(order_id=order.order_id, order=order)
            self.deliver_order_access(order_id=order.order_id)

    def deliver_order_access(self, *, order_id: str) -> None:
        if self._bot_gateway is None:
            return
        try:
            record = self._facade.store.get_subscription_order(order_id)
            user = self._facade.store.get_user(record.user_id)
        except KeyError:
            return
        if user is None or user.telegram_user_id is None:
            return
        self.deliver_user_access(
            user_id=user.user_id,
            telegram_user_id=user.telegram_user_id,
            android_code_purpose="device_access",
        )

    def deliver_user_access(
        self,
        *,
        user_id: str,
        telegram_user_id: int,
        android_code_purpose: str = "device_access",
        include_android_apk: bool = True,
        include_android_code: bool = True,
    ) -> None:
        if self._bot_gateway is None:
            return
        self._send_subscription_access(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
        )

    def broadcast_message(self, text: str) -> dict[str, int]:
        if self._bot_gateway is None:
            return {"sent": 0, "failed": 0}

        telegram_ids = self._facade.store.get_all_telegram_user_ids()
        sent = 0
        failed = 0
        for tg_id in telegram_ids:
            try:
                self._bot_gateway.send_message(
                    chat_id=tg_id,
                    text=text,
                    parse_mode="HTML",
                )
                sent += 1
            except Exception:
                failed += 1
        return {"sent": sent, "failed": failed}

    def _handle_admin_broadcast(self, admin_telegram_id: int, message_id: int) -> BotResponse:
        if self._bot_gateway is None:
            return BotResponse(text="Ошибка: Gateway не инициализирован.")
        telegram_ids = self._facade.store.get_all_telegram_user_ids()
        sent = 0
        failed = 0
        for tg_id in telegram_ids:
            try:
                self._bot_gateway.copy_message(
                    chat_id=tg_id,
                    from_chat_id=admin_telegram_id,
                    message_id=message_id,
                )
                sent += 1
            except Exception:
                failed += 1
        return BotResponse(
            text=(
                "✅ Рассылка завершена!\n\n"
                f"Доставлено: {sent}\n"
                f"Ошибок (заблокировали бота): {failed}\n"
            ),
            reply_markup=self._keyboard([[self._callback_button("\U0001f519 В админку", "admin:home", style="primary")]]),
        )

    def activate_trial_access(
        self,
        *,
        telegram_user_id: int,
    ) -> None:
        user = self._require_registered_user(telegram_user_id)
        self._facade.activate_trial(user_id=user.user_id)
        self.deliver_user_access(
            user_id=user.user_id,
            telegram_user_id=telegram_user_id,
            android_code_purpose="trial",
        )

    def delete_order_invoice(
        self,
        *,
        order_id: str,
        clear_metadata: bool = False,
    ) -> None:
        if self._bot_gateway is None:
            return
        try:
            order = self._facade.store.get_subscription_order(order_id)
        except KeyError:
            return

        chat_id = order.telegram_invoice_chat_id
        message_id = order.telegram_invoice_message_id
        if chat_id is None or message_id is None:
            return

        try:
            self._bot_gateway.delete_message(  # type: ignore[union-attr]
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:  # noqa: BLE001
            return

        if clear_metadata:
            try:
                self._facade.store.clear_order_invoice_message(order_id=order_id)
            except Exception:  # noqa: BLE001
                pass

    def _send_android_apk(
        self,
        *,
        user_id: str,
        telegram_user_id: int,
    ) -> None:
        if self._bot_gateway is None:
            return

        document_source = self._settings.telegram_android_apk_file_id.strip()
        filename = None
        fallback_download_url = self._settings.telegram_android_download_url.strip()
        if not document_source:
            apk_path = self._settings.telegram_android_apk_path.strip()
            if apk_path:
                path = Path(apk_path).expanduser()
                if path.is_file():
                    max_size = 50 * 1024 * 1024
                    if path.stat().st_size <= max_size:
                        document_source = str(path)
                        filename = self._settings.telegram_android_apk_filename.strip() or path.name
            if not document_source and fallback_download_url:
                document_source = fallback_download_url

        if not document_source:
            self._bot_gateway.send_message(  # type: ignore[union-attr]
                chat_id=telegram_user_id,
                text=(
                    "Не удалось отправить файл клиента автоматически.\n"
                    "Напишите в поддержку — поможем с настройкой VPN-клиента."
                ),
                reply_markup=self._keyboard(
                    [
                        [self._url_button("Поддержка", self._settings.support_url)],
                        [self._callback_button("Подписка", "menu:subscription")],
                    ],
                ),
            )
            return

        try:
            self._bot_gateway.send_document(  # type: ignore[union-attr]
                chat_id=telegram_user_id,
                document=document_source,
                filename=filename,
                caption=self._settings.telegram_android_apk_caption,
            )
        except Exception:  # noqa: BLE001
            if fallback_download_url:
                self._bot_gateway.send_message(  # type: ignore[union-attr]
                    chat_id=telegram_user_id,
                    text=(
                        "Не получилось прикрепить файл клиента, поэтому оставляю прямую ссылку:\n"
                        f"{fallback_download_url}\n\n"
                        "Основной доступ теперь выдаётся через subscription-ссылку в боте."
                    ),
                    reply_markup=self._keyboard(
                        [
                            [self._url_button("Скачать файл", fallback_download_url)],
                            [self._callback_button("Подписка", "menu:subscription")],
                        ],
                    ),
                )
                return
            self._bot_gateway.send_message(  # type: ignore[union-attr]
                chat_id=telegram_user_id,
                text=(
                    "Не удалось отправить файл клиента автоматически.\n"
                    "Напишите в поддержку — поможем с настройкой VPN-клиента."
                ),
                reply_markup=self._keyboard(
                    [
                        [self._url_button("Поддержка", self._settings.support_url)],
                        [self._callback_button("Подписка", "menu:subscription")],
                    ],
                ),
            )
            return

        self._bot_gateway.send_message(  # type: ignore[union-attr]
            chat_id=telegram_user_id,
            text=(
                "Файл отправлен. Основной доступ теперь выдаётся через subscription-ссылку в боте."
            ),
            reply_markup=self._keyboard(
                [
                    [self._callback_button("Подписка", "menu:subscription")],
                    [self._url_button("Поддержка", self._settings.support_url)],
                ],
            ),
        )

    def _send_ios_subscription_access(
        self,
        *,
        user_id: str,
        telegram_user_id: int,
    ) -> None:
        self._send_subscription_access(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
        )

    def _send_subscription_access(
        self,
        *,
        user_id: str,
        telegram_user_id: int,
    ) -> None:
        if self._bot_gateway is None:
            return
        try:
            subscription_url = self._facade.get_public_subscription_url(user_id)
            happ_install_url = self._facade.get_public_happ_install_url(user_id)
        except (MarzbanError, SubscriptionRequiredError, ValueError) as exc:
            self._bot_gateway.send_message(  # type: ignore[union-attr]
                chat_id=telegram_user_id,
                text=(
                    "Не удалось получить ссылку подключения автоматически.\n"
                    f"{exc}"
                ),
                reply_markup=self._keyboard(
                    [
                        [self._url_button("Поддержка", self._settings.support_url)],
                        [self._callback_button("Подписка", "menu:subscription")],
                    ],
                ),
            )
            return

        self._bot_gateway.send_message(  # type: ignore[union-attr]
            chat_id=telegram_user_id,
            text=(
                f"{self._service_name()}\n\n"
                "1. Установите Happ.\n"
                "2. Нажмите «Подключить VPN».\n"
                "3. Подписка сама откроется в Happ и добавится в приложение.\n\n"
                "Резервная subscription-ссылка:\n"
                f"{subscription_url}"
            ),
            reply_markup=self._keyboard(
                [
                    [self._url_button("Скачать Happ", self._happ_download_url(), style="primary")],
                    [self._url_button("Подключить VPN", happ_install_url, style="success")],
                    [self._copy_button("Скопировать ссылку", subscription_url)],
                    [
                        self._callback_button("Главное меню", "menu:home", style="primary"),
                        self._url_button("Поддержка", self._settings.support_url),
                    ],
                ],
            ),
        )

    def _send_android_activation_code(
        self,
        *,
        user_id: str,
        telegram_user_id: int,
        purpose: str,
    ) -> None:
        if self._bot_gateway is None:
            return
        try:
            activation_code = self._facade.issue_activation_code(
                user_id=user_id,
                purpose=purpose,
            )
        except (SubscriptionRequiredError, ValueError, RuntimeError) as exc:
            self._bot_gateway.send_message(  # type: ignore[union-attr]
                chat_id=telegram_user_id,
                text=(
                    "Не удалось выпустить код активации автоматически.\n"
                    f"{exc}"
                ),
                reply_markup=self._keyboard(
                    [
                        [self._url_button("Поддержка", self._settings.support_url)],
                        [self._callback_button("Подписка", "menu:subscription")],
                    ],
                ),
            )
            return

        self._bot_gateway.send_message(  # type: ignore[union-attr]
            chat_id=telegram_user_id,
            text=(
                "Ваш резервный код доступа:\n"
                f"{activation_code.code}\n\n"
                "Основной доступ теперь выдаётся через subscription-ссылку в боте."
            ),
            reply_markup=self._keyboard(
                [
                    [self._callback_button("Получить код еще раз", "delivery:code")],
                    [self._callback_button("Подписка", "menu:subscription")],
                ],
            ),
        )

    def _send_order_invoice(self, *, telegram_user_id: int, order: SubscriptionOrder) -> None:
        if self._bot_gateway is None:
            return
        self.delete_order_invoice(order_id=order.order_id)
        payload: dict[str, object] = {
            "chat_id": telegram_user_id,
            "title": f"{self._service_name()} • {order.plan_title}",
            "description": f"Подписка на {order.duration_days} дней",
            "payload": order.order_id,
            "provider_token": self._settings.telegram_payments_provider_token,
            "currency": "RUB",
            "prices": [
                {
                    "label": order.plan_title,
                    "amount": order.amount_rub * 100,
                }
            ],
            "start_parameter": order.order_id,
            "need_email": self._settings.telegram_payments_require_email,
            "send_email_to_provider": self._settings.telegram_payments_send_email_to_provider,
        }
        provider_data = self._provider_data_for_order(order)
        if provider_data is not None:
            payload["provider_data"] = provider_data
        message = self._bot_gateway.send_invoice(**payload)  # type: ignore[union-attr]
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if isinstance(message_id, int):
            self._facade.store.record_order_invoice_message(
                order_id=order.order_id,
                telegram_chat_id=telegram_user_id,
                telegram_message_id=message_id,
            )

    def _send_order_payment_link(self, *, telegram_user_id: int, order: SubscriptionOrder) -> None:
        if self._bot_gateway is None:
            return
        rows: list[list[dict[str, object]]] = []
        if order.payment_url:
            rows.append([self._url_button("💸 Оплатить в YooKassa", order.payment_url)])
            rows.append([self._callback_button("🔄 Проверить оплату", f"order:check:{order.order_id}")])
        rows.append([self._callback_button("❌ Отменить заказ", f"order:cancel:{order.order_id}")])
        self._bot_gateway.send_message(  # type: ignore[union-attr]
            chat_id=telegram_user_id,
            text=(
                "💸 Платеж создан\n\n"
                f"📦 Тариф: {order.plan_title}\n"
                f"💰 Сумма: {self._format_rub(order.amount_rub)}\n\n"
                "После оплаты я проверю YooKassa и пришлю подписку автоматически."
            ),
            reply_markup=self._keyboard(rows),
        )

    def _provider_data_for_order(self, order: SubscriptionOrder) -> str | None:
        if not self._settings.telegram_payments_receipt_enabled:
            return None

        receipt: dict[str, object] = {
            "items": [
                {
                    "description": f"Подписка {self._service_name()} • {order.plan_title}",
                    "quantity": "1.00",
                    "amount": {
                        "value": f"{order.amount_rub:.2f}",
                        "currency": "RUB",
                    },
                    "vat_code": self._settings.telegram_payments_receipt_vat_code,
                    "payment_subject": self._settings.telegram_payments_receipt_subject,
                    "payment_mode": self._settings.telegram_payments_receipt_mode,
                }
            ]
        }
        if self._settings.telegram_payments_receipt_tax_system_code is not None:
            receipt["tax_system_code"] = self._settings.telegram_payments_receipt_tax_system_code
        return json.dumps({"receipt": receipt}, ensure_ascii=False)

    def _get_admin_telegram_ids(self) -> list[int]:
        try:
            admins = self._facade.store.list_admin_users()
            return [u.telegram_user_id for u in admins if u.telegram_user_id is not None]
        except Exception:  # noqa: BLE001
            return []

    def _friendly_error_response(
        self,
        telegram_user_id: int,
        error_message: str,
        edit_message: bool = True,
    ) -> BotResponse:
        user = None
        try:
            user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        except Exception:
            pass
        has_access = False
        trial_avail = True
        if user is not None:
            try:
                subscription = self._facade.get_user_subscription_by_id(user.user_id)
                has_access = subscription.can_use_vpn
                trial_avail = user.trial_activated_at is None
            except Exception:
                pass
        
        text = f"⚠️ Произошла ошибка: {error_message}\n\nПожалуйста, попробуйте еще раз или вернитесь в главное меню."
        return BotResponse(
            text=text,
            reply_markup=self._home_keyboard(
                telegram_user_id,
                has_access,
                trial_available=trial_avail,
            ),
            edit_message=edit_message,
            photo=self._service_photo_source(),
        )

    def handle_message(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        text: str,
        message_id: int | None = None,
    ) -> BotResponse | None:
        try:
            return self._handle_message_impl(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
                text=text,
                message_id=message_id,
            )
        except KeyError as exc:
            if "not linked" in str(exc) or "user" in str(exc).lower():
                return BotResponse(
                    text="⚠️ Профиль не найден. Пожалуйста, введите /start, чтобы начать пользоваться ботом.",
                    reply_markup=self._home_keyboard(
                        telegram_user_id=telegram_user_id,
                        has_active_access=False,
                        trial_available=True,
                    ),
                    photo=self._service_photo_source(),
                )
            return self._friendly_error_response(telegram_user_id, str(exc), edit_message=False)
        except Exception as exc:
            return self._friendly_error_response(telegram_user_id, str(exc), edit_message=False)

    def _handle_message_impl(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        text: str,
        message_id: int | None = None,
    ) -> BotResponse | None:
        normalized = text.strip()
        if not normalized and not message_id:
            return None

        pending = self._take_pending_action(telegram_user_id)
        if pending is not None:
            return self._complete_pending_action(
                action=pending,
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
                text=normalized,
                message_id=message_id,
            )

        parts = normalized.split(maxsplit=1)
        command = parts[0].lower()
        tail = parts[1] if len(parts) > 1 else ""

        if command == "/start":
            referral_token = self._extract_referral_token(tail)
            existing_user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
            if existing_user is None or existing_user.legal_accepted_at is None:
                self._set_pending_start_referral(telegram_user_id, referral_token)
                return self._legal_consent_response()
            return self._continue_start_after_legal(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
                referral_token=referral_token,
            )

        if command == "/help":
            if not self._legal_accepted(telegram_user_id):
                return self._legal_consent_response()
            self._ensure_registered_from_telegram(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
            )
            return self._home_response(telegram_user_id)

        if command == "/admin":
            if self._is_active(telegram_user_id):
                return self._admin_home_response()
            return BotResponse(text="У вас нет прав администратора.")

        if self._is_active(telegram_user_id) and command == "/addadmin":
            target = tail.strip()
            if not target:
                return BotResponse(text="Укажите Telegram ID или username. Пример:\n/addadmin 123456789\n/addadmin @username")
            user_record = None
            if not target.lstrip("@").isdigit() and not target.isdigit():
                user_record = self._facade.store.get_user_by_telegram_username(target)
            else:
                user_record = self._facade.store.get_user_by_telegram_id(int(target.lstrip("@")))
            
            if user_record is None:
                return BotResponse(text=f"Пользователь {target} не найден в базе. Сначала он должен запустить бота.")
            
            self._facade.store.set_user_role(user_record.user_id, "admin")
            return BotResponse(text=f"Пользователь {target} успешно назначен администратором.")

        # /logout removed, handled by simple return to home
        if command == "/logout":
            return self._home_response(telegram_user_id)

        if self._is_active(telegram_user_id) and command == "/users":
            return self._admin_users_response(view="all", offset=0)

        if self._is_active(telegram_user_id) and command == "/orders":
            return self._admin_orders_response()

        if self._is_active(telegram_user_id) and command == "/sources":
            return self._sources_response()

        if self._is_active(telegram_user_id) and command == "/catalog":
            return self._catalog_response()

        if self._is_active(telegram_user_id) and command == "/addlinks":
            if tail.strip():
                return self._add_source_response(
                    kind="raw_links",
                    content=tail.strip(),
                    actor=self._actor_label(display_name, username),
                )
            self._set_pending_action(telegram_user_id, "raw_links")
            return BotResponse(text="Отправьте ссылки следующим сообщением.")

        if self._is_active(telegram_user_id) and command == "/addsub":
            if tail.strip():
                return self._add_source_response(
                    kind="subscription_url",
                    content=tail.strip(),
                    actor=self._actor_label(display_name, username),
                )
            self._set_pending_action(telegram_user_id, "subscription_url")
            return BotResponse(text="Отправьте subscription URL следующим сообщением.")

        if self._is_active(telegram_user_id) and command == "/finduser":
            return self._admin_search_results_response(tail.strip())

        if self._is_active(telegram_user_id) and command in {"/block", "/unblock"}:
            if not tail.strip():
                return BotResponse(text="Укажите user_id после команды.")
            return self._set_user_blocked_response(
                user_id=tail.strip(),
                blocked=command == "/block",
            )

        return self._home_response(telegram_user_id)

    def handle_callback(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        data: str,
    ) -> BotResponse | None:
        try:
            return self._handle_callback_impl(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
                data=data,
            )
        except KeyError as exc:
            if "not linked" in str(exc) or "user" in str(exc).lower():
                return BotResponse(
                    text="⚠️ Профиль не найден. Пожалуйста, откройте главное меню, чтобы зарегистрироваться и начать пользоваться ботом.",
                    reply_markup=self._keyboard(
                        [[self._callback_button("💠 Главное меню", "menu:home", style="primary")]]
                    ),
                    edit_message=True,
                    photo=self._service_photo_source(),
                )
            return self._friendly_error_response(telegram_user_id, str(exc), edit_message=True)
        except Exception as exc:
            return self._friendly_error_response(telegram_user_id, str(exc), edit_message=True)

    def _handle_callback_impl(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        data: str,
    ) -> BotResponse | None:
        if data not in {
            "profile:edit_name",
            "profile:edit_email",
            "admin:add:links",
            "admin:add:subscription",
            "admin:search",
            "promo:enter",
            "legal:accept",
        }:
            self._clear_pending_action(telegram_user_id)
        if data == "legal:accept":
            return self._accept_legal_response(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
            )
        if data.startswith("menu:"):
            self._ensure_registered_from_telegram(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
            )
        if data == "menu:home":
            return self._home_response(telegram_user_id, edit_message=True)
        if data.startswith("guest:platform:"):
            platform = data.split(":", maxsplit=2)[2]
            return self._select_platform_response(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
                platform=platform,
            )
        if data == "guest:trial":
            return self._guest_trial_response(telegram_user_id=telegram_user_id)
        if data == "guest:relink":
            return BotResponse(
                text=(
                    "Сейчас основной способ подключения — Happ. Откройте «Моя подписка», "
                    "активируйте trial или оплатите тариф, и я пришлю кнопку подключения."
                ),
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", "menu:home")]],
                ),
                edit_message=True,
            )
        if data == "menu:account":
            return self._account_response(telegram_user_id, edit_message=True)
        if data == "menu:subscription":
            return self._subscription_response(telegram_user_id, edit_message=True)
        if data == "menu:keys":
            return self._keys_response(telegram_user_id, edit_message=True)
        if data == "menu:traffic":
            return self._traffic_response(telegram_user_id, edit_message=True)
        if data == "menu:devices":
            return self._devices_response(telegram_user_id, edit_message=True)
        if data == "menu:support":
            return self._support_response(edit_message=True)
        if data == "menu:share":
            return self._share_response(telegram_user_id, edit_message=True)
        if data == "profile:edit_name":
            self._set_pending_action(telegram_user_id, "edit_name")
            return BotResponse(
                text="Отправьте новое имя для профиля.",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", "menu:account")]],
                ),
                edit_message=True,
                callback_notice="Жду новое имя.",
            )
        if data == "profile:edit_email":
            self._set_pending_action(telegram_user_id, "edit_email")
            return BotResponse(
                text="Отправьте новый email. Чтобы очистить email, отправьте символ -",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", "menu:account")]],
                ),
                edit_message=True,
                callback_notice="Жду новый email.",
            )
        if data.startswith("profile:platform:"):
            platform = data.split(":", maxsplit=2)[2]
            return self._change_platform_response(
                telegram_user_id=telegram_user_id,
                platform=platform,
            )
        if data == "trial:activate":
            self._ensure_registered_from_telegram(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                username=username,
            )
            return self._trial_response(telegram_user_id=telegram_user_id)
        if data == "delivery:apk":
            return self._delivery_response(telegram_user_id=telegram_user_id)
        if data == "delivery:code":
            return self._delivery_response(telegram_user_id=telegram_user_id)
        if data == "delivery:send":
            return self._delivery_response(telegram_user_id=telegram_user_id)
        if data == "key:open":
            return self._key_details_response(telegram_user_id, edit_message=True)
        if data == "key:create":
            return self._plans_response(
                telegram_user_id,
                edit_message=True,
                notice="Выберите тариф для нового ключа.",
            )
        if data == "key:extend":
            return self._plans_response(
                telegram_user_id,
                edit_message=True,
                notice="Выберите тариф для продления ключа.",
            )
        if data == "promo:enter":
            self._set_pending_action(telegram_user_id, "apply_promo")
            return self._promo_entry_response(edit_message=True)
        if data == "promo:clear":
            self._clear_active_promo(telegram_user_id)
            return self._plans_response(
                telegram_user_id,
                edit_message=True,
                notice="Промокод убран.",
            )
        if data.startswith("key:device:"):
            platform = data.split(":", maxsplit=2)[2]
            return self._key_device_instruction_response(
                telegram_user_id,
                platform=platform,
                edit_message=True,
            )
        if data.startswith("sub:buy:"):
            plan_id = data.split(":", maxsplit=2)[2]
            return self._create_order_response(
                telegram_user_id=telegram_user_id,
                plan_id=plan_id,
            )
        if data.startswith("order:paid:"):
            order_id = data.split(":", maxsplit=2)[2]
            return self._mark_order_paid_response(
                telegram_user_id=telegram_user_id,
                order_id=order_id,
            )
        if data.startswith("order:check:"):
            order_id = data.split(":", maxsplit=2)[2]
            return self._check_order_payment_response(
                telegram_user_id=telegram_user_id,
                order_id=order_id,
            )
        if data.startswith("order:cancel:"):
            order_id = data.split(":", maxsplit=2)[2]
            return self._cancel_order_response(
                telegram_user_id=telegram_user_id,
                order_id=order_id,
            )
        if data == "admin:home":
            if not self._is_active(telegram_user_id):
                return BotResponse(
                    text="Админ-доступ недоступен для этого аккаунта.",
                    edit_message=True,
                    callback_notice="Нет прав администратора.",
                )
            return self._admin_home_response(edit_message=True)
        if data == "admin:logout":
            return self._home_response(
                telegram_user_id,
                edit_message=True,
            )
        if data == "admin:users":
            if not self._is_active(telegram_user_id):
                return BotResponse(
                    text="Админ-доступ недоступен для этого аккаунта.",
                    edit_message=True,
                )
            return self._admin_users_response(view="all", offset=0, edit_message=True)
        if data == "admin:referrals":
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            return self._admin_referrals_response(edit_message=True)
        if data.startswith("admin:referrals:user:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            user_id = data.split(":", maxsplit=3)[3]
            return self._admin_referral_user_response(user_id=user_id, edit_message=True)
        if data.startswith("admin:resetdevices:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            user_id = data.split(":", maxsplit=2)[2]
            return self._admin_reset_devices_response(user_id=user_id, edit_message=True)
        if data.startswith("admin:adjustdays:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _action, user_id, delta_days = data.split(":", maxsplit=3)
            return self._admin_adjust_days_response(
                user_id=user_id,
                delta_days=int(delta_days),
                actor=self._actor_label(display_name, username),
                edit_message=True,
            )
        if data.startswith("admin:reward:revoke:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _reward, _revoke, reward_id, user_id = data.split(":", maxsplit=4)
            return self._admin_revoke_referral_reward_response(
                reward_id=int(reward_id),
                user_id=user_id,
                actor=self._actor_label(display_name, username),
                edit_message=True,
            )
        if data.startswith("admin:users:view:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _prefix, _users, _view, view, offset = data.split(":", maxsplit=4)
            return self._admin_users_response(
                view=view,
                offset=int(offset),
                edit_message=True,
            )
        if data == "admin:orders":
            if not self._is_active(telegram_user_id):
                return BotResponse(
                    text="Админ-доступ недоступен для этого аккаунта.",
                    edit_message=True,
                )
            return self._admin_orders_response(edit_message=True)
        if data == "admin:sources":
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            return self._sources_response(edit_message=True)
        if data == "admin:catalog":
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            return self._catalog_response(edit_message=True)
        if data == "admin:broadcast:start":
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            self._set_pending_action(telegram_user_id, "admin_broadcast")
            return BotResponse(
                text=(
                    "📣 <b>Рассылка пользователям</b>\n\n"
                    "Отправьте следующее сообщение, которое хотите разослать. "
                    "Оно будет в точности скопировано всем пользователям (включая фото, видео, голосовые и формат).\n\n"
                    "Для отмены отправьте /cancel или нажмите «Назад»."
                ),
                reply_markup=self._keyboard([[self._callback_button("\U0001f519 Отмена", "admin:home", style="primary")]]),
                edit_message=True,
            )
        if data == "admin:add:links":
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            self._set_pending_action(telegram_user_id, "raw_links")
            return BotResponse(
                text="Отправьте одним следующим сообщением набор raw-ссылок на сервера.",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", "admin:sources")]],
                ),
                edit_message=True,
                callback_notice="Жду ссылки.",
            )
        if data == "admin:add:subscription":
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            self._set_pending_action(telegram_user_id, "subscription_url")
            return BotResponse(
                text="Отправьте следующим сообщением subscription URL. После этого источник подтянется в каталог.",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", "admin:sources")]],
                ),
                edit_message=True,
                callback_notice="Жду subscription URL.",
            )
        if data.startswith("admin:source:rename:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            source_id = data.split(":", maxsplit=3)[3]
            self._set_pending_action(
                telegram_user_id,
                "rename_source",
                params={"source_id": source_id},
            )
            return BotResponse(
                text="Отправьте новое название источника серверов.",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", f"admin:source:{source_id}", style="primary")]],
                ),
                edit_message=True,
                callback_notice="Жду название.",
            )
        if data == "admin:search":
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            self._set_pending_action(telegram_user_id, "admin_search_user")
            return BotResponse(
                text="Отправьте user_id, Telegram, username или email для поиска пользователя.",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", "admin:home")]],
                ),
                edit_message=True,
                callback_notice="Жду запрос для поиска.",
            )
        if data.startswith("admin:catalog:view:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _catalog, _view, source_id, offset = data.split(":", maxsplit=4)
            return self._catalog_response(
                source_id=None if source_id == "all" else source_id,
                offset=int(offset),
                edit_message=True,
            )
        if data.startswith("admin:source:sync:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            source_id = data.split(":", maxsplit=3)[3]
            return self._sync_source_response(source_id=source_id, edit_message=True)
        if data.startswith("admin:source:delete:confirm:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            source_id = data.split(":", maxsplit=4)[4]
            return self._delete_source_confirm_response(
                source_id=source_id,
                edit_message=True,
            )
        if data.startswith("admin:source:delete:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            source_id = data.split(":", maxsplit=3)[3]
            return self._delete_source_response(source_id=source_id, edit_message=True)
        if data.startswith("admin:source:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            source_id = data.split(":", maxsplit=2)[2]
            return self._source_response(source_id=source_id, edit_message=True)
        if data.startswith("admin:entry:delete:confirm:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _entry, _delete, _confirm, entry_id, source_id, offset = data.split(":", maxsplit=6)
            return self._delete_entry_confirm_response(
                entry_id=entry_id,
                source_id=None if source_id == "all" else source_id,
                offset=int(offset),
                edit_message=True,
            )
        if data.startswith("admin:entry:delete:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _entry, _delete, entry_id, source_id, offset = data.split(":", maxsplit=5)
            return self._delete_entry_response(
                entry_id=entry_id,
                source_id=None if source_id == "all" else source_id,
                offset=int(offset),
                edit_message=True,
            )
        if data.startswith("admin:entry:state:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _entry, _state, entry_id, state, source_id, offset = data.split(":", maxsplit=6)
            return self._set_entry_state_response(
                entry_id=entry_id,
                state=state,
                source_id=None if source_id == "all" else source_id,
                offset=int(offset),
                edit_message=True,
            )
        if data.startswith("admin:entry:rename:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _entry, _rename, entry_id, source_id, offset = data.split(":", maxsplit=5)
            self._set_pending_action(
                telegram_user_id,
                "rename_entry",
                params={"entry_id": entry_id, "source_id": source_id, "offset": offset},
            )
            return BotResponse(
                text="Отправьте новое красивое название сервера.",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", f"admin:entry:{entry_id}:{source_id}:{offset}", style="primary")]],
                ),
                edit_message=True,
                callback_notice="Жду название.",
            )
        if data.startswith("admin:entry:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _entry, entry_id, source_id, offset = data.split(":", maxsplit=4)
            return self._catalog_entry_response(
                entry_id=entry_id,
                source_id=None if source_id == "all" else source_id,
                offset=int(offset),
                edit_message=True,
            )
        if data.startswith("admin:user:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            parts = data.split(":")
            user_id = parts[2]
            view = parts[3] if len(parts) > 3 else "all"
            offset = int(parts[4]) if len(parts) > 4 else 0
            return self._admin_user_response(
                user_id,
                edit_message=True,
                back_view=view,
                back_offset=offset,
            )
        if data.startswith("admin:sendlink:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            user_id = data.split(":", maxsplit=2)[2]
            return self._send_subscription_link_response(user_id=user_id, rotate=False)
        if data.startswith("admin:rotatelink:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            user_id = data.split(":", maxsplit=2)[2]
            return self._send_subscription_link_response(user_id=user_id, rotate=True)
        if data.startswith("admin:block:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            user_id = data.split(":", maxsplit=2)[2]
            return self._set_user_blocked_response(user_id=user_id, blocked=True)
        if data.startswith("admin:unblock:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            user_id = data.split(":", maxsplit=2)[2]
            return self._set_user_blocked_response(user_id=user_id, blocked=False)
        if data.startswith("admin:grant:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _prefix, _grant, user_id, plan_id = data.split(":", maxsplit=3)
            return self._grant_subscription_response(
                user_id=user_id,
                plan_id=plan_id,
                admin_actor=self._actor_label(display_name, username),
            )
        if data.startswith("admin:revoke:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            user_id = data.split(":", maxsplit=2)[2]
            return self._revoke_subscription_response(
                user_id=user_id,
                admin_actor=self._actor_label(display_name, username),
            )
        if data.startswith("admin:delete:confirm:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _delete, _confirm, user_id, view, offset = data.split(":", maxsplit=5)
            return self._delete_user_confirm_response(
                user_id=user_id,
                edit_message=True,
                back_view=view,
                back_offset=int(offset),
            )
        if data.startswith("admin:delete:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            _admin, _delete, user_id, view, offset = data.split(":", maxsplit=4)
            return self._delete_user_response(
                user_id=user_id,
                actor_telegram_user_id=telegram_user_id,
                edit_message=True,
                back_view=view,
                back_offset=int(offset),
            )
        if data.startswith("admin:order:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            order_id = data.split(":", maxsplit=2)[2]
            return self._admin_order_response(order_id, edit_message=True)
        if data.startswith("admin:approve:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            order_id = data.split(":", maxsplit=2)[2]
            return self._approve_order_response(
                order_id=order_id,
                admin_actor=self._actor_label(display_name, username),
            )
        if data.startswith("admin:reject:"):
            if not self._is_active(telegram_user_id):
                return BotResponse(text="Админ-режим закрыт.", edit_message=True)
            order_id = data.split(":", maxsplit=2)[2]
            return self._reject_order_response(
                order_id=order_id,
                admin_actor=self._actor_label(display_name, username),
            )

        return BotResponse(
            text="Действие не распознано.",
            reply_markup=self._home_keyboard(
                telegram_user_id,
                self._has_active_access(telegram_user_id),
                trial_available=self._trial_available(telegram_user_id),
            ),
            edit_message=True,
            photo=self._service_photo_source(),
        )

    def _complete_pending_action(
        self,
        *,
        action: PendingBotAction,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        text: str,
        message_id: int | None = None,
    ) -> BotResponse:
        if action.action == "admin_broadcast" and message_id is not None:
            return self._handle_admin_broadcast(
                admin_telegram_id=telegram_user_id,
                message_id=message_id,
            )
        if action.action == "admin_search_user":
            return self._admin_search_results_response(text)
        if action.action == "edit_name":
            user = self._require_registered_user(telegram_user_id)
            self._facade.store.update_user_profile(user.user_id, display_name=text)
            return self._account_response(
                telegram_user_id,
                notice="Имя обновлено.",
            )
        if action.action == "edit_email":
            user = self._require_registered_user(telegram_user_id)
            email = None if text.strip() == "-" else text.strip()
            self._facade.store.update_user_profile(user.user_id, email=email)
            return self._account_response(
                telegram_user_id,
                notice="Email обновлен.",
            )
        if action.action == "raw_links":
            return self._add_source_response(
                kind="raw_links",
                content=text,
                actor=self._actor_label(display_name, username),
            )
        if action.action == "subscription_url":
            return self._add_source_response(
                kind="subscription_url",
                content=text,
                actor=self._actor_label(display_name, username),
            )
        if action.action == "apply_promo":
            return self._apply_promo_response(
                telegram_user_id=telegram_user_id,
                code=text,
            )
        if action.action == "rename_source":
            source_id = action.params.get("source_id", "")
            return self._rename_source_response(source_id=source_id, title=text)
        if action.action == "rename_entry":
            entry_id = action.params.get("entry_id", "")
            source_id = action.params.get("source_id", "all")
            offset = int(action.params.get("offset", "0") or "0")
            return self._rename_entry_response(
                entry_id=entry_id,
                display_name=text,
                source_id=None if source_id == "all" else source_id,
                offset=offset,
            )
        return self._home_response(telegram_user_id)

    def _continue_start_after_legal(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        referral_token: str | None,
        activate_trial_for_new_user: bool = False,
        edit_message: bool = False,
    ) -> BotResponse:
        existing_user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        user = self._ensure_registered_from_telegram(
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            username=username,
        )
        if referral_token is not None:
            self._facade.record_referral_start(
                referral_token=referral_token,
                telegram_user_id=telegram_user_id,
            )
        should_activate_trial = (
            activate_trial_for_new_user or existing_user is None
        ) and user.trial_activated_at is None
        if should_activate_trial:
            try:
                self.activate_trial_access(telegram_user_id=telegram_user_id)
            except (TrialAlreadyActivatedError, ValueError) as exc:
                return self._home_response(
                    telegram_user_id,
                    edit_message=edit_message,
                    notice=str(exc),
                )
            return self._trial_started_response(
                telegram_user_id,
                edit_message=edit_message,
            )
        return self._home_response(telegram_user_id, edit_message=edit_message)

    def _accept_legal_response(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
    ) -> BotResponse:
        existing_user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        user = self._ensure_registered_from_telegram(
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            username=username,
        )
        if user.legal_accepted_at is None:
            user = self._facade.store.mark_legal_accepted(user.user_id)
        referral_token = self._take_pending_start_referral(telegram_user_id)
        if referral_token is not None:
            self._facade.record_referral_start(
                referral_token=referral_token,
                telegram_user_id=telegram_user_id,
            )
        if existing_user is None and user.trial_activated_at is None:
            try:
                self.activate_trial_access(telegram_user_id=telegram_user_id)
            except (TrialAlreadyActivatedError, ValueError) as exc:
                return self._home_response(
                    telegram_user_id,
                    edit_message=True,
                    notice=str(exc),
                )
            return self._trial_started_response(telegram_user_id, edit_message=True)
        return self._home_response(
            telegram_user_id,
            edit_message=True,
            notice="Согласие принято.",
        )

    def _legal_consent_response(
        self,
        *,
        edit_message: bool = False,
    ) -> BotResponse:
        return BotResponse(
            text=(
                f"{self._service_name()}\n\n"
                "Перед началом работы подтвердите, что вы ознакомились "
                "с пользовательским соглашением и политикой конфиденциальности."
            ),
            reply_markup=self._keyboard(
                [
                    [
                        self._url_button(
                            "Пользовательское соглашение",
                            self._settings.legal_user_agreement_url,
                        )
                    ],
                    [
                        self._url_button(
                            "Политика конфиденциальности",
                            self._settings.legal_privacy_policy_url,
                        )
                    ],
                    [self._callback_button("Принять", "legal:accept", style="success")],
                ]
            ),
            edit_message=edit_message,
        )

    def _select_platform_response(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
        platform: str,
    ) -> BotResponse:
        user = self._facade.register_telegram_onboarding(
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            username=username,
            platform=platform,
        )
        return self._subscription_response(
            telegram_user_id,
            edit_message=True,
            notice=self._platform_selected_notice(user.preferred_platform),
        )

    def _guest_trial_response(self, *, telegram_user_id: int) -> BotResponse:
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        if user is None or user.preferred_platform is None:
            self._facade.register_telegram_onboarding(
                telegram_user_id=telegram_user_id,
                display_name="Telegram user",
                username=None,
                platform="subscription",
            )
        return self._trial_response(telegram_user_id=telegram_user_id)

    def _change_platform_response(
        self,
        *,
        telegram_user_id: int,
        platform: str,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        updated = self._facade.update_user_delivery_platform(
            user_id=user.user_id,
            platform=platform,
        )
        return self._account_response(
            telegram_user_id,
            edit_message=True,
            notice=self._platform_selected_notice(updated.preferred_platform),
        )

    def _trial_response(
        self,
        *,
        telegram_user_id: int,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        try:
            self.activate_trial_access(telegram_user_id=telegram_user_id)
        except (TrialAlreadyActivatedError, ValueError) as exc:
            return self._subscription_response(
                telegram_user_id,
                edit_message=True,
                notice=str(exc),
            )
        notice = "Пробный период активирован. Кнопка подключения уже отправлена в чат."
        return self._subscription_response(
            telegram_user_id,
            edit_message=True,
            notice=notice,
        )

    def _delivery_response(
        self,
        *,
        telegram_user_id: int,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        if not subscription.can_use_vpn:
            return self._subscription_response(
                telegram_user_id,
                edit_message=True,
                notice="Сначала активируйте подписку, потом я смогу отправить кнопку подключения.",
            )
        self.deliver_user_access(
            user_id=user.user_id,
            telegram_user_id=telegram_user_id,
        )
        return self._subscription_response(
            telegram_user_id,
            edit_message=True,
            notice=self._delivery_sent_notice("subscription"),
        )

    def _delivery_apk_response(
        self,
        *,
        telegram_user_id: int,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        if not subscription.can_use_vpn:
            return self._subscription_response(
                telegram_user_id,
                edit_message=True,
                notice="Сначала активируйте подписку, потом я смогу отправить кнопку подключения.",
            )
        self.deliver_user_access(
            user_id=user.user_id,
            telegram_user_id=telegram_user_id,
            include_android_apk=True,
            include_android_code=False,
        )
        return self._subscription_response(
            telegram_user_id,
            edit_message=True,
            notice=self._delivery_sent_notice("subscription"),
        )

    def _delivery_code_response(
        self,
        *,
        telegram_user_id: int,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        if not subscription.can_use_vpn:
            return self._subscription_response(
                telegram_user_id,
                edit_message=True,
                notice="Сначала активируйте подписку, потом я смогу выпустить код.",
            )
        self.deliver_user_access(
            user_id=user.user_id,
            telegram_user_id=telegram_user_id,
            include_android_apk=False,
            include_android_code=True,
        )
        return self._subscription_response(
            telegram_user_id,
            edit_message=True,
            notice="Новый код отправлен в чат.",
        )

    def _key_details_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        if not subscription.can_use_vpn:
            return self._plans_response(
                telegram_user_id,
                edit_message=edit_message,
                notice="Сначала купите подписку, потом здесь появится ключ.",
            )
        subscription_url = self._facade.get_public_subscription_url(user.user_id)
        rows = [
            [self._copy_button("Скопировать ссылку", subscription_url)],
            [
                self._callback_button("📎📱 Android", "key:device:android", style="success"),
                self._callback_button("📎📱 IOS/Айфон", "key:device:ios", style="success"),
            ],
            [
                self._callback_button("📎💻 MacOS", "key:device:macos", style="success"),
                self._callback_button("📎🖥 Windows", "key:device:windows", style="success"),
            ],
            [self._callback_button("💠 Главное меню", "menu:home", style="primary")],
        ]
        lines = [
            f"{self._service_name()}",
            "",
            "Ваша subscription-ссылка:",
            f"<pre><code>{escape(subscription_url)}</code></pre>",
            "",
            "Выберите свое устройство ниже 👇 для получения инструкции",
        ]
        if notice:
            lines.extend(["", f"ℹ️ {notice}"])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
            callback_notice=notice,
            parse_mode="HTML",
        )

    def _key_device_instruction_response(
        self,
        telegram_user_id: int,
        *,
        platform: str,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        if not subscription.can_use_vpn:
            return self._plans_response(telegram_user_id, edit_message=edit_message)
        title = {
            "android": "Android",
            "ios": "IOS/Айфон",
            "macos": "MacOS",
            "windows": "Windows",
        }.get(platform, "Устройство")
        instructions = {
            "android": "1. Установите Happ.\n2. Нажмите «Подключить VPN».\n3. Подтвердите импорт подписки.",
            "ios": "1. Установите Happ из App Store.\n2. Нажмите «Подключить VPN».\n3. Разрешите добавить VPN-профиль.",
            "macos": "1. Установите Happ для MacOS.\n2. Скопируйте ключ или откройте ссылку подключения.\n3. Импортируйте подписку в приложении.",
            "windows": "1. Установите Happ для Windows.\n2. Скопируйте ключ или откройте ссылку подключения.\n3. Импортируйте подписку в приложении.",
        }.get(platform, "Откройте Happ и импортируйте subscription-ссылку.")
        happ_url = self._facade.get_public_happ_install_url(user.user_id)
        subscription_url = self._facade.get_public_subscription_url(user.user_id)
        return BotResponse(
            text=f"{title}\n\n{instructions}",
            reply_markup=self._keyboard(
                [
                    [self._url_button("Подключить VPN", happ_url, style="success")],
                    [self._copy_button("Скопировать ключ", subscription_url)],
                    [self._callback_button("🔙 К ключу", "key:open", style="primary")],
                    [self._callback_button("💠 Главное меню", "menu:home", style="primary")],
                ]
            ),
            edit_message=edit_message,
        )

    # Removed _attempt_admin_login

    def _home_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            text = (
                f"{self._service_name()}\n\n"
                "Привет. Нажмите «Старт», чтобы получить пробный доступ на 3 дня "
                "и подключить VPN через Happ."
            )
            return BotResponse(
                text=text,
                reply_markup=self._home_keyboard(
                    telegram_user_id,
                    False,
                    trial_available=True,
                ),
                edit_message=edit_message,
                photo=self._service_photo_source(),
                callback_notice=notice,
            )

        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        status_line = self._home_status_line(subscription)
        lines = [
            f"{self._service_name()}",
            f"Привет, {user.display_name}",
            "",
            "Мы выдаем подписку для Happ: одна кнопка открывает приложение и добавляет VPN.",
            "",
            status_line,
            self._home_hint(subscription=subscription),
            "",
            "Главное меню:",
        ]
        if notice:
            lines.extend(["", f"\u2139\ufe0f {notice}"])
        return BotResponse(
            text="\n".join(line for line in lines if line),
            reply_markup=self._home_keyboard(
                telegram_user_id,
                subscription.can_use_vpn,
                trial_available=user.trial_activated_at is None,
            ),
            edit_message=edit_message,
            photo=self._service_photo_source(),
            callback_notice=notice,
        )

    def _trial_started_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        text = (
            f"{self._service_name()}\n\n"
            "Пробный доступ на 3 дня активирован.\n"
            f"Доступ до: {self._expiry_label(subscription)}\n\n"
            "Ниже я уже отправил сообщение с кнопками «Скачать Happ» и «Подключить VPN»."
        )
        return BotResponse(
            text=text,
            reply_markup=self._home_keyboard(
                telegram_user_id,
                subscription.can_use_vpn,
                trial_available=False,
            ),
            edit_message=edit_message,
            photo=self._service_photo_source(),
        )

    def _account_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        profile = self._facade.get_user_profile_by_id(user.user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        lines = [
            "Личный кабинет",
            "",
            f"Имя: {profile.display_name}",
            f"Telegram: {profile.telegram_handle}",
            f"Email: {profile.email or 'Не указан'}",
            "",
            f"Подписка: {self._subscription_status_label(subscription)}",
            f"Доступ до: {self._expiry_label(subscription)}",
            f"Лимит устройств: до {self._device_limit_label()}",
            "",
            "Формат доступа: Happ subscription",
        ]
        if notice:
            lines.extend(["", f"\u2139\ufe0f {notice}"])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(
                [
                    [
                        self._callback_button("\u270f\ufe0f Изменить имя", "profile:edit_name"),
                        self._callback_button("\u270f\ufe0f Изменить email", "profile:edit_email"),
                    ],
                    [self._callback_button("Моя подписка", "menu:subscription", style="success")],
                    [self._callback_button("\U0001f519 Назад", "menu:home", style="primary")],
                ],
            ),
            edit_message=edit_message,
            photo=self._cabinet_photo_source(),
            callback_notice=notice,
        )

    def _subscription_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        if subscription.can_use_vpn:
            return self._active_subscription_purchase_response(
                telegram_user_id,
                edit_message=edit_message,
                notice=notice,
            )
        return self._plans_response(
            telegram_user_id,
            edit_message=edit_message,
            notice=notice,
        )

    def _plans_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        orders = self._facade.list_user_orders(user.user_id, limit=1)
        text_lines = [
            "Для подключения:",
            "",
            "1.Выберите необходимый тариф",
            "2.Внесите платеж",
            "3.Получите ключ с простой инструкцией для Вашего устройства.",
            "😉",
            "",
            "Не стесняйтесь обращаться к нам за помощью, даже по мелочам! 😊",
            "",
            "⇩ Выберите нужный тариф ⇩",
        ]
        promo_result = self._active_discount_promo_result(telegram_user_id)
        if promo_result is not None:
            text_lines.extend(
                [
                    "",
                    self._promo_applied_text(promo_result),
                ]
            )
        keyboard_rows: list[list[dict[str, object]]] = []
        if promo_result is None:
            keyboard_rows.append(
                [self._callback_button("Ввести промокод", "promo:enter", style="primary")]
            )
        else:
            keyboard_rows.append(
                [
                    self._callback_button("Изменить промокод", "promo:enter", style="primary"),
                    self._callback_button("Убрать промокод", "promo:clear"),
                ]
            )
        discounted_prices = (
            promo_result.discounted_prices_by_plan
            if promo_result is not None
            else {}
        )
        for plan in self._facade.get_plans():
            discounted_price = discounted_prices.get(plan.id)
            keyboard_rows.append(
                [
                    self._callback_button(
                        self._plan_button_label(plan, discounted_price=discounted_price),
                        f"sub:buy:{plan.id}",
                        style="success",
                    )
                ]
            )

        if orders:
            order = orders[0]
            text_lines.extend(
                [
                    "",
                    "Последний заказ:",
                    (
                        f"{order.plan_title} — {self._order_status_label(order)}"
                        if order.status_message is None
                        else f"{order.plan_title} — {order.status_message}"
                    ),
                ]
            )
        if notice:
            text_lines.extend(["", f"\u2139\ufe0f {notice}"])

        keyboard_rows.append([self._callback_button("\U0001f519 Назад", "menu:home", style="primary")])
        return BotResponse(
            text="\n".join(text_lines),
            reply_markup=self._keyboard(keyboard_rows),
            edit_message=edit_message,
            photo=self._plans_photo_source(),
            callback_notice=notice,
        )

    def _promo_entry_response(
        self,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        lines = [
            "Промокод",
            "",
            "Отправьте промокод следующим сообщением.",
        ]
        if notice:
            lines.extend(["", f"ℹ️ {notice}"])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(
                [[self._callback_button("К тарифам", "menu:subscription", style="primary")]]
            ),
            edit_message=edit_message,
            callback_notice=notice or "Жду промокод.",
        )

    def _active_discount_promo_result(
        self,
        telegram_user_id: int,
    ) -> PromoApplyResult | None:
        code = self._get_active_promo(telegram_user_id)
        if not code:
            return None
        user = self._require_registered_user(telegram_user_id)
        try:
            result = self._facade.apply_promo(
                self._facade._claims_for_user(user),  # noqa: SLF001
                code=code,
            )
        except PromoCodeError:
            self._clear_active_promo(telegram_user_id)
            return None
        if result.effect_type != PromoEffectType.discount_percent:
            self._clear_active_promo(telegram_user_id)
            return None
        return result

    def _active_subscription_purchase_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        key_label = self._key_button_label(user.user_id, subscription)
        lines = [
            "Если Вы уже приняли решение, то можете продлить актуальный ключ или создать новый 👇",
        ]
        if notice:
            lines.extend(["", f"ℹ️ {notice}"])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(
                [
                    [self._callback_button(key_label, "key:open", style="primary")],
                    [self._callback_button("🆕 Создать новый ключ", "key:create", style="success")],
                    [self._callback_button("Ввести промокод", "promo:enter", style="primary")],
                    [self._callback_button("💠 Главное меню", "menu:home", style="primary")],
                ]
            ),
            edit_message=edit_message,
            photo=self._plans_photo_source(),
            callback_notice=notice,
        )

    def _keys_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        if not subscription.can_use_vpn:
            return self._plans_response(
                telegram_user_id,
                edit_message=edit_message,
                notice="У вас пока нет активного ключа.",
            )

        key_name = self._key_short_name(user.user_id)
        days_left = self._subscription_days_left(subscription)
        if days_left is not None and days_left <= 1:
            text = (
                "⚠️ Завтра закончится подписка на VPN!\n\n"
                "✔️ Невероятная скорость\n"
                "✔️ Обход блокировок\n"
                "✔️ Круглосуточная поддержка\n"
                "✔️ Без рекламы\n"
                "И всё это - по цене чашки кофе.\n\n"
                "🔐 Продлите подписку, чтобы мы и дальше могли радовать вас качеством!"
            )
            first_button = f"«🔐 Продлить» {key_name} (vless)"
            first_callback = "key:extend"
        else:
            text = "Ваши действующие ключи 👇"
            first_button = self._key_button_label(user.user_id, subscription)
            first_callback = "key:open"

        if notice:
            text += f"\n\nℹ️ {notice}"
        return BotResponse(
            text=text,
            reply_markup=self._keyboard(
                [
                    [self._callback_button(first_button, first_callback, style="primary")],
                    [self._callback_button("🆕 Создать новый ключ", "key:create", style="success")],
                    [self._callback_button("💠 Главное меню", "menu:home", style="primary")],
                ]
            ),
            edit_message=edit_message,
            callback_notice=notice,
        )

    def _create_order_response(
        self,
        *,
        telegram_user_id: int,
        plan_id: str,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        promo_code = self._get_active_promo(telegram_user_id)
        try:
            order = self._facade.create_order(  # noqa: SLF001
                self._facade._claims_for_user(user),
                plan_id=plan_id,
                promo_code=promo_code,
            )
        except PromoCodeError as exc:
            self._clear_active_promo(telegram_user_id)
            return self._plans_response(
                telegram_user_id,
                edit_message=True,
                notice=f"Промокод не применен: {exc}",
            )
        except ValueError as exc:
            return self._plans_response(
                telegram_user_id,
                edit_message=True,
                notice=str(exc),
            )
        if promo_code:
            self._clear_active_promo(telegram_user_id)
        self.notify_user_new_order(telegram_user_id=telegram_user_id, order=order)
        notice = (
            "Ссылка на оплату создана."
            if self._payments_enabled()
            else "Заказ создан."
        )
        return self._order_details_response(order, notice=notice, edit_message=True)

    def _apply_promo_response(
        self,
        *,
        telegram_user_id: int,
        code: str,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        normalized_code = code.strip()
        if not normalized_code:
            self._set_pending_action(telegram_user_id, "apply_promo")
            return self._promo_entry_response(notice="Отправьте промокод текстом.")
        try:
            result = self._facade.apply_promo(
                self._facade._claims_for_user(user),  # noqa: SLF001
                code=normalized_code,
            )
        except PromoCodeError as exc:
            self._clear_active_promo(telegram_user_id)
            self._set_pending_action(telegram_user_id, "apply_promo")
            return self._promo_entry_response(notice=str(exc))

        if result.effect_type == PromoEffectType.free_subscription:
            self._clear_active_promo(telegram_user_id)
            self.deliver_user_access(
                user_id=user.user_id,
                telegram_user_id=telegram_user_id,
                include_android_apk=True,
                include_android_code=False,
            )
            return self._home_response(
                telegram_user_id,
                notice=f"{result.message} Ниже отправил подключение.",
            )

        self._set_active_promo(telegram_user_id, result.promo_code)
        return self._plans_response(
            telegram_user_id,
            notice="Промокод применен. Выберите тариф со скидкой.",
        )

    def _mark_order_paid_response(
        self,
        *,
        telegram_user_id: int,
        order_id: str,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        order = self._facade.mark_order_paid(
            self._facade._claims_for_user(user),  # noqa: SLF001
            order_id=order_id,
        )
        self.notify_admins_order_paid(order=order, user=user)
        return self._order_details_response(
            order,
            notice="Заказ отправлен на подтверждение администратору.",
            edit_message=True,
        )

    def _check_order_payment_response(
        self,
        *,
        telegram_user_id: int,
        order_id: str,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        try:
            record = self._facade.store.get_subscription_order(order_id)
            if record.user_id != user.user_id:
                raise PermissionError("Этот заказ принадлежит другому пользователю.")
            order, activated = self._facade.check_yookassa_order(order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            return BotResponse(
                text=f"❌ Не удалось проверить оплату: {exc}",
                reply_markup=self._keyboard(
                    [[self._callback_button("↩️ К тарифам", "menu:subscription")]],
                ),
                edit_message=True,
                callback_notice="Ошибка проверки.",
            )
        if activated:
            self.notify_user_order_approved(order_id=order.order_id, order=order)
            self.deliver_order_access(order_id=order.order_id)
            return self._home_response(
                telegram_user_id,
                edit_message=True,
                notice="Оплата подтверждена. Доступ уже активирован.",
            )
        return self._order_details_response(
            order,
            notice="Платеж пока не подтвержден." if order.status.value == "pending_payment" else None,
            edit_message=True,
        )

    def _cancel_order_response(
        self,
        *,
        telegram_user_id: int,
        order_id: str,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        try:
            order = self._facade.cancel_order(
                self._facade._claims_for_user(user),  # noqa: SLF001
                order_id=order_id,
            )
        except (KeyError, PermissionError, ValueError) as exc:
            return BotResponse(
                text=f"\u274c Не удалось отменить заказ: {exc}",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 К подписке", "menu:subscription")]],
                ),
                edit_message=True,
                callback_notice="Ошибка отмены.",
            )
        self.delete_order_invoice(order_id=order.order_id, clear_metadata=True)
        return BotResponse(
            text=f"\U0001f6ab Заказ {order_id} отменён.",
            reply_markup=self._keyboard(
                [[self._callback_button("\U0001f519 К подписке", "menu:subscription")]],
            ),
            edit_message=True,
            callback_notice="Заказ отменён.",
        )

    def _order_details_response(
        self,
        order: SubscriptionOrder,
        *,
        notice: str | None = None,
        edit_message: bool,
    ) -> BotResponse:
        order_emoji = {"pending_payment": "\u23f3", "waiting_confirmation": "\U0001f504", "confirmed": "\u2705", "rejected": "\u274c", "canceled": "\U0001f6ab"}.get(order.status.value, "\U0001f4cb")
        delivery_platform = self._order_delivery_platform(order.order_id)
        lines = ["Оплата", "", f"Тариф: {order.plan_title}"]
        if order.promo_code and order.discount_amount_rub:
            lines.extend(
                [
                    f"Промокод: {order.promo_code}",
                    f"Скидка: {self._format_rub(order.discount_amount_rub)}",
                    f"Сумма: {self._format_rub(order.amount_rub)}",
                ]
            )
        else:
            lines.append(f"Сумма: {self._format_rub(order.amount_rub)}")
        if order.status_message:
            lines.append(f"{order_emoji} {order.status_message}")
        else:
            lines.append(f"{order_emoji} Статус: {self._order_status_label(order)}")
        if self._payments_enabled():
            lines.extend(
                [
                    "",
                    self._telegram_payment_hint(order.status.value, platform=delivery_platform),
                ]
            )
        else:
            lines.extend(["", "Как оплатить:", order.payment_instructions])
        if notice:
            lines.extend(["", f"\u2139\ufe0f {notice}"])
        rows: list[list[dict[str, object]]] = []
        if order.status.value == "pending_payment":
            if self._payments_enabled():
                if order.payment_url:
                    rows.append([self._url_button("💸 Оплатить счет", order.payment_url)])
                rows.append([self._callback_button("🔄 Проверить оплату", f"order:check:{order.order_id}")])
                rows.append([self._callback_button("❌ Отменить счет", f"order:cancel:{order.order_id}")])
            else:
                rows.append([
                    self._callback_button("Я оплатил", f"order:paid:{order.order_id}"),
                    self._callback_button("Отменить", f"order:cancel:{order.order_id}"),
                ])
        if order.status.value in {"rejected", "canceled"}:
            rows.append([self._callback_button("Выбрать другой тариф", "menu:subscription")])
        rows.append([self._callback_button("\U0001f519 К тарифам", "menu:subscription")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
            callback_notice=notice,
        )

    def _traffic_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        limit_text = self._format_bytes(subscription.traffic_limit_bytes) if subscription.traffic_limit_bytes is not None else '\u221e Без лимита'
        text = (
            "Трафик\n\n"
            f"Использовано сейчас: {self._format_bytes(subscription.used_traffic_bytes)}\n"
            f"За все время: {self._format_bytes(subscription.lifetime_used_traffic_bytes)}\n"
            f"Лимит: {limit_text}"
        )
        return BotResponse(
            text=text,
            reply_markup=self._keyboard(
                [[self._callback_button("\U0001f519 Назад", "menu:home")]],
            ),
            edit_message=edit_message,
        )

    def _devices_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        devices = self._facade.list_user_devices(user.user_id)
        if not devices:
            text = "Устройства\n\nПока нет привязанных устройств."
        else:
            lines = ["Устройства", ""]
            for device in devices:
                lines.append(
                    f"{device.label} ({device.platform})\nПоследняя активность: {device.last_seen_at:%d.%m.%Y %H:%M}"
                )
            text = "\n".join(lines)
        return BotResponse(
            text=text,
            reply_markup=self._keyboard(
                [[self._callback_button("\U0001f519 Назад", "menu:home")]],
            ),
            edit_message=edit_message,
        )

    def _support_response(self, *, edit_message: bool = False) -> BotResponse:
        return BotResponse(
            text=(
                "Поддержка\n\n"
                "Если не проходит оплата, не работает подключение или пропал доступ, "
                "напишите нам. Поможем быстро.\n\n"
                "Пользовательское соглашение и политика конфиденциальности всегда доступны ниже."
            ),
            reply_markup=self._keyboard(
                [
                    [self._url_button("Открыть поддержку", self._settings.support_url)],
                    [self._url_button("Пользовательское соглашение", self._settings.legal_user_agreement_url)],
                    [self._url_button("Политика конфиденциальности", self._settings.legal_privacy_policy_url)],
                    [self._callback_button("\U0001f519 Назад", "menu:home")],
                ],
            ),
            edit_message=edit_message,
            photo=self._support_photo_source(),
        )

    def _share_response(
        self,
        telegram_user_id: int,
        *,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._require_registered_user(telegram_user_id)
        referral_url = self._facade.get_referral_url(user.user_id)
        invited_count = self._facade.get_referral_count(user.user_id)
        return BotResponse(
            text=(
                "Поделиться с другом\n\n"
                f"Ваша ссылка:\n{referral_url}\n\n"
                f"Перешли по ссылке: {invited_count}\n"
                "Бонусы за приглашения появятся позже, сейчас ссылка нужна для учета рекомендаций."
            ),
            reply_markup=self._keyboard(
                [
                    [self._copy_button("Скопировать ссылку", referral_url)],
                    [self._url_button("Открыть ссылку", referral_url)],
                    [self._callback_button("\U0001f519 Назад", "menu:home", style="primary")],
                ],
            ),
            edit_message=edit_message,
            photo=self._referral_photo_source(),
        )

    def _admin_home_response(self, *, edit_message: bool = False) -> BotResponse:
        return BotResponse(
            text=self._admin_home_text(),
            reply_markup=self._admin_keyboard(),
            edit_message=edit_message,
        )

    def _admin_home_text(self) -> str:
        orders = self._facade.list_orders_for_review(limit=20)
        catalog = self._facade.get_catalog_status()
        stats = self._facade.store.get_admin_dashboard_stats()
        waiting_confirmation = sum(1 for item in orders if item.status.value == "waiting_confirmation")
        pending_payment = sum(1 for item in orders if item.status.value == "pending_payment")
        return (
            "\u2699\ufe0f Админ-панель\n\n"
            f"\U0001f465 Пользователей: {stats['users_total']}\n"
            f"\u2705 Активных подписок: {stats['active_subscriptions']}\n"
            f"\U0001f4b0 Выручка подтверждена: {self._format_rub(stats['confirmed_revenue_rub'])}\n\n"
            f"\U0001f504 На подтверждении: {waiting_confirmation}\n"
            f"\u23f3 Ожидают оплаты: {pending_payment}\n\n"
            f"\U0001f517 Источников: {catalog.external_sources}\n"
            f"\U0001f5a5 Опубликовано серверов: {catalog.published_entries}\n"
            f"\U0001f648 Скрыто: {catalog.hidden_entries}\n"
            f"\u26d4\ufe0f Отключено: {catalog.disabled_entries}\n\n"
            "Выберите действие:"
        )

    def _admin_orders_response(self, *, edit_message: bool = False) -> BotResponse:
        orders = self._facade.list_orders_for_review(limit=12)
        lines = ["Заказы"]
        rows: list[list[dict[str, object]]] = []
        if not orders:
            lines.append("Активных заказов на оплату и подтверждение нет.")
        for order in orders:
            lines.append(
                f"• {order.order_id} | {order.plan_title} | {self._order_status_label(order)}"
            )
            rows.append(
                [self._callback_button(f"{order.order_id} · {order.plan_title}", f"admin:order:{order.order_id}")]
            )
        rows.append([self._callback_button("\U0001f519 Назад", "admin:home")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
        )

    def _admin_order_response(self, order_id: str, *, edit_message: bool = False) -> BotResponse:
        order = self._facade.get_order(order_id)
        lines = [
            "Карточка заказа",
            f"ID: {order.order_id}",
            f"Тариф: {order.plan_title}",
            f"Сумма: {order.amount_rub} RUB",
            f"Статус: {self._order_status_label(order)}",
            f"Создан: {order.created_at:%d.%m.%Y %H:%M}",
        ]
        rows: list[list[dict[str, object]]] = []
        if order.status.value == "waiting_confirmation":
            rows.append(
                [
                    self._callback_button("Подтвердить", f"admin:approve:{order.order_id}"),
                    self._callback_button("Отклонить", f"admin:reject:{order.order_id}"),
                ],
            )
        elif order.status.value == "pending_payment":
            rows.append([self._callback_button("Отклонить", f"admin:reject:{order.order_id}")])
        rows.append([self._callback_button("\U0001f519 Назад", "admin:orders")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
        )

    def _approve_order_response(self, *, order_id: str, admin_actor: str) -> BotResponse:
        order = self._facade.approve_order(order_id=order_id, admin_actor=admin_actor)
        self.notify_user_order_approved(order_id=order_id, order=order)
        self.deliver_order_access(order_id=order.order_id)
        return BotResponse(
            text=(
                f"Заказ {order.order_id} подтвержден.\n"
                f"Тариф: {order.plan_title}\n"
                f"Статус: {self._order_status_label(order)}"
            ),
            reply_markup=self._keyboard(
                [
                    [self._callback_button("Открыть заказ", f"admin:order:{order.order_id}")],
                    [self._callback_button("\U0001f519 Назад", "admin:orders")],
                ],
            ),
            edit_message=True,
            callback_notice="Подписка выдана.",
        )

    def _reject_order_response(self, *, order_id: str, admin_actor: str) -> BotResponse:
        order = self._facade.reject_order(order_id=order_id, admin_actor=admin_actor)
        self.notify_user_order_rejected(order_id=order_id)
        return BotResponse(
            text=(
                f"Заказ {order.order_id} отклонен.\n"
                f"Тариф: {order.plan_title}\n"
                f"Статус: {self._order_status_label(order)}"
            ),
            reply_markup=self._keyboard(
                [[self._callback_button("\U0001f519 Назад", "admin:orders")]],
            ),
            edit_message=True,
            callback_notice="Заказ отклонен.",
        )

    def _admin_search_results_response(self, query: str) -> BotResponse:
        users = self._facade.search_users(query)
        lines = [f"Результаты поиска: {query}"]
        rows: list[list[dict[str, object]]] = []
        if not users:
            lines.append("Ничего не найдено.")
        for user in users:
            lines.append(f"• {user.display_name} ({user.telegram_handle})")
            rows.append([self._callback_button(user.display_name, f"admin:user:{user.user_id}")])
        rows.append([self._callback_button("\U0001f519 Назад", "admin:home")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
        )

    def _admin_users_response(
        self,
        *,
        view: str,
        offset: int,
        edit_message: bool = False,
    ) -> BotResponse:
        page_size = 10
        users, total = self._facade.list_users_page(limit=page_size, offset=offset, view=view)
        page = (offset // page_size) + 1
        total_pages = max((total + page_size - 1) // page_size, 1)
        lines = [
            "Пользователи",
            "",
            f"Фильтр: {self._admin_users_filter_label(view)}",
            f"Страница: {page}/{total_pages}",
            f"Всего: {total}",
        ]
        rows: list[list[dict[str, object]]] = [
            [
                self._callback_button("Все", "admin:users:view:all:0"),
                self._callback_button("Активные", "admin:users:view:active:0"),
            ],
            [
                self._callback_button("Без подписки", "admin:users:view:no_access:0"),
                self._callback_button("Заблокированные", "admin:users:view:blocked:0"),
            ],
        ]
        if not users:
            lines.extend(["", "По этому фильтру пользователей нет."])
        for user in users:
            subscription = self._facade.get_user_subscription_by_id(user.user_id)
            lines.append(
                f"• {user.display_name} — {self._subscription_status_label(subscription)}"
            )
            rows.append(
                [
                    self._callback_button(
                        f"{user.display_name} · {self._platform_title(user.preferred_platform)}",
                        f"admin:user:{user.user_id}:{view}:{offset}",
                    )
                ]
            )
        nav_row: list[dict[str, str]] = []
        if offset > 0:
            prev_offset = max(offset - page_size, 0)
            nav_row.append(
                self._callback_button("Назад", f"admin:users:view:{view}:{prev_offset}")
            )
        if offset + len(users) < total:
            next_offset = offset + page_size
            nav_row.append(
                self._callback_button("Вперед", f"admin:users:view:{view}:{next_offset}")
            )
        if nav_row:
            rows.append(nav_row)
        rows.append([self._callback_button("\U0001f519 Назад", "admin:home")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
        )

    def _admin_referrals_response(self, *, edit_message: bool = False) -> BotResponse:
        summaries = self._facade.list_referral_leaderboard(limit=10)
        lines = ["🤝 Рефералы", ""]
        rows: list[list[dict[str, object]]] = []
        if not summaries:
            lines.append("Рефералов пока нет.")
        for index, summary in enumerate(summaries, start=1):
            user = self._facade.get_user(summary.user_id)
            lines.append(
                (
                    f"{index}. {user.display_name} ({user.telegram_handle})\n"
                    f"   Привел: {summary.invited_total}, оплатили: {summary.paid_total}, "
                    f"активный бонус: +{summary.bonus_days_active} дн., "
                    f"отозвано: {summary.bonus_days_revoked} дн., pending: {summary.pending_total}"
                )
            )
            rows.append(
                [
                    self._callback_button(
                        user.display_name,
                        f"admin:referrals:user:{summary.user_id}",
                    )
                ]
            )
        rows.append([self._callback_button("\U0001f519 Назад", "admin:home")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
        )

    def _admin_referral_user_response(
        self,
        *,
        user_id: str,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._facade.get_user(user_id)
        summary = self._facade.get_referral_summary(user_id)
        rewards = self._facade.list_referral_rewards(inviter_user_id=user_id, limit=10)
        lines = [
            f"🤝 Рефералы: {user.display_name}",
            "",
            f"Всего приглашено: {summary.invited_total}",
            f"Оплатили: {summary.paid_total}",
            f"Наград всего: {summary.reward_total}",
            f"Активных наград: {summary.active_reward_total}",
            f"Активный бонус: +{summary.bonus_days_active} дн.",
            f"Отозвано: {summary.bonus_days_revoked} дн.",
            f"Pending после лимита: {summary.pending_total}",
        ]
        rows: list[list[dict[str, object]]] = [
            [
                self._callback_button("+3 дня", f"admin:adjustdays:{user_id}:3", style="success"),
                self._callback_button("-3 дня", f"admin:adjustdays:{user_id}:-3", style="danger"),
            ],
        ]
        active_rewards = [reward for reward in rewards if reward.revoked_at is None]
        if active_rewards:
            lines.extend(["", "Последние активные награды:"])
            for reward in active_rewards[:5]:
                lines.append(
                    f"• #{reward.reward_id}: +{reward.bonus_days} дн., trigger {reward.trigger_type}"
                )
                rows.append(
                    [
                        self._callback_button(
                            f"Отозвать бонус #{reward.reward_id}",
                            f"admin:reward:revoke:{reward.reward_id}:{user_id}",
                            style="danger",
                        )
                    ]
                )
        rows.append([self._callback_button("Открыть пользователя", f"admin:user:{user_id}", style="primary")])
        rows.append([self._callback_button("\U0001f519 Назад", "admin:referrals")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
        )

    def _sources_response(
        self,
        *,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        sources = self._facade.list_external_sources(limit=20)
        lines = [
            "\U0001f517 Источники серверов",
            "",
            "Здесь можно открыть источник, пересинхронизировать его или удалить вместе со всеми серверами.",
        ]
        if not sources:
            lines.extend(["", "Источники пока не загружены."])
        else:
            for source in sources:
                lines.append(
                    (
                        f"• {source.title} [{self._source_kind_label(source.kind)}]\n"
                        f"  ID: {source.source_id} | всего: {source.entry_count} | "
                        f"опубл: {source.published_count} | скрыто: {source.hidden_count} | "
                        f"выкл: {source.disabled_count}\n"
                        f"  Статус: {self._source_status_label(source.last_error)}"
                    )
                )
        if notice:
            lines.extend(["", f"\u2139\ufe0f {notice}"])

        rows: list[list[dict[str, object]]] = [
            [
                self._callback_button("Добавить raw-ссылки", "admin:add:links"),
                self._callback_button("Добавить subscription URL", "admin:add:subscription"),
            ],
            [self._callback_button("Открыть каталог серверов", "admin:catalog")],
        ]
        for source in sources:
            rows.append(
                [
                    self._callback_button(
                        f"{self._short_label(source.title)} · {source.entry_count}",
                        f"admin:source:{source.source_id}",
                    )
                ]
            )
        rows.append([self._callback_button("\U0001f519 Назад", "admin:home")])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
            callback_notice=notice,
        )

    def _source_response(
        self,
        *,
        source_id: str,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        try:
            source = self._facade.get_external_source(source_id)
        except KeyError:
            return self._sources_response(
                edit_message=edit_message,
                notice="Источник уже не найден.",
            )

        lines = [
            "\U0001f4e5 Источник",
            f"Название: {source.title}",
            f"ID: {source.source_id}",
            f"Тип: {self._source_kind_label(source.kind)}",
            f"Добавил: {source.created_by}",
            f"Создан: {source.created_at:%d.%m.%Y %H:%M}",
            f"Последняя синхронизация: {self._format_datetime(source.last_synced_at)}",
            f"Всего серверов: {source.entry_count}",
            (
                "Состояния: "
                f"опубликовано {source.published_count}, "
                f"скрыто {source.hidden_count}, "
                f"отключено {source.disabled_count}"
            ),
            f"Статус: {self._source_status_label(source.last_error)}",
            f"Исходные данные: {self._clip_text(source.source_text, limit=220)}",
        ]
        if notice:
            lines.extend(["", f"\u2139\ufe0f {notice}"])
        rows = [
            [
                self._callback_button("Обновить источник", f"admin:source:sync:{source.source_id}", style="success"),
                self._callback_button(
                    "Сервера источника",
                    f"admin:catalog:view:{source.source_id}:0",
                    style="primary",
                ),
            ],
            [self._callback_button("Переименовать источник", f"admin:source:rename:{source.source_id}", style="primary")],
            [self._callback_button("Удалить источник и все его сервера", f"admin:source:delete:confirm:{source.source_id}", style="danger")],
            [self._callback_button("\U0001f519 Назад", "admin:sources", style="primary")],
        ]
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
            callback_notice=notice,
        )

    def _sync_source_response(
        self,
        *,
        source_id: str,
        edit_message: bool = False,
    ) -> BotResponse:
        try:
            summary = self._facade.sync_external_source(source_id)
        except KeyError:
            return self._sources_response(
                edit_message=edit_message,
                notice="Источник уже удален.",
            )
        except Exception as exc:  # noqa: BLE001
            return self._source_response(
                source_id=source_id,
                edit_message=edit_message,
                notice=f"Синхронизация не удалась: {exc}",
            )
        return self._source_response(
            source_id=summary.source.source_id,
            edit_message=edit_message,
            notice=(
                f"Синхронизация завершена. Импортировано: {summary.imported_count}. "
                f"Push: {summary.delivered_push_count}."
            ),
        )

    def _delete_source_confirm_response(
        self,
        *,
        source_id: str,
        edit_message: bool = False,
    ) -> BotResponse:
        try:
            source = self._facade.get_external_source(source_id)
        except KeyError:
            return self._sources_response(
                edit_message=edit_message,
                notice="Источник уже удален.",
            )

        return BotResponse(
            text=(
                "Удаление источника\n\n"
                f"Название: {source.title}\n"
                f"ID: {source.source_id}\n"
                f"Будут удалены источник и все привязанные к нему сервера: {source.entry_count} шт.\n"
                "После удаления эти сервера полностью пропадут из backend-каталога."
            ),
            reply_markup=self._keyboard(
                [
                    [self._callback_button("Да, удалить источник", f"admin:source:delete:{source.source_id}")],
                    [self._callback_button("\U0001f519 Назад", f"admin:source:{source.source_id}")],
                ],
            ),
            edit_message=edit_message,
            callback_notice="Подтвердите удаление источника.",
        )

    def _delete_source_response(
        self,
        *,
        source_id: str,
        edit_message: bool = False,
    ) -> BotResponse:
        try:
            deleted = self._facade.delete_external_source(source_id)
        except KeyError:
            return self._sources_response(
                edit_message=edit_message,
                notice="Источник уже удален.",
            )
        return self._sources_response(
            edit_message=edit_message,
            notice=(
                f"Источник {deleted.title} удален. "
                f"Из backend-каталога убрано серверов: {deleted.entry_count}."
            ),
        )

    def _catalog_response(
        self,
        *,
        source_id: str | None = None,
        offset: int = 0,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        page_size = self._catalog_page_size()
        source = None
        if source_id is not None:
            try:
                source = self._facade.get_external_source(source_id)
            except KeyError:
                return self._sources_response(
                    edit_message=edit_message,
                    notice="Источник уже удален.",
                )

        entries = self._facade.list_catalog_entries(
            limit=page_size + 1,
            offset=offset,
            source_id=source_id,
        )
        has_next = len(entries) > page_size
        page_entries = entries[:page_size]

        title = "\U0001f5a5 Каталог серверов"
        if source is not None:
            title = f"\U0001f5a5 Сервера источника {source.title}"
        lines = [title]
        shown_range = (
            f"{offset + 1}-{offset + len(page_entries)}"
            if page_entries
            else "0"
        )
        if source is not None:
            lines.extend(
                [
                    f"Источник: {source.source_id}",
                    f"Показаны записи: {shown_range}",
                ]
            )
        elif page_entries:
            lines.append(f"Показаны записи: {offset + 1}-{offset + len(page_entries)}")

        if not page_entries:
            lines.extend(["", "Серверов в этом разделе сейчас нет."])
        else:
            for entry in page_entries:
                lines.append(
                    (
                        f"• {entry.location.display_name} | {entry.location.protocol.upper()} | "
                        f"{entry.location.host}:{entry.location.port}\n"
                        f"  ID: {entry.entry_id} | {self._publish_state_label(entry.publish_state)} | "
                        f"источник: {entry.source_title}"
                    )
                )
        if notice:
            lines.extend(["", f"\u2139\ufe0f {notice}"])

        scope_token = self._catalog_scope_token(source_id)
        rows: list[list[dict[str, object]]] = []
        if source is None:
            rows.append([self._callback_button("Источники", "admin:sources")])
        else:
            rows.append([self._callback_button("Открыть источник", f"admin:source:{source.source_id}")])
        for entry in page_entries:
            rows.append(
                [
                    self._callback_button(
                        f"{self._short_label(entry.location.display_name)} · {entry.location.host}:{entry.location.port}",
                        f"admin:entry:{entry.entry_id}:{scope_token}:{offset}",
                    )
                ]
            )

        nav_row: list[dict[str, str]] = []
        if offset > 0:
            nav_row.append(
                self._callback_button(
                    "Назад",
                    f"admin:catalog:view:{scope_token}:{max(offset - page_size, 0)}",
                )
            )
        if has_next:
            nav_row.append(
                self._callback_button(
                    "Вперед",
                    f"admin:catalog:view:{scope_token}:{offset + page_size}",
                )
            )
        if nav_row:
            rows.append(nav_row)

        back_target = "admin:home" if source is None else f"admin:source:{source.source_id}"
        rows.append([self._callback_button("\U0001f519 Назад", back_target)])
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
            callback_notice=notice,
        )

    def _catalog_entry_response(
        self,
        *,
        entry_id: str,
        source_id: str | None,
        offset: int,
        edit_message: bool = False,
        notice: str | None = None,
    ) -> BotResponse:
        try:
            entry = self._facade.get_catalog_entry(entry_id)
        except KeyError:
            return self._catalog_response(
                source_id=source_id,
                offset=offset,
                edit_message=edit_message,
                notice="Сервер уже удален.",
            )

        scope_token = self._catalog_scope_token(source_id)
        lines = [
            "\U0001f5a5 Сервер",
            f"Название: {entry.location.display_name}",
            f"ID: {entry.entry_id}",
            f"Протокол: {entry.location.protocol.upper()}",
            f"Адрес: {entry.location.host}:{entry.location.port}",
            f"Сеть: {entry.location.network}",
            f"Безопасность: {entry.location.security_mode}",
            f"Нода: {entry.location.node_name}",
            f"Состояние: {self._publish_state_label(entry.publish_state)}",
            f"Источник: {entry.source_title} ({entry.source_id})",
        ]
        if notice:
            lines.extend(["", f"\u2139\ufe0f {notice}"])

        rows = [
            [
                self._callback_button("Опубликовать", f"admin:entry:state:{entry.entry_id}:published:{scope_token}:{offset}", style="success"),
                self._callback_button("Скрыть", f"admin:entry:state:{entry.entry_id}:hidden:{scope_token}:{offset}", style="primary"),
            ],
            [
                self._callback_button("Отключить", f"admin:entry:state:{entry.entry_id}:disabled:{scope_token}:{offset}", style="danger"),
                self._callback_button("Переименовать", f"admin:entry:rename:{entry.entry_id}:{scope_token}:{offset}", style="primary"),
            ],
            [self._callback_button("Удалить сервер", f"admin:entry:delete:confirm:{entry.entry_id}:{scope_token}:{offset}", style="danger")],
            [self._callback_button("Открыть источник", f"admin:source:{entry.source_id}", style="primary")],
            [self._callback_button("\U0001f519 Назад", f"admin:catalog:view:{scope_token}:{offset}", style="primary")],
        ]
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
            callback_notice=notice,
        )

    def _delete_entry_confirm_response(
        self,
        *,
        entry_id: str,
        source_id: str | None,
        offset: int,
        edit_message: bool = False,
    ) -> BotResponse:
        try:
            entry = self._facade.get_catalog_entry(entry_id)
        except KeyError:
            return self._catalog_response(
                source_id=source_id,
                offset=offset,
                edit_message=edit_message,
                notice="Сервер уже удален.",
            )

        scope_token = self._catalog_scope_token(source_id)
        return BotResponse(
            text=(
                "Удаление сервера\n\n"
                f"Название: {entry.location.display_name}\n"
                f"ID: {entry.entry_id}\n"
                f"Источник: {entry.source_title}\n"
                "После удаления сервер исчезнет из backend-каталога."
            ),
            reply_markup=self._keyboard(
                [
                    [self._callback_button("Да, удалить сервер", f"admin:entry:delete:{entry.entry_id}:{scope_token}:{offset}")],
                    [self._callback_button("\U0001f519 Назад", f"admin:entry:{entry.entry_id}:{scope_token}:{offset}")],
                ],
            ),
            edit_message=edit_message,
            callback_notice="Подтвердите удаление сервера.",
        )

    def _delete_entry_response(
        self,
        *,
        entry_id: str,
        source_id: str | None,
        offset: int,
        edit_message: bool = False,
    ) -> BotResponse:
        try:
            deleted = self._facade.delete_catalog_entry(entry_id)
        except KeyError:
            return self._catalog_response(
                source_id=source_id,
                offset=offset,
                edit_message=edit_message,
                notice="Сервер уже удален.",
            )

        page_size = self._catalog_page_size()
        target_offset = offset
        while target_offset > 0 and not self._facade.list_catalog_entries(
            limit=1,
            offset=target_offset,
            source_id=source_id,
        ):
            target_offset = max(target_offset - page_size, 0)

        return self._catalog_response(
            source_id=source_id,
            offset=target_offset,
            edit_message=edit_message,
            notice=f"Сервер {deleted.location.display_name} удален из backend-каталога.",
        )

    def _set_entry_state_response(
        self,
        *,
        entry_id: str,
        state: str,
        source_id: str | None,
        offset: int,
        edit_message: bool,
    ) -> BotResponse:
        try:
            updated = self._facade.set_catalog_entry_state(entry_id, state=state)
        except (KeyError, ValueError) as exc:
            return self._catalog_response(
                source_id=source_id,
                offset=offset,
                edit_message=edit_message,
                notice=f"Не удалось обновить сервер: {exc}",
            )
        return self._catalog_entry_response(
            entry_id=updated.entry_id,
            source_id=source_id,
            offset=offset,
            edit_message=edit_message,
            notice=f"Состояние обновлено: {self._publish_state_label(updated.publish_state)}.",
        )

    def _rename_source_response(self, *, source_id: str, title: str) -> BotResponse:
        try:
            updated = self._facade.update_external_source_title(source_id, title)
        except (KeyError, ValueError) as exc:
            return self._sources_response(notice=f"Не удалось переименовать источник: {exc}")
        return self._source_response(
            source_id=updated.source_id,
            notice=f"Источник переименован: {updated.title}.",
        )

    def _rename_entry_response(
        self,
        *,
        entry_id: str,
        display_name: str,
        source_id: str | None,
        offset: int,
    ) -> BotResponse:
        try:
            updated = self._facade.update_catalog_entry_display_name(
                entry_id,
                display_name=display_name,
            )
        except (KeyError, ValueError) as exc:
            return self._catalog_response(
                source_id=source_id,
                offset=offset,
                notice=f"Не удалось переименовать сервер: {exc}",
            )
        return self._catalog_entry_response(
            entry_id=updated.entry_id,
            source_id=source_id,
            offset=offset,
            notice=f"Сервер переименован: {updated.location.display_name}.",
        )

    def _add_source_response(
        self,
        *,
        kind: str,
        content: str,
        actor: str,
    ) -> BotResponse:
        try:
            summary = self._facade.add_external_source(
                kind=kind,
                source_text=content,
                created_by=actor,
            )
        except Exception as exc:  # noqa: BLE001
            return BotResponse(
                text=f"Не удалось загрузить источник.\n{exc}",
                reply_markup=self._keyboard(
                    [
                        [self._callback_button("К источникам", "admin:sources")],
                        [self._callback_button("\U0001f519 Назад", "admin:home")],
                    ],
                ),
            )

        warning_text = ""
        if summary.skipped_messages:
            warning_text = " Пропущено: " + "; ".join(summary.skipped_messages[:3])
        return self._source_response(
            source_id=summary.source.source_id,
            notice=(
                f"Источник синхронизирован. Импортировано: {summary.imported_count}. "
                f"Push: {summary.delivered_push_count}.{warning_text}"
            ).strip(),
        )

    def _set_user_blocked_response(self, *, user_id: str, blocked: bool) -> BotResponse:
        user = self._facade.set_user_blocked(user_id, blocked)
        status = "заблокирован" if user.blocked else "разблокирован"
        return BotResponse(
            text=f"Пользователь {user.display_name} ({user.user_id}) {status}.",
            reply_markup=self._admin_keyboard(),
        )

    def _admin_reset_devices_response(
        self,
        *,
        user_id: str,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._facade.get_user(user_id)
        removed = self._facade.reset_subscription_devices(user_id)
        return BotResponse(
            text=(
                f"Устройства subscription-ссылки сброшены для {user.display_name}.\n"
                f"Удалено слотов: {removed}."
            ),
            reply_markup=self._keyboard(
                [[self._callback_button("Открыть пользователя", f"admin:user:{user_id}", style="primary")]],
            ),
            edit_message=edit_message,
            callback_notice="Устройства сброшены.",
        )

    def _admin_adjust_days_response(
        self,
        *,
        user_id: str,
        delta_days: int,
        actor: str,
        edit_message: bool = False,
    ) -> BotResponse:
        user = self._facade.get_user(user_id)
        subscription = self._facade.adjust_subscription_days(
            user_id=user_id,
            delta_days=delta_days,
            reason="admin_adjustment",
            actor=actor,
        )
        sign = "+" if delta_days > 0 else ""
        expiry = (
            f"{subscription.expires_at:%d.%m.%Y %H:%M}"
            if subscription.expires_at is not None
            else "нет"
        )
        return BotResponse(
            text=(
                f"Дни подписки изменены для {user.display_name}: {sign}{delta_days}.\n"
                f"Статус: {self._subscription_status_label(self._facade.get_user_subscription_by_id(user_id))}\n"
                f"Срок действия: {expiry}"
            ),
            reply_markup=self._keyboard(
                [[self._callback_button("Открыть пользователя", f"admin:user:{user_id}", style="primary")]],
            ),
            edit_message=edit_message,
            callback_notice="Дни изменены.",
        )

    def _admin_revoke_referral_reward_response(
        self,
        *,
        reward_id: int,
        user_id: str,
        actor: str,
        edit_message: bool = False,
    ) -> BotResponse:
        reward = self._facade.revoke_referral_reward(reward_id=reward_id, actor=actor)
        return BotResponse(
            text=(
                f"Бонус #{reward.reward_id} отозван.\n"
                f"Снято дней: {reward.bonus_days}."
            ),
            reply_markup=self._keyboard(
                [[self._callback_button("К рефералам пользователя", f"admin:referrals:user:{user_id}", style="primary")]],
            ),
            edit_message=edit_message,
            callback_notice="Бонус отозван.",
        )

    def _send_subscription_link_response(
        self,
        *,
        user_id: str,
        rotate: bool,
    ) -> BotResponse:
        user = self._facade.get_user(user_id)
        subscription = self._facade.get_user_subscription_by_id(user_id)
        if not subscription.can_use_vpn:
            return BotResponse(
                text=(
                    f"У пользователя {user.display_name} нет активной подписки. "
                    "Сначала выдайте доступ или подтвердите заказ."
                ),
                reply_markup=self._keyboard(
                    [[self._callback_button("Открыть пользователя", f"admin:user:{user_id}", style="primary")]],
                ),
                edit_message=True,
                callback_notice="Нет активной подписки.",
            )
        subscription_url = (
            self._facade.rotate_public_subscription_url(user_id)
            if rotate
            else self._facade.get_public_subscription_url(user_id)
        )
        if user.telegram_user_id is not None:
            self._send_subscription_access(
                user_id=user_id,
                telegram_user_id=user.telegram_user_id,
            )
        action = "перевыпущена и отправлена" if rotate else "отправлена"
        return BotResponse(
            text=(
                f"Subscription-ссылка {action} пользователю {user.display_name}.\n\n"
                f"{subscription_url}"
            ),
            reply_markup=self._keyboard(
                [
                    [self._copy_button("Скопировать ссылку", subscription_url)],
                    [self._callback_button("Открыть пользователя", f"admin:user:{user_id}", style="primary")],
                ],
            ),
            edit_message=True,
            callback_notice="Ссылка отправлена.",
        )

    def _admin_user_response(
        self,
        user_id: str,
        *,
        edit_message: bool = False,
        back_view: str = "all",
        back_offset: int = 0,
    ) -> BotResponse:
        profile = self._facade.get_user_profile_by_id(user_id)
        subscription = self._facade.get_user_subscription_by_id(user_id)
        user = self._facade.get_user(user_id)
        referral_summary = self._facade.get_referral_summary(user_id)
        lines = [
            "Карточка пользователя",
            f"ID: {profile.user_id}",
            f"Имя: {profile.display_name}",
            f"Telegram: {profile.telegram_handle}",
            f"Email: {profile.email or 'Не указан'}",
            f"Блокировка: {'да' if user.blocked else 'нет'}",
            f"Статус: {self._subscription_status_label(subscription)}",
            f"Срок действия: {self._expiry_label(subscription)}",
            "Устройства: без лимита",
            (
                "Рефералы: "
                f"{referral_summary.invited_total} всего, "
                f"{referral_summary.paid_total} оплатили, "
                f"+{referral_summary.bonus_days_active} дн., "
                f"pending {referral_summary.pending_total}"
            ),
        ]
        rows = [
            [self._callback_button("Выдать 1 месяц", f"admin:grant:{profile.user_id}:monthly_1", style="success")],
            [self._callback_button("Выдать 3 месяца", f"admin:grant:{profile.user_id}:quarterly_3", style="success")],
            [self._callback_button("Выдать 6 месяцев", f"admin:grant:{profile.user_id}:semiannual_6", style="success")],
            [self._callback_button("Выдать 12 месяцев", f"admin:grant:{profile.user_id}:yearly_12", style="success")],
            [
                self._callback_button("+3 дня", f"admin:adjustdays:{profile.user_id}:3", style="success"),
                self._callback_button("-3 дня", f"admin:adjustdays:{profile.user_id}:-3", style="danger"),
            ],
            [
                self._callback_button("Отправить ссылку", f"admin:sendlink:{profile.user_id}", style="success"),
                self._callback_button("Перевыпустить ссылку", f"admin:rotatelink:{profile.user_id}", style="primary"),
            ],
            [self._callback_button("Рефералы", f"admin:referrals:user:{profile.user_id}", style="primary")],
            [self._callback_button("Снять подписку", f"admin:revoke:{profile.user_id}", style="danger")],
            [
                self._callback_button(
                    "Разблокировать" if user.blocked else "Заблокировать",
                    f"admin:{'unblock' if user.blocked else 'block'}:{profile.user_id}",
                    style="danger" if not user.blocked else "success",
                )
            ],
            [
                self._callback_button(
                    "Удалить из базы",
                    f"admin:delete:confirm:{profile.user_id}:{back_view}:{back_offset}",
                    style="danger",
                )
            ],
            [self._callback_button("\U0001f519 Назад", f"admin:users:view:{back_view}:{back_offset}", style="primary")],
        ]
        return BotResponse(
            text="\n".join(lines),
            reply_markup=self._keyboard(rows),
            edit_message=edit_message,
        )

    def _delete_user_confirm_response(
        self,
        *,
        user_id: str,
        edit_message: bool,
        back_view: str,
        back_offset: int,
    ) -> BotResponse:
        profile = self._facade.get_user_profile_by_id(user_id)
        return BotResponse(
            text=(
                "Удаление пользователя\n\n"
                f"Имя: {profile.display_name}\n"
                f"Telegram: {profile.telegram_handle}\n"
                "Будут удалены профиль, устройства, подписка, заказы и коды активации.\n"
                "После этого /start для него откроется как для нового пользователя."
            ),
            reply_markup=self._keyboard(
                [
                    [
                        self._callback_button(
                            "Да, удалить",
                            f"admin:delete:{user_id}:{back_view}:{back_offset}",
                        )
                    ],
                    [
                        self._callback_button(
                            "\U0001f519 Назад",
                            f"admin:user:{user_id}:{back_view}:{back_offset}",
                        )
                    ],
                ],
            ),
            edit_message=edit_message,
            callback_notice="Подтвердите удаление.",
        )

    def _delete_user_response(
        self,
        *,
        user_id: str,
        actor_telegram_user_id: int,
        edit_message: bool,
        back_view: str,
        back_offset: int,
    ) -> BotResponse:
        actor = self._require_registered_user(actor_telegram_user_id)
        if actor.user_id == user_id:
            return BotResponse(
                text="Нельзя удалить свой текущий админ-аккаунт через бота.",
                reply_markup=self._keyboard(
                    [[self._callback_button("\U0001f519 Назад", f"admin:user:{user_id}:{back_view}:{back_offset}")]],
                ),
                edit_message=edit_message,
                callback_notice="Самого себя удалять нельзя.",
            )

        orders = self._facade.store.list_subscription_orders(user_id=user_id, limit=100)
        for order in orders:
            self.delete_order_invoice(order_id=order.order_id, clear_metadata=False)

        deleted_user = self._facade.delete_user(user_id)
        if deleted_user.telegram_user_id is not None:
            with self._lock:
                self._pending_actions.pop(deleted_user.telegram_user_id, None)

        return BotResponse(
            text=(
                f"Пользователь {deleted_user.display_name} удален из базы.\n"
                "Если он снова напишет /start, бот покажет ему новый onboarding."
            ),
            reply_markup=self._keyboard(
                [[self._callback_button("\U0001f519 К списку пользователей", f"admin:users:view:{back_view}:{back_offset}")]],
            ),
            edit_message=edit_message,
            callback_notice="Пользователь удален.",
        )

    def _grant_subscription_response(
        self,
        *,
        user_id: str,
        plan_id: str,
        admin_actor: str,
    ) -> BotResponse:
        self._facade.grant_subscription(
            user_id=user_id,
            plan_id=plan_id,
            granted_by=admin_actor,
        )
        profile = self._facade.get_user_profile_by_id(user_id)
        subscription = self._facade.get_user_subscription_by_id(user_id)
        user = self._facade.get_user(user_id)
        if user.telegram_user_id is not None:
            self._send_subscription_access(
                user_id=user_id,
                telegram_user_id=user.telegram_user_id,
            )
        return BotResponse(
            text=(
                f"Подписка выдана пользователю {profile.display_name}.\n"
                f"Статус: {self._subscription_status_label(subscription)}\n"
                f"Срок действия: {self._expiry_label(subscription)}\n"
                "Если Telegram привязан, subscription-ссылка уже отправлена пользователю."
            ),
            reply_markup=self._keyboard(
                [
                    [self._callback_button("Открыть пользователя", f"admin:user:{user_id}")],
                    [self._callback_button("\U0001f519 Назад", "admin:home")],
                ],
            ),
            edit_message=True,
            callback_notice="Подписка выдана.",
        )

    def _revoke_subscription_response(
        self,
        *,
        user_id: str,
        admin_actor: str,
    ) -> BotResponse:
        self._facade.revoke_subscription(
            user_id=user_id,
            reason="admin_revoke",
            granted_by=admin_actor,
        )
        profile = self._facade.get_user_profile_by_id(user_id)
        subscription = self._facade.get_user_subscription_by_id(user_id)
        return BotResponse(
            text=(
                f"Подписка снята у пользователя {profile.display_name}.\n"
                f"Статус: {self._subscription_status_label(subscription)}\n"
                "Список серверов у пользователя будет скрыт до следующей оплаты."
            ),
            reply_markup=self._keyboard(
                [
                    [self._callback_button("Открыть пользователя", f"admin:user:{user_id}")],
                    [self._callback_button("\U0001f519 Назад", "admin:home")],
                ],
            ),
            edit_message=True,
            callback_notice="Подписка снята.",
        )

    def _require_registered_user(self, telegram_user_id: int):
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            raise KeyError("Telegram user is not linked to an app account.")
        return user

    def _ensure_registered_from_telegram(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None,
    ):
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        if user is not None:
            return user
        return self._facade.register_telegram_onboarding(
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            username=username,
            platform="subscription",
        )

    @staticmethod
    def _extract_referral_token(tail: str) -> str | None:
        payload = tail.strip()
        if not payload.startswith("ref_"):
            return None
        token = payload.removeprefix("ref_").strip()
        return token or None

    def _is_active(self, telegram_user_id: int) -> bool:
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            return False
        return user.role == "admin"

    def _legal_accepted(self, telegram_user_id: int) -> bool:
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        return user is not None and user.legal_accepted_at is not None

    def _has_active_access(self, telegram_user_id: int) -> bool:
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            return False
        subscription = self._facade.get_user_subscription_by_id(user.user_id)
        return subscription.can_use_vpn

    def _set_pending_action(
        self,
        telegram_user_id: int,
        action: str,
        *,
        params: dict[str, str] | None = None,
    ) -> None:
        with self._lock:
            self._pending_actions[telegram_user_id] = PendingBotAction(
                action=action,
                created_at=datetime.now(UTC),
                params=params or {},
            )

    def _set_active_promo(self, telegram_user_id: int, code: str) -> None:
        normalized_code = code.strip().upper()
        with self._lock:
            if normalized_code:
                self._active_promo_codes[telegram_user_id] = normalized_code
            else:
                self._active_promo_codes.pop(telegram_user_id, None)

    def _get_active_promo(self, telegram_user_id: int) -> str | None:
        with self._lock:
            return self._active_promo_codes.get(telegram_user_id)

    def _clear_active_promo(self, telegram_user_id: int) -> None:
        with self._lock:
            self._active_promo_codes.pop(telegram_user_id, None)

    def _set_pending_start_referral(
        self,
        telegram_user_id: int,
        referral_token: str | None,
    ) -> None:
        with self._lock:
            if referral_token:
                self._pending_start_referrals[telegram_user_id] = referral_token
            else:
                self._pending_start_referrals.pop(telegram_user_id, None)

    def _take_pending_start_referral(self, telegram_user_id: int) -> str | None:
        with self._lock:
            return self._pending_start_referrals.pop(telegram_user_id, None)

    def _clear_pending_action(self, telegram_user_id: int) -> None:
        with self._lock:
            self._pending_actions.pop(telegram_user_id, None)

    def _take_pending_action(self, telegram_user_id: int) -> PendingBotAction | None:
        with self._lock:
            pending = self._pending_actions.pop(telegram_user_id, None)
        if pending is None:
            return None
        if pending.created_at + timedelta(minutes=10) < datetime.now(UTC):
            return None
        return pending

    def _guest_keyboard(self) -> dict[str, object]:
        return self._home_keyboard(0, False, trial_available=True)

    def _home_keyboard(
        self,
        telegram_user_id: int,
        has_access: bool,
        *,
        trial_available: bool,
    ) -> dict[str, object]:
        rows: list[list[dict[str, object]]] = []
        if has_access:
            rows.append([self._callback_button("Подключить VPN", "delivery:send", style="success")])
        elif trial_available:
            rows.append([self._callback_button("3 дня бесплатно", "trial:activate", style="success")])
        rows.extend(
            [
                [self._callback_button("Купить подписку", "menu:subscription", style="success")],
                [
                    self._callback_button("Мои ключи", "menu:keys", style="success"),
                    self._callback_button("Помощь", "menu:support", style="success"),
                ],
                [
                    self._callback_button("Пригласить", "menu:share", style="success"),
                    self._callback_button("Личный кабинет", "menu:account", style="success"),
                ],
                [self._url_button("Скачать Happ", self._happ_download_url(), style="primary")],
            ]
        )
        return self._keyboard(rows)

    def _admin_keyboard(self) -> dict[str, object]:
        rows = [
            [
                self._callback_button("\U0001f465 Пользователи", "admin:users", style="success"),
                self._callback_button("\U0001f4cb Заказы", "admin:orders", style="success"),
            ],
            [
                self._callback_button("🤝 Рефералы", "admin:referrals", style="success"),
                self._callback_button("\U0001f50d Найти пользователя", "admin:search", style="primary"),
            ],
            [
                self._callback_button("\U0001f517 Источники", "admin:sources", style="success"),
                self._callback_button("\U0001f5a5 Серверы", "admin:catalog", style="success"),
            ],
            [
                self._callback_button("📣 Рассылка", "admin:broadcast:start", style="primary"),
            ],
            [
                self._callback_button("Добавить raw-ссылки", "admin:add:links"),
                self._callback_button("Добавить subscription URL", "admin:add:subscription"),
            ],
        ]
        if self._settings.admin_web_url.strip():
            rows.append(
                [self._url_button("\U0001f310 Веб-админка", self._settings.admin_web_url)],
            )
        rows.append([self._callback_button("\U0001f519 Назад", "menu:home", style="primary")])
        return self._keyboard(rows)

    @staticmethod
    def _subscription_status_label(subscription: SubscriptionState) -> str:
        return {
            SubscriptionStatus.active: "Активна",
            SubscriptionStatus.inactive: "Неактивна",
            SubscriptionStatus.revoked: "Отключена",
            SubscriptionStatus.expired: "Истекла",
        }[subscription.status]

    @staticmethod
    def _expiry_label(subscription: SubscriptionState) -> str:
        if subscription.expires_at is None:
            return "—"
        return subscription.expires_at.strftime("%d.%m.%Y")

    @staticmethod
    def _order_status_label(order: SubscriptionOrder) -> str:
        return {
            "pending_payment": "Ожидает оплаты",
            "waiting_confirmation": "Ждет подтверждения",
            "confirmed": "Подтвержден",
            "rejected": "Отклонен",
            "canceled": "Отменен",
        }.get(order.status.value, order.status.value)

    @staticmethod
    def _format_bytes(value: int | None) -> str:
        if value is None:
            return "—"
        units = ("B", "KB", "MB", "GB", "TB")
        size = float(value)
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        return f"{size:.2f} {units[unit_index]}"

    @staticmethod
    def _format_rub(value: int) -> str:
        return f"{value:,}".replace(",", " ") + " ₽"

    def _home_status_line(self, subscription: SubscriptionState) -> str:
        if subscription.can_use_vpn and subscription.expires_at is not None:
            return f"Доступ активен до {subscription.expires_at:%d.%m.%Y}"
        if subscription.can_use_vpn:
            return "Доступ активен"
        return "Доступ не активен"

    @staticmethod
    def _home_hint(
        *,
        subscription: SubscriptionState,
    ) -> str:
        if subscription.can_use_vpn:
            return "Можно продлить подписку или заново открыть подключение в Happ."
        return "Выберите тариф, оплатите его в Telegram и получите подключение для Happ."

    @staticmethod
    def _subscription_cta_label(has_access: bool) -> str:
        return "Продлить доступ" if has_access else "Выбрать тариф"

    def _plan_button_label(self, plan: Plan, *, discounted_price: int | None = None) -> str:
        price = plan.price_rub
        if discounted_price is not None and discounted_price < plan.price_rub:
            price = discounted_price
        labels = {
            "monthly_1": f"🟡1 мес - {self._format_rub(price)}",
            "quarterly_3": f"🔥3 мес - {self._format_rub(price)}",
            "semiannual_6": f"🟢6 мес - {self._format_rub(price)}",
            "yearly_12": f"🟣12 мес - {self._format_rub(price)}",
        }
        label = labels.get(plan.id, f"{plan.title} - {self._format_rub(price)}")
        if discounted_price is not None and discounted_price < plan.price_rub:
            label += f" вместо {self._format_rub(plan.price_rub)}"
        return label

    @staticmethod
    def _promo_applied_text(result: PromoApplyResult) -> str:
        if result.discount_percent:
            return f"🎟 Промокод {result.promo_code}: скидка {result.discount_percent}% учтена в ценах."
        return f"🎟 Промокод {result.promo_code} применен."

    def _telegram_payment_hint(self, status: str, *, platform: str) -> str:
        return {
            "pending_payment": self._payment_notice_for_platform(platform),
            "waiting_confirmation": "Платеж уже получен. Обычно доступ включается сразу после обработки.",
            "confirmed": self._delivery_sent_notice(platform),
            "rejected": "Заказ не был подтвержден. Можно выбрать другой тариф или обратиться в поддержку.",
            "canceled": "Заказ закрыт. При необходимости можно создать новый счет.",
        }.get(status, "Статус заказа обновлен.")

    @staticmethod
    def _platform_title(platform: str | None) -> str:
        if platform == "ios":
            return "iPhone / iPad"
        if platform == "android":
            return "Android"
        return "Не выбрана"

    def _platform_delivery_title(self, platform: str) -> str:
        return "Happ subscription"

    @staticmethod
    def _delivery_button_label(platform: str) -> str:
        return "Подключить VPN"

    def _subscription_delivery_hint(
        self,
        *,
        has_access: bool,
        trial_available: bool,
    ) -> str:
        if has_access:
            return "Если нужно, я могу еще раз отправить кнопку подключения для Happ."
        if trial_available:
            return "Можно начать с бесплатного trial на 3 дня. После активации я сразу пришлю кнопку подключения."
        return "После оплаты я пришлю кнопку «Подключить VPN» для Happ."

    @staticmethod
    def _platform_selected_notice(platform: str | None) -> str:
        return "Отлично, буду выдавать подключение для Happ."

    @staticmethod
    def _payment_notice_for_platform(platform: str) -> str:
        return "Откройте ссылку оплаты. После YooKassa-подтверждения я пришлю кнопку подключения для Happ."

    @staticmethod
    def _delivery_sent_notice(platform: str) -> str:
        return "Кнопка подключения уже отправлена ниже."

    def _service_name(self) -> str:
        return self._settings.vpn_service_name.strip() or "MeFast VPN"

    def _service_photo_source(self) -> str | None:
        configured = Path(self._settings.service_logo_path).expanduser()
        if configured.is_absolute() and configured.is_file():
            return str(configured)
        candidates = [
            Path.cwd() / configured,
            Path(__file__).resolve().parents[2] / configured,
            Path(__file__).resolve().parents[3] / configured,
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def _support_photo_source(self) -> str | None:
        candidates = [
            Path.cwd() / "support.png",
            Path(__file__).resolve().parents[2] / "support.png",
            Path(__file__).resolve().parents[3] / "support.png",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def _plans_photo_source(self) -> str | None:
        candidates = [
            Path.cwd() / "subscription.png",
            Path(__file__).resolve().parents[2] / "subscription.png",
            Path(__file__).resolve().parents[3] / "subscription.png",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def _cabinet_photo_source(self) -> str | None:
        candidates = [
            Path.cwd() / "cabinet.png",
            Path(__file__).resolve().parents[2] / "cabinet.png",
            Path(__file__).resolve().parents[3] / "cabinet.png",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def _referral_photo_source(self) -> str | None:
        candidates = [
            Path.cwd() / "referral.png",
            Path(__file__).resolve().parents[2] / "referral.png",
            Path(__file__).resolve().parents[3] / "referral.png",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def _key_short_name(self, user_id: str) -> str:
        try:
            user = self._facade.get_user(user_id)
            username = self._facade._claims_for_user(user).marzban_username  # noqa: SLF001
        except Exception:
            username = user_id
        suffix = username.rsplit("_", maxsplit=1)[-1] if "_" in username else username
        suffix = suffix.strip() or username
        return f"_{suffix[:11]}"

    def _key_button_label(self, user_id: str, subscription: SubscriptionState) -> str:
        days_left = self._subscription_days_left(subscription)
        days_label = "∞" if days_left is None else f"{max(days_left, 0)} день"
        return f"🔑 {self._key_short_name(user_id)} ({days_label}) (vless)"

    def _current_key_text(self, user_id: str) -> str:
        try:
            token = self._facade.get_public_subscription_url(user_id).rstrip("/").rsplit("/", 1)[-1]
            export = self._facade.build_public_subscription_export(token)
            first_line = next((line.strip() for line in export.body.splitlines() if line.strip()), "")
            if first_line:
                return first_line
        except Exception:
            pass
        return self._facade.get_public_subscription_url(user_id)

    @staticmethod
    def _subscription_days_left(subscription: SubscriptionState) -> int | None:
        if subscription.expires_at is None:
            return None
        expires_at = subscription.expires_at
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        delta = expires_at - now_naive
        seconds = max(delta.total_seconds(), 0)
        return max(1, int((seconds + 86_399) // 86_400)) if seconds > 0 else 0

    def _happ_download_url(self) -> str:
        return self._settings.happ_download_url.strip() or "https://www.happ.su/main"

    def _device_limit_label(self) -> str:
        limit = int(self._settings.max_devices_per_subscription or 0)
        return f"{limit} устройств" if limit > 0 else "без лимита"

    @staticmethod
    def _admin_users_filter_label(view: str) -> str:
        return {
            "all": "Все",
            "active": "Активные",
            "no_access": "Без подписки",
            "blocked": "Заблокированные",
        }.get(view, "Все")

    @staticmethod
    def _catalog_page_size() -> int:
        return 8

    @staticmethod
    def _catalog_scope_token(source_id: str | None) -> str:
        return source_id or "all"

    @staticmethod
    def _source_kind_label(kind: str) -> str:
        return {
            "raw_links": "raw links",
            "subscription_url": "subscription URL",
            "marzban_import": "Marzban import",
        }.get(kind, kind)

    @staticmethod
    def _publish_state_label(state: str) -> str:
        return {
            "published": "Опубликован",
            "hidden": "Скрыт",
            "disabled": "Отключен",
        }.get(state, state)

    @staticmethod
    def _source_status_label(last_error: str | None) -> str:
        if last_error:
            return f"ошибка: {last_error}"
        return "ok"

    @staticmethod
    def _format_datetime(value: datetime | None) -> str:
        if value is None:
            return "—"
        return value.strftime("%d.%m.%Y %H:%M")

    @staticmethod
    def _short_label(text: str, *, limit: int = 28) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(limit - 3, 1)].rstrip() + "..."

    @staticmethod
    def _clip_text(text: str, *, limit: int = 160) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(limit - 3, 1)].rstrip() + "..."

    def _trial_available(self, telegram_user_id: int) -> bool:
        user = self._facade.store.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            return False
        return user.trial_activated_at is None

    def _order_delivery_platform(self, order_id: str) -> str:
        try:
            record = self._facade.store.get_subscription_order(order_id)
        except KeyError:
            return "android"
        return self._facade.get_user_delivery_platform(record.user_id)

    @staticmethod
    def _keyboard(rows: list[list[dict[str, object]]]) -> dict[str, object]:
        return {"inline_keyboard": rows}

    @staticmethod
    def _has_button_emoji(text: str) -> bool:
        for char in text:
            codepoint = ord(char)
            if (
                0x2600 <= codepoint <= 0x27BF
                or 0x1F000 <= codepoint <= 0x1FAFF
                or codepoint == 0xFE0F
            ):
                return True
        return False

    @staticmethod
    def _decorate_button_text(
        text: str,
        *,
        callback_data: str | None = None,
        url: str | None = None,
        copy_text: bool = False,
    ) -> str:
        if not text.strip() or TelegramAdminService._has_button_emoji(text):
            return text

        lower_text = text.lower()
        payload = (callback_data or url or "").lower()

        if copy_text or "скоп" in lower_text:
            emoji = "📋"
        elif "назад" in lower_text or payload.endswith(":back"):
            emoji = "🔙"
        elif "главное" in lower_text or payload == "menu:home":
            emoji = "💠"
        elif "поддерж" in lower_text or payload == "menu:support":
            emoji = "🆘"
        elif "скач" in lower_text:
            emoji = "📥"
        elif "соглаш" in lower_text or "политик" in lower_text or "документ" in lower_text:
            emoji = "📄"
        elif payload.startswith("delivery:") or "подключ" in lower_text:
            emoji = "🔌"
        elif payload.startswith("trial:") or "бесплат" in lower_text:
            emoji = "🎁"
        elif (
            payload.startswith("sub:")
            or "подпис" in lower_text
            or "тариф" in lower_text
            or "оплат" in lower_text
        ):
            emoji = "💳"
        elif payload.startswith("key:") or "ключ" in lower_text:
            emoji = "🔑"
        elif (
            payload.startswith("profile:")
            or payload == "menu:account"
            or "кабинет" in lower_text
        ):
            emoji = "👤"
        elif payload == "menu:share" or "приглас" in lower_text or "ссылк" in lower_text:
            emoji = "🤝"
        elif payload.startswith("admin:users") or "пользовател" in lower_text:
            emoji = "👥"
        elif (
            payload.startswith("admin:orders")
            or payload.startswith("admin:order")
            or "заказ" in lower_text
            or "счет" in lower_text
        ):
            emoji = "📋"
        elif (
            payload.startswith("admin:sources")
            or payload.startswith("admin:source")
            or "источник" in lower_text
        ):
            emoji = "🔗"
        elif (
            payload.startswith("admin:catalog")
            or payload.startswith("admin:entry")
            or "сервер" in lower_text
        ):
            emoji = "🖥"
        elif payload.startswith("admin:search") or "найти" in lower_text or "поиск" in lower_text:
            emoji = "🔎"
        elif "добавить" in lower_text or payload.startswith("admin:add"):
            emoji = "➕"
        elif "подтверд" in lower_text or "опубликовать" in lower_text:
            emoji = "✅"
        elif "отклон" in lower_text or "отмен" in lower_text:
            emoji = "❌"
        elif "удал" in lower_text:
            emoji = "🗑️"
        elif "скры" in lower_text:
            emoji = "🙈"
        elif "отключ" in lower_text or "снять" in lower_text or "блок" in lower_text:
            emoji = "⛔"
        elif "выдать" in lower_text:
            emoji = "🎁"
        elif "откры" in lower_text:
            emoji = "📂" if callback_data else "🌐"
        else:
            emoji = "➡️"
        return f"{emoji} {text}"

    @staticmethod
    def _callback_button(
        text: str,
        callback_data: str,
        *,
        style: str | None = None,
    ) -> dict[str, object]:
        button: dict[str, object] = {
            "text": TelegramAdminService._decorate_button_text(
                text,
                callback_data=callback_data,
            ),
            "callback_data": callback_data,
        }
        if style is not None:
            button["style"] = style
        return button

    @staticmethod
    def _url_button(text: str, url: str, *, style: str | None = None) -> dict[str, object]:
        button: dict[str, object] = {
            "text": TelegramAdminService._decorate_button_text(text, url=url),
            "url": url,
        }
        if style is not None:
            button["style"] = style
        return button

    @staticmethod
    def _copy_button(text: str, value: str) -> dict[str, object]:
        return {
            "text": TelegramAdminService._decorate_button_text(text, copy_text=True),
            "copy_text": {"text": value},
            "style": "success",
        }

    @staticmethod
    def _actor_label(display_name: str, username: str | None) -> str:
        if username:
            return f"@{username}"
        return display_name
