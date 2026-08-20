"""
Admin / Debug routes for Swagger testing.

All endpoints hit the REAL Marzban panel — nothing is mocked.
Admin endpoints require admin role authentication.
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict

from datetime import UTC, datetime
from secrets import compare_digest
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.contracts import (
    Plan,
    AdminCatalogStatus,
    AdminDashboardStats,
    AdminOrdersResponse,
    AdminPromosResponse,
    AppSession,
    AppReleaseInfo,
    AppReleasePublishRequest,
    AdminUserDetailResponse,
    AdminUserListItem,
    AdminUserListResponse,
    CreateMarzbanUserRequest,
    InventoryRefreshResponse,
    MarzbanInboundInfo,
    MarzbanUserInfo,
    QuickTestResponse,
    PromoCreateRequest,
    SubscriptionGrantRequest,
    SubscriptionRevokeRequest,
    SubscriptionStatus,
    SystemStatsResponse,
    TelegramExchangeRequest,
    VpnConfigResponse,
    VpnLocation,
)
from app.services.facade import SpeedyFacadeService
from app.services.facade import PromoCodeError
from app.services.marzban import MarzbanClient, MarzbanError

admin_router = APIRouter(prefix="/admin", tags=["🔧 Admin — Real Marzban"])
v1_admin_router = APIRouter(prefix="/v1/admin", tags=["Admin Web App"])

_marzban: MarzbanClient | None = None
_speedy: SpeedyFacadeService | None = None


def _get_marzban() -> MarzbanClient:
    global _marzban
    if _marzban is None:
        _marzban = MarzbanClient(settings)
    return _marzban


def _get_speedy() -> SpeedyFacadeService:
    global _speedy
    if _speedy is None:
        _speedy = SpeedyFacadeService(settings)
    return _speedy


from app.api.routes import get_current_claims, get_speedy_service, telegram_admin_service
from app.services.auth_tokens import SessionClaims


class LoginRateLimiter:
    """In-memory rate limiter for admin login: max 5 attempts per 120 seconds per IP."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 120):
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, client_ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[client_ip]
            self._attempts[client_ip] = [t for t in attempts if now - t < self._window]
            if len(self._attempts[client_ip]) >= self._max_attempts:
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many login attempts. Try again in {self._window} seconds.",
                )

    def record(self, client_ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._attempts[client_ip].append(now)


_login_rate_limiter = LoginRateLimiter(max_attempts=5, window_seconds=120)


class AdminPasswordLoginRequest(BaseModel):
    password: str = Field(min_length=1)
    telegram_user_id: int
    device_id: str = "admin-web-panel"
    device_label: str = "Admin Web Panel"


class AdminSourceCreateRequest(BaseModel):
    kind: str
    source_text: str = Field(min_length=1)
    title: str | None = None


class AdminUserProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=100)
    email: str | None = None


def _check_admin_web_password(password: str | None) -> None:
    expected = settings.admin_web_password.strip() or settings.admin_bot_password.strip()
    if not expected:
        return
    if password is None or not compare_digest(password, expected):
        raise HTTPException(status_code=401, detail="Invalid admin password.")


def _source_payload(source: Any) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "kind": source.kind,
        "title": source.title,
        "source_text": source.source_text,
        "created_by": source.created_by,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
        "last_synced_at": source.last_synced_at,
        "last_error": source.last_error,
        "entry_count": source.entry_count,
        "published_count": source.published_count,
        "hidden_count": source.hidden_count,
        "disabled_count": source.disabled_count,
    }


def _entry_payload(entry: Any, *, include_config: bool = False) -> dict[str, Any]:
    payload = {
        "entry_id": entry.entry_id,
        "source_id": entry.source_id,
        "source_kind": entry.source_kind,
        "source_title": entry.source_title,
        "fingerprint": entry.fingerprint,
        "identity_key": entry.identity_key,
        "publish_state": entry.publish_state,
        "location": entry.location.model_dump(mode="json"),
        "share_uri": entry.share_uri,
        "display_override": entry.display_override,
    }
    if include_config:
        payload["config_variants"] = entry.config_variants
    return payload


def _admin_user_item(service: SpeedyFacadeService, user_id: str) -> AdminUserListItem:
    user = service.get_user(user_id)
    subscription = service.get_user_subscription_by_id(user_id)
    return AdminUserListItem(
        user_id=user.user_id,
        role=user.role,
        blocked=user.blocked,
        telegram_user_id=user.telegram_user_id,
        telegram_linked=user.telegram_user_id is not None,
        telegram_handle=user.telegram_handle,
        telegram_username=user.telegram_username,
        display_name=user.display_name,
        email=user.email,
        current_plan_id=subscription.current_plan_id,
        plan_title=subscription.plan_title,
        subscription_status=subscription.status,
        last_seen_at=user.last_seen_at,
        telegram_linked_at=user.telegram_linked_at,
    )

def require_admin(
    claims: SessionClaims = Depends(get_current_claims),
) -> SessionClaims:
    """Require admin role. Password is only checked at login, not on every request."""
    if claims.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return claims


@v1_admin_router.post("/auth/login", response_model=AppSession)
def login_v1_admin_with_password(
    payload: AdminPasswordLoginRequest,
    request: Request,
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AppSession:
    client_ip = request.client.host if request.client else "unknown"
    _login_rate_limiter.check(client_ip)
    _login_rate_limiter.record(client_ip)
    _check_admin_web_password(payload.password)
    try:
        session = service.exchange_auth(
            TelegramExchangeRequest(
                telegram_auth_payload={
                    "id": str(payload.telegram_user_id),
                    "username": "admin",
                },
                device_id=payload.device_id,
                device_label=payload.device_label,
                platform="web",
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if session.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return session

@v1_admin_router.get("/dashboard", response_model=AdminDashboardStats)
def get_v1_admin_dashboard(
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AdminDashboardStats:
    stats = service.store.get_admin_dashboard_stats()
    return AdminDashboardStats(**stats)

class TelegramBroadcastRequest(BaseModel):
    message_text: str = Field(min_length=1)

@v1_admin_router.post("/telegram/broadcast")
def broadcast_telegram_message(
    payload: TelegramBroadcastRequest,
    claims: SessionClaims = Depends(require_admin),
) -> dict[str, int]:
    return telegram_admin_service.broadcast_message(payload.message_text)

@v1_admin_router.get("/users", response_model=AdminUserListResponse)
def get_v1_admin_users(
    offset: int = 0,
    limit: int = 20,
    search: str | None = None,
    subscription_status: str | None = None,
    plan_id: str | None = None,
    role: str | None = None,
    blocked: bool | None = None,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AdminUserListResponse:
    users, total = service.store.get_paginated_users(
        offset=offset,
        limit=limit,
        query=search,
        role=role,
        blocked=blocked,
        subscription_status=subscription_status,
        plan_id=plan_id,
    )
    out_users = [_admin_user_item(service, user.user_id) for user in users]
    return AdminUserListResponse(users=out_users, total_count=total)

@v1_admin_router.get("/users/{user_id}", response_model=AdminUserDetailResponse)
def get_v1_admin_user_detail(
    user_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AdminUserDetailResponse:
    try:
        return AdminUserDetailResponse(
            user=_admin_user_item(service, user_id),
            summary=service.get_user_account_summary_by_id(user_id),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc


@v1_admin_router.get("/orders", response_model=AdminOrdersResponse)
def get_v1_admin_orders(
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AdminOrdersResponse:
    statuses = (status,) if status else ("waiting_confirmation", "pending_payment")
    orders = [
        service.get_order(order.order_id)
        for order in service.store.list_subscription_orders(
            statuses=statuses,
            limit=limit,
            offset=offset,
        )
    ]
    total = service.store.count_subscription_orders(statuses=statuses)
    return AdminOrdersResponse(orders=orders, total_count=total)


@v1_admin_router.post("/orders/{order_id}/approve")
def approve_admin_order(
    order_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        order = service.approve_order(order_id=order_id, admin_actor=claims.user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Order not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    telegram_admin_service.notify_user_order_approved(order_id=order_id, order=order)
    return {"status": "ok"}


@v1_admin_router.post("/orders/{order_id}/reject")
def reject_admin_order(
    order_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        service.reject_order(order_id=order_id, admin_actor=claims.user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Order not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    telegram_admin_service.notify_user_order_rejected(order_id=order_id)
    return {"status": "ok"}


@v1_admin_router.post("/users/{user_id}/subscription/grant")
def grant_user_subscription(
    user_id: str,
    payload: SubscriptionGrantRequest,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        service.grant_subscription(
            user_id=user_id,
            plan_id=payload.plan_id,
            granted_by=claims.user_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User or plan not found.") from exc
    user = service.get_user(user_id)
    if user.telegram_user_id is not None:
        telegram_admin_service.deliver_user_access(
            user_id=user_id,
            telegram_user_id=user.telegram_user_id,
        )
    return {"status": "ok"}


@v1_admin_router.post("/users/{user_id}/subscription/revoke")
def revoke_user_subscription(
    user_id: str,
    payload: SubscriptionRevokeRequest,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        service.revoke_subscription(
            user_id=user_id,
            reason=payload.reason,
            granted_by=claims.user_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    return {"status": "ok"}


@v1_admin_router.post("/users/{user_id}/block")
def block_user(
    user_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str | bool]:
    service.store.set_user_blocked(user_id, True)
    return {"status": "ok", "blocked": True}


@v1_admin_router.post("/users/{user_id}/unblock")
def unblock_user(
    user_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str | bool]:
    service.store.set_user_blocked(user_id, False)
    return {"status": "ok", "blocked": False}


@v1_admin_router.get("/catalog-status", response_model=AdminCatalogStatus)
def get_v1_admin_catalog_status(
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AdminCatalogStatus:
    status_payload = service.get_catalog_status()
    return AdminCatalogStatus(
        marzban_locations=status_payload.marzban_locations,
        external_sources=status_payload.external_sources,
        published_entries=status_payload.published_entries,
        hidden_entries=status_payload.hidden_entries,
        disabled_entries=status_payload.disabled_entries,
        last_synced_at=status_payload.last_synced_at,
    )


@v1_admin_router.post("/users/{user_id}/subscription-link/send")
def send_user_subscription_link(
    user_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    user = service.get_user(user_id)
    subscription = service.get_user_subscription_by_id(user_id)
    if not subscription.can_use_vpn:
        raise HTTPException(status_code=409, detail="User has no active subscription.")
    url = service.get_public_subscription_url(user_id)
    if user.telegram_user_id is not None:
        telegram_admin_service.deliver_user_access(
            user_id=user_id,
            telegram_user_id=user.telegram_user_id,
        )
    return {"status": "ok", "subscription_url": url}


@v1_admin_router.post("/users/{user_id}/subscription-link/rotate")
def rotate_user_subscription_link(
    user_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    url = service.rotate_public_subscription_url(user_id)
    user = service.get_user(user_id)
    if user.telegram_user_id is not None:
        telegram_admin_service.deliver_user_access(
            user_id=user_id,
            telegram_user_id=user.telegram_user_id,
        )
    return {"status": "ok", "subscription_url": url}


@v1_admin_router.post("/sources/{source_id}/rename")
def rename_admin_source(
    source_id: str,
    payload: dict[str, str],
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        source = service.update_external_source_title(
            source_id,
            payload.get("title", ""),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "source_id": source.source_id, "title": source.title}


@v1_admin_router.post("/catalog/entries/{entry_id}/state")
def set_admin_catalog_entry_state(
    entry_id: str,
    payload: dict[str, str],
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        entry = service.set_catalog_entry_state(
            entry_id,
            state=payload.get("state", ""),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Catalog entry not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "entry_id": entry.entry_id, "publish_state": entry.publish_state}


@v1_admin_router.post("/catalog/entries/{entry_id}/rename")
def rename_admin_catalog_entry(
    entry_id: str,
    payload: dict[str, str],
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        entry = service.update_catalog_entry_display_name(
            entry_id,
            display_name=payload.get("display_name", ""),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Catalog entry not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "entry_id": entry.entry_id, "display_name": entry.location.display_name}


@v1_admin_router.get("/promos", response_model=AdminPromosResponse)
def get_v1_admin_promos(
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AdminPromosResponse:
    return AdminPromosResponse(promos=service.list_promo_codes())


@v1_admin_router.post("/promos", response_model=dict[str, str])
def create_v1_admin_promo(
    payload: PromoCreateRequest,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        service.create_promo_code(payload, created_by=claims.user_id)
    except PromoCodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@v1_admin_router.post("/promos/{code}/deactivate", response_model=dict[str, str])
def deactivate_v1_admin_promo(
    code: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        service.deactivate_promo_code(code)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Promo code not found.") from exc
    return {"status": "ok"}


@v1_admin_router.get("/app-release/current", response_model=AppReleaseInfo | None)
def get_v1_admin_current_release(
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AppReleaseInfo | None:
    return service.get_current_app_release_info(platform="android")


@v1_admin_router.post("/app-release/publish", response_model=AppReleaseInfo)
def publish_v1_admin_release(
    payload: AppReleasePublishRequest,
    request: Request,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> AppReleaseInfo:
    base_url = str(request.base_url).rstrip("/")
    download_url = f"{base_url}/v1/downloads/android-apk"
    try:
        return service.publish_android_release(
            payload,
            published_by=claims.user_id,
            download_url=download_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@v1_admin_router.post("/users/{user_id}/profile")
def update_admin_user_profile(
    user_id: str,
    payload: AdminUserProfileUpdateRequest,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    try:
        user = service.store.update_user_profile(
            user_id,
            display_name=payload.display_name,
            email=payload.email,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "user": _admin_user_item(service, user.user_id).model_dump(mode="json")}


@v1_admin_router.post("/users/{user_id}/delete")
def delete_admin_user(
    user_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    if user_id == claims.user_id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself.")
    try:
        service.delete_user(user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="User not found.") from exc
    return {"status": "ok"}


@v1_admin_router.get("/search/users")
def search_admin_users(
    q: str,
    limit: int = 10,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    return {
        "users": [
            _admin_user_item(service, user.user_id).model_dump(mode="json")
            for user in service.search_users(q, limit=limit)
        ]
    }


@v1_admin_router.get("/sources")
def list_admin_sources(
    limit: int = 100,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    return {"sources": [_source_payload(source) for source in service.list_external_sources(limit=limit)]}


@v1_admin_router.post("/sources")
def create_admin_source(
    payload: AdminSourceCreateRequest,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    try:
        summary = service.add_external_source(
            kind=payload.kind,
            source_text=payload.source_text,
            title=payload.title,
            created_by=claims.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "ok",
        "source": _source_payload(summary.source),
        "imported_count": summary.imported_count,
        "skipped_messages": summary.skipped_messages,
        "delivered_push_count": summary.delivered_push_count,
    }


@v1_admin_router.get("/sources/{source_id}")
def get_admin_source(
    source_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    try:
        return {"source": _source_payload(service.get_external_source(source_id))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found.") from exc


@v1_admin_router.post("/sources/{source_id}/sync")
def sync_admin_source(
    source_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    try:
        summary = service.sync_external_source(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "ok",
        "source": _source_payload(summary.source),
        "imported_count": summary.imported_count,
        "skipped_messages": summary.skipped_messages,
        "delivered_push_count": summary.delivered_push_count,
    }


@v1_admin_router.post("/sources/{source_id}/delete")
def delete_admin_source(
    source_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        service.delete_external_source(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Source not found.") from exc
    return {"status": "ok"}


@v1_admin_router.get("/catalog/entries")
def list_admin_catalog_entries(
    offset: int = 0,
    limit: int = 50,
    source_id: str | None = None,
    include_hidden: bool = True,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    entries = service.list_catalog_entries(
        include_hidden=include_hidden,
        limit=limit,
        offset=offset,
        source_id=source_id,
    )
    return {
        "entries": [_entry_payload(entry) for entry in entries],
        "offset": offset,
        "limit": limit,
    }


@v1_admin_router.get("/catalog/entries/{entry_id}")
def get_admin_catalog_entry(
    entry_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, Any]:
    try:
        return {"entry": _entry_payload(service.get_catalog_entry(entry_id), include_config=True)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Catalog entry not found.") from exc


@v1_admin_router.post("/catalog/entries/{entry_id}/delete")
def delete_admin_catalog_entry(
    entry_id: str,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> dict[str, str]:
    try:
        service.delete_catalog_entry(entry_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Catalog entry not found.") from exc
    return {"status": "ok"}


import json
import subprocess
import os
import signal

@v1_admin_router.get("/parser/config")
def get_parser_config(claims: SessionClaims = Depends(require_admin)) -> dict[str, Any]:
    try:
        with open("/opt/proxy_scanner/config.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}

@v1_admin_router.put("/parser/config")
def set_parser_config(payload: dict[str, Any], claims: SessionClaims = Depends(require_admin)) -> dict[str, str]:
    with open("/opt/proxy_scanner/config.json", "w") as f:
        json.dump(payload, f, indent=2)
    return {"status": "ok"}

@v1_admin_router.get("/parser/status")
def get_parser_status(claims: SessionClaims = Depends(require_admin)) -> dict[str, Any]:
    try:
        with open("/opt/proxy_scanner/status.json", "r") as f:
            return json.load(f)
    except Exception:
        return {"is_running": False}

@v1_admin_router.get("/parser/history")
def get_parser_history(claims: SessionClaims = Depends(require_admin)) -> list[dict[str, Any]]:
    try:
        with open("/opt/proxy_scanner/history.json", "r") as f:
            return json.load(f)
    except Exception:
        return []

@v1_admin_router.post("/parser/start")
def start_parser(claims: SessionClaims = Depends(require_admin)) -> dict[str, str]:
    pid_file = "/opt/proxy_scanner/scanner.pid"
    if os.path.exists(pid_file):
        try:
            pid = int(open(pid_file).read().strip())
            os.kill(pid, 0)
            return {"status": "already_running"}
        except (ProcessLookupError, ValueError):
            os.unlink(pid_file)
    subprocess.Popen(
        ["/opt/speedy-vpn-backend/.venv/bin/python3", "/opt/proxy_scanner/proxy_scanner.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return {"status": "started"}

@v1_admin_router.post("/parser/stop")
def stop_parser(claims: SessionClaims = Depends(require_admin)) -> dict[str, str]:
    with open("/opt/proxy_scanner/stop_signal", "w") as f:
        f.write("stop")
    return {"status": "stop_signal_sent"}


@v1_admin_router.get("/system/stats", response_model=SystemStatsResponse)
def get_v1_system_stats(
    claims: SessionClaims = Depends(require_admin),
) -> SystemStatsResponse:
    return get_system_stats(_claims=claims)


@v1_admin_router.get("/system/inbounds", response_model=list[MarzbanInboundInfo])
def get_v1_system_inbounds(
    claims: SessionClaims = Depends(require_admin),
) -> list[MarzbanInboundInfo]:
    return list_inbounds(_claims=claims)


@v1_admin_router.get("/system/hosts")
def get_v1_system_hosts(
    claims: SessionClaims = Depends(require_admin),
) -> dict[str, list[dict[str, Any]]]:
    return list_hosts(_claims=claims)


@v1_admin_router.get("/marzban/users")
def get_v1_marzban_users(
    claims: SessionClaims = Depends(require_admin),
) -> dict[str, Any]:
    return list_marzban_users(_claims=claims)


@v1_admin_router.post("/marzban/users/create", response_model=MarzbanUserInfo)
def create_v1_marzban_user(
    payload: CreateMarzbanUserRequest,
    claims: SessionClaims = Depends(require_admin),
) -> MarzbanUserInfo:
    return create_marzban_user(payload=payload, _claims=claims)


@v1_admin_router.get("/marzban/users/{username}", response_model=MarzbanUserInfo)
def get_v1_marzban_user(
    username: str,
    claims: SessionClaims = Depends(require_admin),
) -> MarzbanUserInfo:
    return get_marzban_user(username=username, _claims=claims)


@v1_admin_router.post("/inventory/refresh", response_model=InventoryRefreshResponse)
def refresh_v1_inventory(
    username: str = "admin_web_test",
    claims: SessionClaims = Depends(require_admin),
) -> InventoryRefreshResponse:
    return refresh_inventory(username=username, _claims=claims)


@v1_admin_router.get("/inventory/configs/{location_id}", response_model=VpnConfigResponse)
def get_v1_inventory_config(
    location_id: str,
    username: str = "admin_web_test",
    claims: SessionClaims = Depends(require_admin),
) -> VpnConfigResponse:
    return get_config_for_location(location_id=location_id, username=username, _claims=claims)


@v1_admin_router.post("/test/full-flow", response_model=QuickTestResponse)
def run_v1_full_flow_test(
    device_id: str = "admin-web-test-001",
    device_label: str = "Admin Web Test Device",
    claims: SessionClaims = Depends(require_admin),
) -> QuickTestResponse:
    return full_integration_test(device_id=device_id, device_label=device_label, _claims=claims)

# ─────────────────────────────────────────────
#  System & Infrastructure
# ─────────────────────────────────────────────

@admin_router.get(
    "/system/stats",
    response_model=SystemStatsResponse,
    summary="Marzban System Stats (real)",
    description="Returns live CPU, memory, bandwidth and user counters from the Marzban panel.",
)
def get_system_stats(
    _claims: SessionClaims = Depends(require_admin),
) -> SystemStatsResponse:
    try:
        raw = _get_marzban().get_system_stats()
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return SystemStatsResponse(
        version=str(raw.get("version", "")),
        mem_total=int(raw.get("mem_total", 0)),
        mem_used=int(raw.get("mem_used", 0)),
        cpu_cores=int(raw.get("cpu_cores", 0)),
        cpu_usage=float(raw.get("cpu_usage", 0.0)),
        total_user=int(raw.get("total_user", 0)),
        users_active=int(raw.get("users_active", 0)),
        incoming_bandwidth=int(raw.get("incoming_bandwidth", 0)),
        outgoing_bandwidth=int(raw.get("outgoing_bandwidth", 0)),
        incoming_bandwidth_speed=int(raw.get("incoming_bandwidth_speed", 0)),
        outgoing_bandwidth_speed=int(raw.get("outgoing_bandwidth_speed", 0)),
        raw=raw,
    )


@admin_router.get(
    "/system/inbounds",
    response_model=list[MarzbanInboundInfo],
    summary="List all Xray inbounds (real)",
    description="Returns every inbound (protocol+port+tag) configured in Marzban. This is what the VPN client connects to.",
)
def list_inbounds(
    _claims: SessionClaims = Depends(require_admin),
) -> list[MarzbanInboundInfo]:
    try:
        inbounds = _get_marzban().get_inbounds()
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    result: list[MarzbanInboundInfo] = []
    for protocol, entries in inbounds.items():
        for entry in entries:
            tag = entry.get("tag", "")
            port = entry.get("port", 0)
            stream = entry.get("stream_settings") or entry.get("streamSettings") or {}
            network = str(stream.get("network", "tcp") if isinstance(stream, dict) else "tcp")
            security = str(stream.get("security", "none") if isinstance(stream, dict) else "none")
            result.append(MarzbanInboundInfo(
                tag=str(tag),
                protocol=str(protocol),
                port=int(port) if isinstance(port, int) else 0,
                network=network,
                security=security,
                settings_preview=str(entry)[:300],
            ))
    return result


@admin_router.get(
    "/system/hosts",
    summary="List all Marzban hosts (real, raw JSON)",
    description="Returns the raw hosts configuration from Marzban.",
)
def list_hosts(
    _claims: SessionClaims = Depends(require_admin),
) -> dict[str, list[dict[str, Any]]]:
    try:
        return _get_marzban().get_hosts()
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ─────────────────────────────────────────────
#  User Management
# ─────────────────────────────────────────────

@admin_router.post(
    "/users/create",
    response_model=MarzbanUserInfo,
    summary="Create Marzban user (real)",
    description="Creates a real user in Marzban with all available inbounds. The user will have unlimited traffic and no expiry. This is the same logic the app uses via guest auth.",
)
def create_marzban_user(
    payload: CreateMarzbanUserRequest,
    _claims: SessionClaims = Depends(require_admin),
) -> MarzbanUserInfo:
    try:
        raw = _get_marzban().ensure_managed_user(
            username=payload.username,
            note=payload.note,
        )
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _raw_to_user_info(raw)


@admin_router.get(
    "/users/{username}",
    response_model=MarzbanUserInfo,
    summary="Get Marzban user details (real)",
    description="Returns full details for a specific Marzban user including traffic usage, proxies, and subscription URL.",
)
def get_marzban_user(
    username: str,
    _claims: SessionClaims = Depends(require_admin),
) -> MarzbanUserInfo:
    try:
        raw = _get_marzban().get_user(username)
    except MarzbanError as exc:
        if "404" in str(exc):
            raise HTTPException(status_code=404, detail=f"User '{username}' not found in Marzban.") from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _raw_to_user_info(raw)


@admin_router.get(
    "/users",
    summary="List all Marzban users (real, raw JSON)",
    description="Returns a list of all users from Marzban. Shows the raw Marzban response.",
)
def list_marzban_users(
    _claims: SessionClaims = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _get_marzban().list_users()
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ─────────────────────────────────────────────
#  Inventory & VPN Config
# ─────────────────────────────────────────────

@admin_router.post(
    "/inventory/refresh",
    response_model=InventoryRefreshResponse,
    summary="Force refresh server inventory (real)",
    description="Forces a full inventory rebuild from Marzban subscription configs. Creates/updates a Marzban user, fetches all subscription configs, probes every endpoint for latency. Returns the complete server list with VPN configs.",
)
def refresh_inventory(
    username: str = "admin_swagger_test",
    _claims: SessionClaims = Depends(require_admin),
) -> InventoryRefreshResponse:
    try:
        snapshot = _get_marzban().build_inventory(
            username=username,
            display_name="Swagger Admin Test",
        )
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return InventoryRefreshResponse(
        locations=snapshot.locations,
        config_ids=list(snapshot.config_by_location_id.keys()),
        default_location_id=snapshot.default_location_id,
        server_ip=snapshot.server_ip,
        user_status=snapshot.user_status,
        stale=snapshot.stale,
        fetched_at=snapshot.fetched_at,
    )


@admin_router.get(
    "/inventory/configs/{location_id}",
    response_model=VpnConfigResponse,
    summary="Get VPN config for location (real)",
    description="Returns the full Xray JSON config for a specific server/location. Use inventory/refresh first to get location IDs.",
)
def get_config_for_location(
    location_id: str,
    username: str = "admin_swagger_test",
    _claims: SessionClaims = Depends(require_admin),
) -> VpnConfigResponse:
    try:
        snapshot = _get_marzban().build_inventory(
            username=username,
            display_name="Swagger Admin Test",
        )
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    config_profiles = snapshot.config_by_location_id.get(location_id)
    if config_profiles is None:
        available = list(snapshot.config_by_location_id.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Location '{location_id}' not found. Available: {available}",
        )
    config_json = config_profiles.get("balanced")
    if config_json is None:
        raise HTTPException(status_code=404, detail="Balanced routing profile not found.")

    loc = next((l for l in snapshot.locations if l.id == location_id), None)
    return VpnConfigResponse(
        server_id=location_id,
        engine="xray",
        protocol=loc.protocol if loc else "unknown",
        config_json=config_json,
        updated_at=snapshot.fetched_at,
    )


# ─────────────────────────────────────────────
#  Full Integration Test
# ─────────────────────────────────────────────

@admin_router.post(
    "/test/full-flow",
    response_model=QuickTestResponse,
    summary="🚀 Full integration test (auth → bootstrap → config → profile)",
    description=(
        "Runs the complete app flow in one request with REAL data:\n"
        "1. Guest auth (creates Marzban user)\n"
        "2. Bootstrap (loads session + locations + subscription)\n"
        "3. VPN config (generates Xray JSON for first server)\n"
        "4. Profile (loads user profile)\n"
        "5. Subscription (loads subscription state)\n\n"
        "All from real Marzban panel — nothing mocked."
    ),
)
def full_integration_test(
    device_id: str = "swagger-test-001",
    device_label: str = "Swagger Test Device",
    _claims: SessionClaims = Depends(require_admin),
) -> QuickTestResponse:
    speedy = _get_speedy()

    # Step 1: Auth
    try:
        exchange_payload = TelegramExchangeRequest(
            telegram_auth_payload={},
            device_id=device_id,
            device_label=device_label,
            platform="swagger",
        )
        session = speedy.exchange_auth(exchange_payload)
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=f"Auth failed: {exc}") from exc

    # Decode claims for subsequent calls
    from app.services.auth_tokens import decode_app_token
    claims = decode_app_token(settings, session.auth_token)

    # Step 2: Bootstrap
    try:
        boot = speedy.build_bootstrap(claims)
    except MarzbanError as exc:
        raise HTTPException(status_code=503, detail=f"Bootstrap failed: {exc}") from exc

    # Step 3: VPN Config for first location
    first_loc = boot.locations[0] if boot.locations else None
    vpn_config_server_id = ""
    vpn_config_protocol = ""
    vpn_config_engine = ""
    step_vpn = "SKIPPED — no locations"
    if first_loc:
        try:
            cfg = speedy.get_vpn_config(claims, first_loc.id)
            vpn_config_server_id = cfg.server_id
            vpn_config_protocol = cfg.protocol
            vpn_config_engine = cfg.engine
            step_vpn = "OK"
        except Exception as exc:
            step_vpn = f"FAILED: {exc}"

    # Step 4: Profile
    try:
        profile = speedy.get_profile(claims)
        step_profile = "OK"
    except Exception as exc:
        profile = None
        step_profile = f"FAILED: {exc}"

    # Step 5: Subscription
    try:
        sub = speedy.get_subscription(claims)
        step_sub = "OK"
    except Exception as exc:
        sub = None
        step_sub = f"FAILED: {exc}"

    return QuickTestResponse(
        step_auth="OK",
        user_id=session.user_id,
        auth_token=session.auth_token,
        step_bootstrap="OK",
        locations_count=len(boot.locations),
        locations=boot.locations,
        step_vpn_config=step_vpn,
        vpn_config_server_id=vpn_config_server_id,
        vpn_config_protocol=vpn_config_protocol,
        vpn_config_engine=vpn_config_engine,
        step_profile=step_profile,
        profile_display_name=profile.display_name if profile else "",
        profile_ip=profile.current_ip_address if profile else "",
        step_subscription=step_sub,
        subscription_status=sub.status.value if sub else "",
        all_ok=(
            step_vpn == "OK"
            and step_profile == "OK"
            and step_sub == "OK"
            and len(boot.locations) > 0
        ),
    )


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _raw_to_user_info(raw: dict[str, Any]) -> MarzbanUserInfo:
    return MarzbanUserInfo(
        username=str(raw.get("username", "")),
        status=str(raw.get("status", "")),
        data_limit=raw.get("data_limit"),
        data_limit_reset_strategy=raw.get("data_limit_reset_strategy"),
        expire=raw.get("expire"),
        used_traffic=int(raw.get("used_traffic", 0)),
        lifetime_used_traffic=int(raw.get("lifetime_used_traffic", 0)),
        created_at=str(raw.get("created_at", "")),
        online_at=str(raw.get("online_at", "")),
        sub_updated_at=str(raw.get("sub_updated_at", "")),
        subscription_url=raw.get("subscription_url"),
        links=list(raw.get("links", [])),
        note=raw.get("note"),
        on_hold_expire_duration=raw.get("on_hold_expire_duration"),
        on_hold_timeout=raw.get("on_hold_timeout"),
        proxies=raw.get("proxies", {}),
        inbounds=raw.get("inbounds", {}),
    )

class UpdatePlanPriceRequest(BaseModel):
    price_rub: int = Field(..., ge=0, description="Новая цена в рублях")

@v1_admin_router.put("/catalog/plans/{plan_id}/price", response_model=Plan)
def update_plan_price(
    plan_id: str,
    payload: UpdatePlanPriceRequest,
    claims: SessionClaims = Depends(require_admin),
    service: SpeedyFacadeService = Depends(get_speedy_service),
) -> Plan:
    try:
        updated = service.update_plan_price(plan_id, payload.price_rub)
        return service._plan_to_contract(updated)
    except KeyError:
        raise HTTPException(status_code=404, detail="Plan not found")
