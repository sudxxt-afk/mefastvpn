from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings


class YooKassaError(RuntimeError):
    pass


@dataclass(slots=True)
class YooKassaPayment:
    payment_id: str
    status: str
    paid: bool
    confirmation_url: str | None = None


class YooKassaClient:
    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = http_client or httpx.Client(timeout=settings.yookassa_timeout_seconds)

    def create_payment(
        self,
        *,
        amount_rub: int,
        order_id: str,
        description: str,
        return_url: str,
        idempotence_key: str,
        metadata: dict[str, str],
    ) -> YooKassaPayment:
        payload: dict[str, object] = {
            "amount": {
                "value": f"{amount_rub:.2f}",
                "currency": "RUB",
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "description": description[:128],
            "metadata": {
                "order_id": order_id,
                **metadata,
            },
        }
        data = self._request(
            "POST",
            "/payments",
            json=payload,
            headers={"Idempotence-Key": idempotence_key},
        )
        payment = self._parse_payment(data)
        if not payment.confirmation_url and payment.status == "pending":
            raise YooKassaError("YooKassa did not return confirmation_url.")
        return payment

    def get_payment(self, payment_id: str) -> YooKassaPayment:
        data = self._request("GET", f"/payments/{payment_id}")
        return self._parse_payment(data)

    def _request(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
        shop_id = self._settings.yookassa_shop_id.strip()
        secret_key = self._settings.yookassa_secret_key.strip()
        if not shop_id or not secret_key:
            raise YooKassaError("YooKassa credentials are not configured.")

        base_url = self._settings.yookassa_api_base_url.rstrip("/")
        try:
            response = self._client.request(
                method,
                f"{base_url}{path}",
                auth=(shop_id, secret_key),
                **kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise YooKassaError(f"YooKassa API request failed: {exc}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise YooKassaError("YooKassa API returned invalid response.")
        return data

    @staticmethod
    def _parse_payment(data: dict[str, object]) -> YooKassaPayment:
        payment_id = data.get("id")
        status = data.get("status")
        paid = data.get("paid")
        confirmation = data.get("confirmation")
        confirmation_url = None
        if isinstance(confirmation, dict):
            raw_url = confirmation.get("confirmation_url")
            confirmation_url = raw_url if isinstance(raw_url, str) else None
        if not isinstance(payment_id, str) or not isinstance(status, str):
            raise YooKassaError("YooKassa payment response misses id/status.")
        return YooKassaPayment(
            payment_id=payment_id,
            status=status,
            paid=bool(paid),
            confirmation_url=confirmation_url,
        )
