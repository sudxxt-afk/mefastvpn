from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.services.catalog_store import CatalogStore, DevicePushTarget


@dataclass(slots=True)
class PushDispatchResult:
    targeted_devices: int
    delivered_devices: int


class PushGateway:
    def send(
        self,
        *,
        targets: Iterable[DevicePushTarget],
        title: str,
        body: str,
        data: dict[str, str],
    ) -> PushDispatchResult:
        raise NotImplementedError


class FcmLegacyPushGateway(PushGateway):
    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http = http_client or httpx.Client(timeout=10.0)

    def send(
        self,
        *,
        targets: Iterable[DevicePushTarget],
        title: str,
        body: str,
        data: dict[str, str],
    ) -> PushDispatchResult:
        tokens = [target.push_token for target in targets if target.push_token.strip()]
        if not tokens:
            return PushDispatchResult(targeted_devices=0, delivered_devices=0)
        if not self._settings.fcm_server_key.strip():
            return PushDispatchResult(targeted_devices=len(tokens), delivered_devices=0)

        delivered = 0
        for chunk in _chunked(tokens, 500):
            response = self._http.post(
                "https://fcm.googleapis.com/fcm/send",
                headers={
                    "Authorization": f"key={self._settings.fcm_server_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "registration_ids": chunk,
                    "priority": "high",
                    "content_available": True,
                    "notification": {"title": title, "body": body},
                    "data": data,
                },
            )
            response.raise_for_status()
            payload = response.json()
            delivered += int(payload.get("success", 0))
        return PushDispatchResult(
            targeted_devices=len(tokens),
            delivered_devices=delivered,
        )


class PushNotifier:
    def __init__(
        self,
        settings: Settings,
        store: CatalogStore,
        gateway: PushGateway | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._gateway = gateway or FcmLegacyPushGateway(settings)

    def notify_catalog_refresh(self, *, reason: str) -> PushDispatchResult:
        targets = self._store.list_push_targets()
        return self._gateway.send(
            targets=targets,
            title="MeFast VPN catalog updated",
            body="Open the app to refresh servers and routes.",
            data={"type": "catalog_refresh", "reason": reason},
        )

    def notify_subscription_update(
        self,
        *,
        user_id: str,
        title: str,
        body: str,
        event_type: str,
        reason: str,
    ) -> PushDispatchResult:
        targets = [
            target
            for target in self._store.list_push_targets()
            if target.user_id == user_id
        ]
        return self._gateway.send(
            targets=targets,
            title=title,
            body=body,
            data={"type": event_type, "reason": reason},
        )

    def notify_app_update(
        self,
        *,
        title: str,
        body: str,
        version_name: str,
        version_code: int,
        download_url: str,
        sha256: str,
        release_notes: str,
    ) -> PushDispatchResult:
        targets = [
            target
            for target in self._store.list_push_targets()
            if target.platform.strip().lower() == "android"
        ]
        return self._gateway.send(
            targets=targets,
            title=title,
            body=body,
            data={
                "type": "app_update",
                "version_name": version_name,
                "version_code": str(version_code),
                "download_url": download_url,
                "sha256": sha256,
                "release_notes": release_notes,
            },
        )


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]
