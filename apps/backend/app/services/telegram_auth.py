from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from secrets import choice, token_urlsafe
from threading import Lock

import httpx

from app.config import Settings
from app.schemas.contracts import (
    AppSession,
    DeviceBinding,
    TelegramExchangeRequest,
    TelegramAuthFlowResponse,
    TelegramAuthStatus,
    TelegramAuthStatusResponse,
)
from app.services.telegram_admin import BotResponse, TelegramAdminService


@dataclass(slots=True)
class TelegramUser:
    telegram_id: int
    display_name: str
    username: str | None


@dataclass(slots=True)
class RegisteredUser:
    user_id: str
    telegram_handle: str
    display_name: str
    email: str | None
    is_premium: bool = False


@dataclass(slots=True)
class PendingAuthFlow:
    flow_id: str
    one_time_code: str
    auth_url: str
    device_id: str
    device_label: str
    platform: str
    created_at: datetime
    expires_at: datetime
    status: TelegramAuthStatus = TelegramAuthStatus.pending
    session: AppSession | None = None
    verified_at: datetime | None = None
    telegram_user_id: int | None = None
    relink_user_id: str | None = None


class TelegramBotGateway:
    def fetch_updates(self, *, offset: int | None) -> list[dict[str, object]]:
        raise NotImplementedError

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        raise NotImplementedError

    def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        raise NotImplementedError

    def edit_message_media(
        self,
        *,
        chat_id: int,
        message_id: int,
        media: dict[str, object],
        photo_path: str | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        raise NotImplementedError

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
    ) -> None:
        raise NotImplementedError

    def send_invoice(
        self,
        *,
        chat_id: int,
        title: str,
        description: str,
        payload: str,
        provider_token: str,
        currency: str,
        prices: list[dict[str, object]],
        start_parameter: str | None = None,
        provider_data: str | None = None,
        need_email: bool = False,
        send_email_to_provider: bool = False,
    ) -> dict[str, object]:
        raise NotImplementedError

    def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        raise NotImplementedError

    def delete_message(
        self,
        *,
        chat_id: int,
        message_id: int,
    ) -> None:
        raise NotImplementedError

    def send_document(
        self,
        *,
        chat_id: int,
        document: str,
        caption: str | None = None,
        filename: str | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise NotImplementedError

    def send_photo(
        self,
        *,
        chat_id: int,
        photo: str,
        caption: str | None = None,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, object]:
        raise NotImplementedError

    def copy_message(
        self,
        *,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise NotImplementedError


class HttpxTelegramBotGateway(TelegramBotGateway):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.Client(timeout=10.0)

    @property
    def _api_base_url(self) -> str:
        return f"https://api.telegram.org/bot{self._settings.telegram_bot_token}"

    def fetch_updates(self, *, offset: int | None) -> list[dict[str, object]]:
        params: dict[str, object] = {
            "timeout": 0,
            "allowed_updates": ["message", "callback_query", "pre_checkout_query"],
        }
        if offset is not None:
            params["offset"] = offset

        payload = self._request(
            "GET",
            f"{self._api_base_url}/getUpdates",
            params=params,
        )
        return list(payload.get("result", []))

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._request(
            "POST",
            f"{self._api_base_url}/sendMessage",
            json=payload,
        )

    def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._request(
            "POST",
            f"{self._api_base_url}/editMessageText",
            json=payload,
        )

    def edit_message_media(
        self,
        *,
        chat_id: int,
        message_id: int,
        media: dict[str, object],
        photo_path: str | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        media_payload = dict(media)
        if photo_path:
            path = Path(photo_path).expanduser()
            if path.is_file():
                media_payload["media"] = "attach://photo"
                payload["media"] = json.dumps(media_payload, ensure_ascii=False)
                with path.open("rb") as handle:
                    self._request(
                        "POST",
                        f"{self._api_base_url}/editMessageMedia",
                        data=payload,
                        files={"photo": (path.name, handle, "image/png")},
                    )
                return
            media_payload["media"] = photo_path

        payload["media"] = json.dumps(media_payload, ensure_ascii=False)
        self._request(
            "POST",
            f"{self._api_base_url}/editMessageMedia",
            json=payload,
        )

    def copy_message(
        self,
        *,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._request(
            "POST",
            f"{self._api_base_url}/copyMessage",
            json=payload,
        )

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
    ) -> None:
        payload: dict[str, object] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._request(
            "POST",
            f"{self._api_base_url}/answerCallbackQuery",
            json=payload,
        )

    def send_invoice(
        self,
        *,
        chat_id: int,
        title: str,
        description: str,
        payload: str,
        provider_token: str,
        currency: str,
        prices: list[dict[str, object]],
        start_parameter: str | None = None,
        provider_data: str | None = None,
        need_email: bool = False,
        send_email_to_provider: bool = False,
    ) -> dict[str, object]:
        request_payload: dict[str, object] = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "provider_token": provider_token,
            "currency": currency,
            "prices": prices,
            "need_email": need_email,
            "send_email_to_provider": send_email_to_provider,
        }
        if start_parameter:
            request_payload["start_parameter"] = start_parameter
        if provider_data is not None:
            request_payload["provider_data"] = provider_data
        response = self._request(
            "POST",
            f"{self._api_base_url}/sendInvoice",
            json=request_payload,
        )
        message = response.get("result")
        if not isinstance(message, dict):
            raise RuntimeError("Telegram Bot API sendInvoice returned no message payload.")
        return message

    def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "pre_checkout_query_id": pre_checkout_query_id,
            "ok": ok,
        }
        if error_message:
            payload["error_message"] = error_message
        self._request(
            "POST",
            f"{self._api_base_url}/answerPreCheckoutQuery",
            json=payload,
        )

    def delete_message(
        self,
        *,
        chat_id: int,
        message_id: int,
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        self._request(
            "POST",
            f"{self._api_base_url}/deleteMessage",
            json=payload,
        )

    def send_document(
        self,
        *,
        chat_id: int,
        document: str,
        caption: str | None = None,
        filename: str | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        path = Path(document).expanduser()
        payload: dict[str, object] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        if path.is_file():
            upload_name = filename or path.name
            with path.open("rb") as handle:
                response = self._request(
                    "POST",
                    f"{self._api_base_url}/sendDocument",
                    data=payload,
                    files={
                        "document": (
                            upload_name,
                            handle,
                            "application/vnd.android.package-archive",
                        )
                    },
                )
        else:
            payload["document"] = document
            response = self._request(
                "POST",
                f"{self._api_base_url}/sendDocument",
                data=payload,
            )

        message = response.get("result")
        if not isinstance(message, dict):
            raise RuntimeError("Telegram Bot API sendDocument returned no message payload.")
        return message

    def send_photo(
        self,
        *,
        chat_id: int,
        photo: str,
        caption: str | None = None,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, object]:
        path = Path(photo).expanduser()
        payload: dict[str, object] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        if path.is_file():
            with path.open("rb") as handle:
                response = self._request(
                    "POST",
                    f"{self._api_base_url}/sendPhoto",
                    data=payload,
                    files={"photo": (path.name, handle, "image/png")},
                )
        else:
            payload["photo"] = photo
            response = self._request(
                "POST",
                f"{self._api_base_url}/sendPhoto",
                data=payload,
            )

        message = response.get("result")
        if not isinstance(message, dict):
            raise RuntimeError("Telegram Bot API sendPhoto returned no message payload.")
        return message

    def _request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> dict[str, object]:
        try:
            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            message = "Telegram Bot API request failed."
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                description = payload.get("description")
                if isinstance(description, str) and description.strip():
                    message = description
            raise RuntimeError(message) from exc

        payload = response.json()
        if payload.get("ok") is not True:
            description = payload.get("description")
            if isinstance(description, str) and description.strip():
                raise RuntimeError(description)
            raise RuntimeError("Telegram Bot API request failed.")
        return payload


class TelegramAuthService:
    def __init__(
        self,
        settings: Settings,
        bot_gateway: TelegramBotGateway | None = None,
        session_exchange: Callable[[TelegramExchangeRequest], AppSession] | None = None,
        admin_service: TelegramAdminService | None = None,
        session_relink: Callable[[str, TelegramExchangeRequest], AppSession] | None = None,
    ) -> None:
        self._settings = settings
        self._bot_gateway = bot_gateway or HttpxTelegramBotGateway(settings)
        self._session_exchange = session_exchange
        self._session_relink = session_relink
        self._admin_service = admin_service
        if admin_service is not None:
            admin_service.set_bot_gateway(self._bot_gateway)
        self._flows: dict[str, PendingAuthFlow] = {}
        self._users: dict[int, RegisteredUser] = {}
        self._next_update_offset: int | None = None
        self._lock = Lock()

    def start_flow(
        self,
        *,
        device_id: str,
        device_label: str,
        platform: str,
        relink_user_id: str | None = None,
    ) -> TelegramAuthFlowResponse:
        self._ensure_is_configured()

        now = datetime.now(UTC)
        flow_id = token_urlsafe(12)
        one_time_code = "".join(choice("0123456789") for _ in range(6))
        auth_url = (
            f"https://t.me/{self._settings.telegram_bot_username}"
            f"?start=login_{flow_id}"
        )
        expires_at = now + timedelta(
            seconds=self._settings.telegram_auth_timeout_seconds
        )

        with self._lock:
            self._flows[flow_id] = PendingAuthFlow(
                flow_id=flow_id,
                one_time_code=one_time_code,
                auth_url=auth_url,
                device_id=device_id,
                device_label=device_label,
                platform=platform,
                created_at=now,
                expires_at=expires_at,
                relink_user_id=relink_user_id,
            )

        return TelegramAuthFlowResponse(
            flow_id=flow_id,
            auth_url=auth_url,
            one_time_code=one_time_code,
            bot_username=self._settings.telegram_bot_username,
            status_check_after_ms=2000,
            expires_at=expires_at,
            instructions=(
                "Open the bot in Telegram and press Start. "
                "If Telegram does not pass the link payload, send "
                f"'/login {one_time_code}' to the bot manually."
            ),
        )

    def get_status(self, flow_id: str) -> TelegramAuthStatusResponse:
        self._ensure_is_configured()
        self.process_updates()

        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is None:
                raise KeyError(flow_id)

            now = datetime.now(UTC)
            if flow.status is TelegramAuthStatus.pending and now >= flow.expires_at:
                flow.status = TelegramAuthStatus.expired

            return TelegramAuthStatusResponse(
                flow_id=flow.flow_id,
                status=flow.status,
                session=flow.session,
                verified_at=flow.verified_at,
                expires_at=flow.expires_at,
            )

    def _ensure_is_configured(self) -> None:
        if (
            not self._settings.telegram_bot_token.strip()
            or not self._settings.telegram_bot_username.strip()
        ):
            raise RuntimeError(
                "Telegram auth is not configured. "
                "Set SPEEDY_TELEGRAM_BOT_TOKEN and SPEEDY_TELEGRAM_BOT_USERNAME."
            )

    def process_updates(self) -> None:
        if self._admin_service is not None:
            self._admin_service.process_pending_payment_checks()
        updates = self._bot_gateway.fetch_updates(offset=self._next_update_offset)
        if not updates:
            return

        with self._lock:
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._next_update_offset = update_id + 1
                self._consume_update(update)

    def _consume_update(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            self._consume_message(message)
            return

        pre_checkout_query = update.get("pre_checkout_query")
        if isinstance(pre_checkout_query, dict):
            self._consume_pre_checkout_query(pre_checkout_query)
            return

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            self._consume_callback_query(callback_query)
            return

    def _consume_message(self, message: dict[str, object]) -> None:
        from_user = message.get("from")
        chat = message.get("chat")
        if not isinstance(from_user, dict) or not isinstance(chat, dict):
            return

        telegram_user = self._parse_telegram_user(from_user)
        if telegram_user is None:
            return

        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        successful_payment = message.get("successful_payment")
        if isinstance(successful_payment, dict):
            self._consume_successful_payment(
                telegram_user=telegram_user,
                payment_payload=successful_payment,
            )
            return

        message_id = message.get("message_id")
        text = message.get("text")
        if not isinstance(text, str):
            text = message.get("caption", "")

        flow = self._match_flow(text)
        if flow is None:
            if self._admin_service is None:
                return
            try:
                reply = self._admin_service.handle_message(
                    telegram_user_id=telegram_user.telegram_id,
                    display_name=telegram_user.display_name,
                    username=telegram_user.username,
                    text=text,
                    message_id=message_id,
                )
            except Exception as exc:
                reply = BotResponse(text=str(exc))
            if reply:
                self._deliver_bot_response(chat_id=chat_id, response=reply)
            return

        if flow.status is not TelegramAuthStatus.pending:
            return

        if datetime.now(UTC) >= flow.expires_at:
            flow.status = TelegramAuthStatus.expired
            return

        try:
            session = self._create_session(flow=flow, telegram_user=telegram_user)
        except Exception as exc:
            flow.status = TelegramAuthStatus.expired
            try:
                self._bot_gateway.send_message(chat_id=chat_id, text=str(exc))
            except RuntimeError:
                return
            return
        flow.status = TelegramAuthStatus.completed
        flow.session = session
        flow.verified_at = datetime.now(UTC)
        flow.telegram_user_id = telegram_user.telegram_id

        try:
            self._bot_gateway.send_message(
                chat_id=chat_id,
                text="Authorization confirmed. Return to the MeFast VPN app.",
            )
        except RuntimeError:
            return

    def _consume_pre_checkout_query(self, pre_checkout_query: dict[str, object]) -> None:
        if self._admin_service is None:
            return

        query_id = pre_checkout_query.get("id")
        from_user = pre_checkout_query.get("from")
        invoice_payload = pre_checkout_query.get("invoice_payload")
        total_amount = pre_checkout_query.get("total_amount")
        currency = pre_checkout_query.get("currency")
        if (
            not isinstance(query_id, str)
            or not isinstance(from_user, dict)
            or not isinstance(invoice_payload, str)
            or not isinstance(total_amount, int)
            or not isinstance(currency, str)
        ):
            return

        telegram_user = self._parse_telegram_user(from_user)
        if telegram_user is None:
            return

        try:
            self._admin_service.validate_pre_checkout_query(
                telegram_user_id=telegram_user.telegram_id,
                invoice_payload=invoice_payload,
                total_amount=total_amount,
                currency=currency,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self._bot_gateway.answer_pre_checkout_query(
                    pre_checkout_query_id=query_id,
                    ok=False,
                    error_message=str(exc),
                )
            except RuntimeError:
                return
            return

        try:
            self._bot_gateway.answer_pre_checkout_query(
                pre_checkout_query_id=query_id,
                ok=True,
            )
        except RuntimeError:
            return

    def _consume_successful_payment(
        self,
        *,
        telegram_user: TelegramUser,
        payment_payload: dict[str, object],
    ) -> None:
        if self._admin_service is None:
            return

        try:
            order = self._admin_service.complete_successful_payment(
                telegram_user_id=telegram_user.telegram_id,
                payment_payload=payment_payload,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self._bot_gateway.send_message(chat_id=telegram_user.telegram_id, text=str(exc))
            except RuntimeError:
                return
            return
        _ = order

    def _consume_callback_query(self, callback_query: dict[str, object]) -> None:
        if self._admin_service is None:
            return

        callback_query_id = callback_query.get("id")
        from_user = callback_query.get("from")
        message = callback_query.get("message")
        data = callback_query.get("data")
        if (
            not isinstance(callback_query_id, str)
            or not isinstance(from_user, dict)
            or not isinstance(message, dict)
            or not isinstance(data, str)
        ):
            return

        chat = message.get("chat")
        message_id = message.get("message_id")
        if not isinstance(chat, dict) or not isinstance(message_id, int):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        telegram_user = self._parse_telegram_user(from_user)
        if telegram_user is None:
            return

        try:
            reply = self._admin_service.handle_callback(
                telegram_user_id=telegram_user.telegram_id,
                display_name=telegram_user.display_name,
                username=telegram_user.username,
                data=data,
            )
        except Exception as exc:
            reply = BotResponse(text=str(exc), edit_message=True)

        if reply is not None:
            if reply.callback_notice:
                try:
                    self._bot_gateway.answer_callback_query(
                        callback_query_id=callback_query_id,
                        text=reply.callback_notice,
                    )
                except RuntimeError:
                    pass
            self._deliver_bot_response(
                chat_id=chat_id,
                response=reply,
                message_id=message_id,
            )

    def _deliver_bot_response(
        self,
        *,
        chat_id: int,
        response: BotResponse,
        message_id: int | None = None,
    ) -> None:
        """Deliver a bot response as an in-place card update when possible.

        UX contract: every navigation screen lives on ONE persistent message
        per chat ("the card"). Callbacks edit that card instead of spawning
        new messages. Only truly ephemeral payloads (invoices, files, access
        links) are sent as separate messages by their own senders.
        """
        gateway = self._bot_gateway
        if gateway is None:
            return

        session: tuple[int, str, str | None, str | None] | None = None
        admin_service = self._admin_service
        if admin_service is not None:
            try:
                session = admin_service.ui_store.get_bot_ui_session(chat_id)
            except Exception:
                session = None

        # --- photo card -----------------------------------------------------
        if response.photo:
            reuse_file_id = (
                session is not None
                and session[1] == "photo"
                and session[2] == response.photo
                and session[3]
            )
            if (
                session is not None
                and session[1] == "photo"
                and (message_id is None or message_id == session[0])
            ):
                media_payload: dict[str, object] = {"type": "photo"}
                if reuse_file_id and session is not None:
                    media_payload["media"] = session[3]
                    photo_path = None
                else:
                    media_payload["media"] = "attach://photo"
                    photo_path = response.photo
                if response.parse_mode:
                    media_payload["parse_mode"] = response.parse_mode
                try:
                    gateway.edit_message_media(
                        chat_id=chat_id,
                        message_id=session[0],
                        media=media_payload,
                        photo_path=photo_path,
                        reply_markup=response.reply_markup,
                    )
                    return
                except RuntimeError as exc:
                    if self._is_not_modified(exc):
                        return

            # cannot convert in place: replace the old card with a fresh one
            if session is not None:
                try:
                    gateway.delete_message(chat_id=chat_id, message_id=session[0])
                except Exception:
                    pass
            try:
                sent = gateway.send_photo(
                    chat_id=chat_id,
                    photo=response.photo,
                    caption=response.text,
                    reply_markup=response.reply_markup,
                    parse_mode=response.parse_mode,
                )
            except RuntimeError:
                return
            self._remember_ui_card(chat_id, "photo", sent, token=response.photo)
            return

        # --- text card ------------------------------------------------------
        replaced_photo_card = False
        if session is not None and session[1] == "photo":
            # a text screen replaces the photo card: keep the single-card UX
            try:
                gateway.delete_message(chat_id=chat_id, message_id=session[0])
            except Exception:
                pass
            session = None
            replaced_photo_card = True

        if session is not None:
            if message_id is not None and message_id != session[0]:
                # pressed on a stale copy: update the live card instead
                pass
            if session[1] == "text":
                try:
                    gateway.edit_message_text(
                        chat_id=chat_id,
                        message_id=session[0],
                        text=response.text,
                        reply_markup=response.reply_markup,
                        parse_mode=response.parse_mode,
                    )
                    return
                except RuntimeError as exc:
                    if self._is_not_modified(exc):
                        return

        if (
            message_id is not None
            and not replaced_photo_card
            and (session is None or session[1] != "text")
        ):
            try:
                gateway.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=response.text,
                    reply_markup=response.reply_markup,
                    parse_mode=response.parse_mode,
                )
                self._remember_ui_card(chat_id, "text", message_id)
                return
            except RuntimeError as exc:
                if self._is_not_modified(exc):
                    return

        try:
            sent_message = gateway.send_message(
                chat_id=chat_id,
                text=response.text,
                reply_markup=response.reply_markup,
                parse_mode=response.parse_mode,
            )
        except RuntimeError:
            return
        self._remember_ui_card(chat_id, "text", sent_message)

    @staticmethod
    def _is_not_modified(exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "message is not modified" in message

    @staticmethod
    def _extract_photo_file_id(message: dict[str, object]) -> str | None:
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            last = photos[-1]
            if isinstance(last, dict):
                file_id = last.get("file_id")
                if isinstance(file_id, str):
                    return file_id
        return None

    def _remember_ui_card(
        self,
        chat_id: int,
        kind: str,
        message_or_id: dict[str, object] | int | None,
        *,
        token: str | None = None,
    ) -> None:
        admin_service = self._admin_service
        if admin_service is None:
            return
        if isinstance(message_or_id, dict):
            raw = message_or_id.get("message_id")
        else:
            raw = message_or_id
        try:
            mid = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        file_id = None
        if kind == "photo" and isinstance(message_or_id, dict):
            file_id = self._extract_photo_file_id(message_or_id)
        try:
            admin_service.ui_store.set_bot_ui_session(
                chat_id=chat_id,
                message_id=mid,
                kind=kind,
                photo_token=token,
                file_id=file_id,
            )
        except Exception:
            pass

    def _match_flow(self, text: str) -> PendingAuthFlow | None:
        normalized = text.strip()

        flow_id = self._extract_flow_id(normalized)
        if flow_id is not None:
            return self._flows.get(flow_id)

        code = self._extract_manual_code(normalized)
        if code is None:
            return None

        for flow in self._flows.values():
            if flow.one_time_code == code:
                return flow
        return None

    @staticmethod
    def _extract_flow_id(text: str) -> str | None:
        if not text.startswith("/start "):
            return None

        payload = text.split(" ", maxsplit=1)[1].strip()
        if not payload.startswith("login_"):
            return None

        flow_id = payload.removeprefix("login_").strip()
        return flow_id or None

    @staticmethod
    def _extract_manual_code(text: str) -> str | None:
        if text.isdigit() and len(text) == 6:
            return text

        if text.startswith("/login "):
            code = text.split(" ", maxsplit=1)[1].strip()
            if code.isdigit() and len(code) == 6:
                return code

        return None

    @staticmethod
    def _parse_telegram_user(from_user: dict[str, object]) -> TelegramUser | None:
        telegram_id = from_user.get("id")
        first_name = from_user.get("first_name")
        last_name = from_user.get("last_name")
        username = from_user.get("username")

        if not isinstance(telegram_id, int) or not isinstance(first_name, str):
            return None

        last_name_value = last_name if isinstance(last_name, str) else ""
        username_value = username if isinstance(username, str) else None
        display_name = " ".join(
            part for part in (first_name.strip(), last_name_value.strip()) if part
        )
        if not display_name:
            display_name = username_value or f"Telegram {telegram_id}"

        return TelegramUser(
            telegram_id=telegram_id,
            display_name=display_name,
            username=username_value,
        )

    def _create_session(
        self,
        *,
        flow: PendingAuthFlow,
        telegram_user: TelegramUser,
    ) -> AppSession:
        if self._session_exchange is not None:
            telegram_handle = (
                f"@{telegram_user.username}"
                if telegram_user.username
                else f"telegram:{telegram_user.telegram_id}"
            )
            exchange_req = TelegramExchangeRequest(
                telegram_auth_payload={
                    "id": str(telegram_user.telegram_id),
                    "username": telegram_user.username or "",
                    "telegram_handle": telegram_handle,
                    "display_name": telegram_user.display_name,
                },
                device_id=flow.device_id,
                device_label=flow.device_label,
                platform=flow.platform,
            )
            if flow.relink_user_id and self._session_relink is not None:
                return self._session_relink(flow.relink_user_id, exchange_req)
            return self._session_exchange(exchange_req)

        registered_user = self._users.get(telegram_user.telegram_id)
        if registered_user is None:
            telegram_handle = (
                f"@{telegram_user.username}"
                if telegram_user.username
                else f"telegram:{telegram_user.telegram_id}"
            )
            registered_user = RegisteredUser(
                user_id=f"tg_{telegram_user.telegram_id}",
                telegram_handle=telegram_handle,
                display_name=telegram_user.display_name,
                email=None,
            )
            self._users[telegram_user.telegram_id] = registered_user
        else:
            registered_user.display_name = telegram_user.display_name
            if telegram_user.username:
                registered_user.telegram_handle = f"@{telegram_user.username}"

        now = datetime.now(UTC)
        device_binding = DeviceBinding(
            device_id=flow.device_id,
            platform=flow.platform,
            label=flow.device_label,
            created_at=now,
            last_seen_at=now,
        )

        return AppSession(
            user_id=registered_user.user_id,
            telegram_handle=registered_user.telegram_handle,
            display_name=registered_user.display_name,
            email=registered_user.email,
            auth_token=token_urlsafe(24),
            is_premium=registered_user.is_premium,
            expires_at=now + timedelta(days=self._settings.session_ttl_days),
            device_binding=device_binding,
        )
