from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from app.config import settings
from app.schemas.contracts import (
    AccountProfile,
    ActivationCodeExchangeRequest,
    AppReleaseInfo,
    AppSession,
    BootstrapResponse,
    DeviceBindRequest,
    DeviceBinding,
    LocationSelectRequest,
    Plan,
    PromoApplyRequest,
    PromoApplyResult,
    PromoAuthExchangeRequest,
    ProfileUpdateRequest,
    SubscriptionOrder,
    SubscriptionOrderCreateRequest,
    PushTokenRegisterRequest,
    SubscriptionState,
    TelegramAuthFlowResponse,
    TelegramAuthStatusResponse,
    TelegramLinkConfirmRequest,
    TelegramStartRequest,
    TelegramExchangeRequest,
    VpnConfigResponse,
    VpnLocation,
)
from app.services.auth_tokens import SessionClaims, SessionTokenError, decode_app_token
from app.services.catalog_store import PROFILE_UNSET, TelegramIdentityConflictError
from app.services.facade import (
    ActivationCodeError,
    ActivationCodeThrottledError,
    PromoCodeError,
    SpeedyFacadeService,
    SubscriptionRequiredError,
    UserBlockedError,
)
from app.services.marzban import MarzbanError
from app.services.telegram_admin import TelegramAdminService
from app.services.telegram_auth import TelegramAuthService

router = APIRouter(prefix="/v1", tags=["speedy-vpn"])
public_router = APIRouter(tags=["public-subscriptions"])
speedy_service = SpeedyFacadeService(settings)
telegram_admin_service = TelegramAdminService(settings, speedy_service)
auth_service = TelegramAuthService(
    settings,
    session_exchange=speedy_service.exchange_auth,
    admin_service=telegram_admin_service,
    session_relink=speedy_service.link_telegram_auth,
)


def get_speedy_service() -> SpeedyFacadeService:
    return speedy_service


@public_router.get("/s/{token}", response_class=PlainTextResponse)
def get_public_subscription(
    token: str,
    request: Request,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> PlainTextResponse:
    try:
        export = service.build_public_subscription_export(
            token,
            client_host=request.client.host if request.client is not None else None,
            user_agent=request.headers.get("user-agent"),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Subscription link not found.") from exc
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except SubscriptionRequiredError as exc:
        raise HTTPException(status_code=403, detail=_api_error("subscription_required", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    headers = service.public_subscription_headers(
        export,
        request_base_url=str(request.base_url),
    )
    content_type = headers.pop("content-type")
    return PlainTextResponse(
        content=export.body,
        status_code=200,
        headers=headers,
        media_type=content_type,
    )


@public_router.get("/happ/{token}")
def open_happ_subscription(
    token: str,
    request: Request,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> RedirectResponse:
    try:
        deep_link = service.build_happ_deep_link(
            token,
            request_base_url=str(request.base_url),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Subscription link not found.") from exc
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except SubscriptionRequiredError as exc:
        raise HTTPException(status_code=403, detail=_api_error("subscription_required", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return RedirectResponse(url=deep_link, status_code=307)


@public_router.get("/assets/logo.png")
def get_public_logo() -> FileResponse:
    configured_path = Path(settings.service_logo_path).expanduser()
    if configured_path.is_absolute():
        candidates = [configured_path]
    else:
        routes_path = Path(__file__).resolve()
        candidates = [
            Path.cwd() / configured_path,
            routes_path.parents[2] / configured_path,
            routes_path.parents[3] / configured_path,
        ]
    logo_path = next((path for path in candidates if path.is_file()), None)
    if logo_path is None:
        raise HTTPException(status_code=404, detail="Logo not found.")
    return FileResponse(path=logo_path, media_type="image/png")


@public_router.get("/pay/return/{order_id}", response_class=HTMLResponse)
def yookassa_payment_return(
    order_id: str,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> HTMLResponse:
    try:
        order, activated = service.check_yookassa_order(order_id=order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Order not found.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if activated:
        telegram_admin_service.notify_user_order_approved(order_id=order.order_id, order=order)
        telegram_admin_service.deliver_order_access(order_id=order.order_id)

    bot_username = settings.telegram_bot_username.strip()
    bot_url = f"https://t.me/{bot_username}" if bot_username else settings.support_url
    if order.status.value == "confirmed":
        title = "Оплата подтверждена"
        message = "Подписка активирована. Вернитесь в Telegram-бот, там уже будет доступ."
    elif order.status.value == "canceled":
        title = "Платеж не завершен"
        message = "YooKassa не подтвердила оплату. Можно вернуться в бот и создать новый заказ."
    else:
        title = "Проверяем оплату"
        message = "Платеж пока не подтвержден. Если вы уже оплатили, вернитесь в бот и нажмите проверку оплаты."

    safe_title = escape(title)
    safe_message = escape(message)
    safe_bot_url = escape(bot_url, quote=True)
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #f8fafc; }}
    main {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; box-sizing: border-box; }}
    section {{ width: min(480px, 100%); background: #111827; border: 1px solid #334155; border-radius: 16px; padding: 28px; }}
    h1 {{ margin: 0 0 12px; font-size: 28px; }}
    p {{ margin: 0 0 22px; color: #cbd5e1; line-height: 1.5; }}
    a {{ display: inline-block; background: #22c55e; color: white; text-decoration: none; font-weight: 700; border-radius: 10px; padding: 12px 18px; }}
  </style>
</head>
<body>
  <main>
    <section>
      <h1>{safe_title}</h1>
      <p>{safe_message}</p>
      <a href="{safe_bot_url}">Открыть Telegram-бот</a>
    </section>
  </main>
</body>
</html>"""
    )


def _api_error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def get_current_claims(
    authorization: Annotated[str | None, Header()] = None,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> SessionClaims:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )

    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = decode_app_token(settings, token)
        service.assert_user_allowed(claims)
        return claims
    except SessionTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_api_error("invalid_session", str(exc)),
        ) from exc
    except UserBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_api_error("account_blocked", str(exc)),
        ) from exc


@router.post("/auth/telegram/start", response_model=TelegramAuthFlowResponse)
def start_telegram_auth(payload: TelegramStartRequest) -> TelegramAuthFlowResponse:
    try:
        return auth_service.start_flow(
            device_id=payload.device_id,
            device_label=payload.device_label,
            platform=payload.platform,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/auth/telegram/relink/start", response_model=TelegramAuthFlowResponse)
def start_telegram_relink(
    payload: TelegramStartRequest,
    claims: SessionClaims = Depends(get_current_claims),
) -> TelegramAuthFlowResponse:
    return start_account_link_telegram(payload, claims)


@router.post("/account/link-telegram/start", response_model=TelegramAuthFlowResponse)
def start_account_link_telegram(
    payload: TelegramStartRequest,
    claims: SessionClaims = Depends(get_current_claims),
) -> TelegramAuthFlowResponse:
    try:
        return auth_service.start_flow(
            device_id=payload.device_id,
            device_label=payload.device_label,
            platform=payload.platform,
            relink_user_id=claims.user_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/account/link-telegram/confirm", response_model=AppSession)
def confirm_account_link_telegram(
    payload: TelegramLinkConfirmRequest,
) -> AppSession:
    try:
        status_payload = auth_service.get_status(payload.flow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Telegram auth flow not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if status_payload.status.value == "completed" and status_payload.session is not None:
        return status_payload.session
    if status_payload.status.value == "expired":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=_api_error("telegram_link_expired", "Telegram confirmation expired."),
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_api_error("telegram_link_pending", "Telegram confirmation is still pending."),
    )


@router.get(
    "/auth/telegram/status/{flow_id}",
    response_model=TelegramAuthStatusResponse,
)
def get_telegram_auth_status(flow_id: str) -> TelegramAuthStatusResponse:
    try:
        return auth_service.get_status(flow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Telegram auth flow not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/auth/telegram/exchange", response_model=AppSession)
def exchange_telegram_auth(
    payload: TelegramExchangeRequest,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AppSession:
    try:
        return service.exchange_auth(payload)
    except TelegramIdentityConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=_api_error("telegram_identity_conflict", str(exc)),
        ) from exc
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/auth/activation-code/exchange", response_model=AppSession)
def exchange_activation_code(
    payload: ActivationCodeExchangeRequest,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AppSession:
    try:
        return service.exchange_activation_code(payload)
    except ActivationCodeThrottledError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_api_error("activation_code_throttled", str(exc)),
        ) from exc
    except ActivationCodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_api_error("activation_code_invalid", str(exc)),
        ) from exc
    except UserBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_api_error("account_blocked", str(exc)),
        ) from exc


@router.post("/auth/promo/exchange", response_model=AppSession)
def exchange_promo_auth(
    payload: PromoAuthExchangeRequest,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AppSession:
    try:
        return service.exchange_promo_auth(payload)
    except PromoCodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_api_error("promo_invalid", str(exc)),
        ) from exc
    except UserBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_api_error("account_blocked", str(exc)),
        ) from exc


@router.post("/devices/bind", response_model=DeviceBinding)
def bind_device(payload: DeviceBindRequest) -> DeviceBinding:
    now = datetime.now(UTC)
    return DeviceBinding(
        device_id=payload.device_id,
        platform=payload.platform,
        label=payload.device_label,
        created_at=now,
        last_seen_at=now,
    )


@router.post("/devices/push-token", response_model=DeviceBinding)
def register_push_token(
    payload: PushTokenRegisterRequest,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> DeviceBinding:
    try:
        return service.register_push_token(
            claims,
            device_id=payload.device_id,
            platform=payload.platform,
            push_token=payload.push_token,
            provider=payload.provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/bootstrap", response_model=BootstrapResponse)
def bootstrap(
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> BootstrapResponse:
    try:
        return service.build_bootstrap(claims)
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/locations", response_model=list[VpnLocation])
def list_locations(
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> list[VpnLocation]:
    try:
        return service.get_locations(claims)
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/locations/ping", response_model=list[VpnLocation])
def ping_locations(
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> list[VpnLocation]:
    try:
        return service.ping_all_locations(claims)
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/locations/select", response_model=VpnLocation)
def select_location(
    payload: LocationSelectRequest,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> VpnLocation:
    try:
        return service.select_location(claims, payload.location_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Location not found.") from exc
    except SubscriptionRequiredError as exc:
        raise HTTPException(
            status_code=403,
            detail=_api_error("subscription_required", str(exc)),
        ) from exc
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/profile", response_model=AccountProfile)
def get_profile(
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AccountProfile:
    try:
        return service.get_profile(claims)
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/profile", response_model=AccountProfile)
def patch_profile(
    payload: ProfileUpdateRequest,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AccountProfile:
    display_name = payload.display_name if "display_name" in payload.model_fields_set else PROFILE_UNSET
    email = payload.email if "email" in payload.model_fields_set else PROFILE_UNSET

    try:
        return service.update_profile(
            claims,
            display_name=display_name,
            email=email,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Profile not found.") from exc
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc


@router.get("/subscription", response_model=SubscriptionState)
def get_subscription(
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> SubscriptionState:
    try:
        return service.get_subscription(claims)
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/plans", response_model=list[Plan])
def list_plans(
    _: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> list[Plan]:
    return service.get_plans()


@router.post("/promos/apply", response_model=PromoApplyResult)
def apply_promo(
    payload: PromoApplyRequest,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> PromoApplyResult:
    try:
        return service.apply_promo(claims, code=payload.code)
    except PromoCodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_api_error("promo_invalid", str(exc)),
        ) from exc


@router.get("/app-release/current", response_model=AppReleaseInfo | None)
def get_current_app_release(
    _: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AppReleaseInfo | None:
    return service.get_current_app_release_info(platform="android")


@router.get("/orders", response_model=list[SubscriptionOrder])
def list_orders(
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> list[SubscriptionOrder]:
    return service.list_orders(claims)


@router.post("/orders", response_model=SubscriptionOrder)
def create_order(
    payload: SubscriptionOrderCreateRequest,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> SubscriptionOrder:
    try:
        order = service.create_order(
            claims,
            plan_id=payload.plan_id,
            promo_code=payload.promo_code,
        )
    except PromoCodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_api_error("promo_invalid", str(exc)),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Notify user in Telegram bot if they have a linked account
    telegram_admin_service.notify_user_new_order(
        telegram_user_id=claims.telegram_user_id,
        order=order,
    )
    return order


@router.post("/orders/{order_id}/mark-paid", response_model=SubscriptionOrder)
def mark_order_paid(
    order_id: str,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> SubscriptionOrder:
    try:
        order = service.mark_order_paid(claims, order_id=order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Order not found.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=_api_error("order_forbidden", str(exc))) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_api_error("order_invalid_state", str(exc))) from exc
    # Notify admins in Telegram bot
    user = service.store.get_user(claims.user_id)
    telegram_admin_service.notify_admins_order_paid(order=order, user=user)
    return order


@router.post("/orders/{order_id}/cancel", response_model=SubscriptionOrder)
def cancel_order(
    order_id: str,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> SubscriptionOrder:
    try:
        order = service.cancel_order(claims, order_id=order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Order not found.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=_api_error("order_forbidden", str(exc))) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_api_error("order_invalid_state", str(exc))) from exc
    telegram_admin_service.delete_order_invoice(order_id=order_id, clear_metadata=True)
    return order


@router.get("/downloads/android-apk")
def download_android_apk() -> FileResponse:
    apk_path = Path(settings.telegram_android_apk_path).expanduser()
    if not settings.telegram_android_apk_path.strip() or not apk_path.is_file():
        raise HTTPException(status_code=404, detail="Android APK is not available.")
    filename = settings.telegram_android_apk_filename.strip() or apk_path.name
    return FileResponse(
        apk_path,
        media_type="application/vnd.android.package-archive",
        filename=filename,
    )


@router.get("/vpn/config", response_model=VpnConfigResponse)
def get_vpn_config(
    location_id: str,
    routing_profile: str | None = None,
    claims: SessionClaims = Depends(get_current_claims),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> VpnConfigResponse:
    try:
        return service.get_vpn_config(
            claims,
            location_id,
            routing_profile=routing_profile,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Location not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SubscriptionRequiredError as exc:
        raise HTTPException(
            status_code=403,
            detail=_api_error("subscription_required", str(exc)),
        ) from exc
    except UserBlockedError as exc:
        raise HTTPException(status_code=403, detail=_api_error("account_blocked", str(exc))) from exc
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/my-ip")
def get_my_ip(request: Request) -> dict[str, str]:
    """Return the caller's IP address as seen by the server.
    
    Note: Only trusts X-Forwarded-For behind a known reverse proxy.
    In direct-access mode, uses request.client.host.
    """
    ip = request.client.host if request.client else "unknown"
    return {"ip": ip}
