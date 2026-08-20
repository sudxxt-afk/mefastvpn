import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from threading import Event, Thread

from app.api.admin_routes import admin_router, v1_admin_router
from app.api.routes import auth_service, public_router, router
from app.config import settings

_logger = logging.getLogger(__name__)

_telegram_stop_event = Event()
_telegram_poll_thread: Thread | None = None


def _telegram_poll_loop() -> None:
    while not _telegram_stop_event.wait(settings.telegram_bot_poll_interval_seconds):
        try:
            auth_service.process_updates()
        except Exception:
            _logger.exception("Telegram poll error")
            continue


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _telegram_poll_thread
    if (
        _telegram_poll_thread is None
        and settings.telegram_bot_token.strip()
        and settings.telegram_bot_username.strip()
    ):
        _telegram_stop_event.clear()
        _telegram_poll_thread = Thread(
            target=_telegram_poll_loop,
            name="speedy-telegram-poll",
            daemon=True,
        )
        _telegram_poll_thread.start()
    yield
    _telegram_stop_event.set()
    if _telegram_poll_thread is not None:
        _telegram_poll_thread.join(timeout=3)
        _telegram_poll_thread = None

app = FastAPI(
    title="MeFast VPN — Backend Facade",
    description=(
        "## Real Marzban Integration — Nothing Mocked\n\n"
        "Этот API — фасад перед Marzban панелью. Все данные **реальные**:\n"
        "серверы, пользователи, VPN конфиги, статистика.\n\n"
        "### Быстрый тест\n"
        "1. Откройте **🔧 Admin → /admin/test/full-flow** и нажмите Execute\n"
        "2. Вы получите результат полного flow: auth → bootstrap → config → profile\n\n"
        "### App API (для Flutter клиента)\n"
        "- `POST /v1/auth/telegram/exchange` — guest auth (без Telegram)\n"
        "- `GET /v1/bootstrap` — session + серверы + подписка\n"
        "- `GET /v1/locations` — список серверов\n"
        "- `GET /v1/vpn/config?location_id=...` — Xray JSON конфиг\n\n"
        "### Admin API (для тестирования)\n"
        "- `GET /admin/system/stats` — CPU, RAM, трафик из Marzban\n"
        "- `GET /admin/system/inbounds` — все Xray протоколы\n"
        "- `POST /admin/users/create` — создать пользователя\n"
        "- `GET /admin/users` — список всех пользователей\n"
        "- `POST /admin/inventory/refresh` — обновить серверы\n"
        "- `POST /admin/test/full-flow` — 🚀 полный тест\n"
    ),
    version="1.0.0",
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url="/redoc" if settings.environment == "development" else None,
    openapi_tags=[
        {
            "name": "health",
            "description": "Health check",
        },
        {
            "name": "speedy-vpn",
            "description": "App API — используется Flutter клиентом. Все данные реальные из Marzban.",
        },
        {
            "name": "🔧 Admin — Real Marzban",
            "description": (
                "Admin эндпоинты для тестирования через Swagger. "
                "Все операции идут напрямую в Marzban — ничего не замокано."
            ),
        },
        {
            "name": "Admin Web App",
            "description": "Admin эндпоинты для веб-панели управления.",
        },
    ],
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-App-Platform"],
)

app.include_router(router)
app.include_router(public_router)
app.include_router(admin_router)
app.include_router(v1_admin_router)


@app.get("/healthz", tags=["health"])
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
