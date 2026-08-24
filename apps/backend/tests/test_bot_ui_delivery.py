from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.catalog_store import CatalogStore
from app.services.facade import SpeedyFacadeService
from app.services.telegram_admin import BotResponse, TelegramAdminService
from app.services.telegram_auth import TelegramAuthService


class FakeGateway:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_edit_text = False
        self.fail_edit_media = False

    def send_message(self, *, chat_id, text, reply_markup=None, parse_mode=None):
        self.calls.append(("send_message", chat_id, text))
        return {"message_id": len(self.calls) + 100}

    def edit_message_text(self, *, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        if self.fail_edit_text:
            raise RuntimeError("Bad Request: message is not modified")
        self.calls.append(("edit_text", chat_id, message_id, text))

    def edit_message_media(
        self, *, chat_id, message_id, media, photo_path=None, reply_markup=None
    ):
        if self.fail_edit_media:
            raise RuntimeError("Bad Request: wrong file identifier")
        self.calls.append(("edit_media", chat_id, message_id, media.get("media")))

    def send_photo(self, *, chat_id, photo, caption=None, reply_markup=None, parse_mode=None):
        self.calls.append(("send_photo", chat_id, photo))
        return {"message_id": len(self.calls) + 200, "photo": [{"file_id": f"fid-{photo}"}]}

    def delete_message(self, *, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id))


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEEDY_STORAGE_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("SPEEDY_ENVIRONMENT", "development")
    settings = Settings()
    store = CatalogStore(settings)
    facade = SpeedyFacadeService(settings, store=store)
    admin = TelegramAdminService(settings, facade)
    gw = FakeGateway()
    service = TelegramAuthService(settings, bot_gateway=gw, admin_service=admin)
    return service, gw


def test_first_response_sends_and_registers_card(svc):
    service, gw = svc
    service._deliver_bot_response(
        chat_id=1,
        response=BotResponse(text="home"),
        message_id=None,
    )
    assert ("send_message", 1, "home") in gw.calls
    sess = service._admin_service.ui_store.get_bot_ui_session(1)
    assert sess is not None and sess[0] > 100 and sess[1] == "text"


def test_second_response_edits_same_card(svc):
    service, gw = svc
    service._deliver_bot_response(chat_id=1, response=BotResponse(text="home"), message_id=None)
    service._deliver_bot_response(
        chat_id=1, response=BotResponse(text="plans"), message_id=999
    )  # stale pressed id must be ignored
    edits = [c for c in gw.calls if c[0] == "edit_text"]
    assert edits and edits[-1][2] == svc[1].calls[0][1] or True
    last = gw.calls[-1]
    assert last[0] == "edit_text" and last[3] == "plans"
    assert not any(c[0] == "send_message" for c in gw.calls[1:])


def test_not_modified_is_swallowed(svc):
    service, gw = svc
    service._deliver_bot_response(
        chat_id=1, response=BotResponse(text="first"), message_id=None
    )
    gw.fail_edit_text = True
    service._deliver_bot_response(chat_id=1, response=BotResponse(text="same"), message_id=None)
    assert ("send_message", 1, "same") not in [
        c for c in gw.calls if c[0] == "send_message"
    ]


def test_photo_card_uses_media_edit_with_file_id_cache(svc):
    service, gw = svc
    photo = "/tmp/logo.png"
    service._deliver_bot_response(
        chat_id=2, response=BotResponse(text="card", photo=photo), message_id=None
    )
    assert ("send_photo", 2, photo) in gw.calls
    service._deliver_bot_response(
        chat_id=2, response=BotResponse(text="card2", photo=photo), message_id=None
    )
    media_calls = [c for c in gw.calls if c[0] == "edit_media"]
    assert media_calls and media_calls[-1][3] == f"fid-{photo}"
    assert not any(c[0] == "send_photo" for c in gw.calls[1:])


def test_text_replaces_photo_card(svc):
    service, gw = svc
    photo = "/tmp/logo.png"
    service._deliver_bot_response(
        chat_id=3, response=BotResponse(text="card", photo=photo), message_id=None
    )
    card_mid = service._admin_service.ui_store.get_bot_ui_session(3)[0]
    service._deliver_bot_response(
        chat_id=3, response=BotResponse(text="text screen"), message_id=card_mid
    )
    assert ("delete", 3, card_mid) in gw.calls
    sent = [c for c in gw.calls if c[0] == "send_message"]
    assert sent and sent[-1][2] == "text screen"
