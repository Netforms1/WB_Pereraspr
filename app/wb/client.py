"""WB seller cabinet client.

ВНИМАНИЕ: у функции «Перераспределение остатков» нет публичного API.
Все эндпоинты ниже — внутренние, их нужно один раз снять из DevTools
браузера (вкладка Network на seller.wildberries.ru при выполнении
действия вручную) и подставить в TODO-местах.

Что нужно вытащить из DevTools:
  1. POST на отправку SMS-кода (request_sms_code)
  2. POST на проверку кода → возвращает токены/cookies (verify_sms_code)
  3. GET списка доступных складов (fetch_warehouses)
  4. POST перераспределения товара (submit_redistribution)
  5. GET артикулов/остатков по складу (опционально, для валидации)

Скелет рассчитан на то, что после первого SMS-логина WB отдаёт
JWT/cookies, которые мы кладём в httpx.Client. Если у WB сейчас
другая схема (например, x-supplier-id или CSRF) — добавьте поля
в _build_headers().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from app.db import Storage, get_storage
from app.db.storage import Warehouse

logger = logging.getLogger(__name__)


# TODO(WB): заменить на реальные URL из DevTools.
SELLER_BASE = "https://seller.wildberries.ru"
PASSPORT_BASE = "https://passport.wildberries.ru"

URL_SMS_REQUEST = f"{PASSPORT_BASE}/api/v2/auth/sms"  # TODO(WB)
URL_SMS_VERIFY = f"{PASSPORT_BASE}/api/v2/auth/verify"  # TODO(WB)
URL_WAREHOUSES = f"{SELLER_BASE}/ns/sm-warehouses/suppliers-portal-core/warehouses"  # TODO(WB)
URL_REDISTRIBUTE = f"{SELLER_BASE}/ns/sm-stocks/suppliers-portal-core/redistribute"  # TODO(WB)


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": SELLER_BASE,
    "Referer": f"{SELLER_BASE}/",
}


class WBError(Exception):
    """Базовая ошибка WB."""


class WBAuthError(WBError):
    """Сессия отсутствует / истекла / отвергнута WB."""


class WBNotReadyError(WBError):
    """Перераспределение ещё закрыто (типично до 9:00 МСК или при выкупе всех слотов)."""


@dataclass
class _Session:
    cookies: dict
    headers_extra: dict
    phone: Optional[str] = None


class WBClient:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._session: Optional[_Session] = None
        self._sms_token: Optional[str] = None
        self._http = httpx.AsyncClient(timeout=15.0, headers=DEFAULT_HEADERS, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    async def load(self) -> bool:
        data = await self.storage.load_session()
        if not data:
            return False
        self._session = _Session(
            cookies=data.get("cookies", {}),
            headers_extra=data.get("headers_extra", {}),
            phone=data.get("phone"),
        )
        return True

    async def _persist(self) -> None:
        assert self._session is not None
        await self.storage.save_session(
            {
                "cookies": self._session.cookies,
                "headers_extra": self._session.headers_extra,
                "phone": self._session.phone,
            }
        )

    def _build_headers(self) -> dict:
        h = dict(DEFAULT_HEADERS)
        if self._session:
            h.update(self._session.headers_extra)
        return h

    def _cookies(self) -> dict:
        return dict(self._session.cookies) if self._session else {}

    # --- Авторизация по SMS -------------------------------------------------

    async def request_sms_code(self, phone: str) -> None:
        """Шаг 1: запросить SMS-код. Phone в формате 79991234567."""
        # TODO(WB): подставить реальное тело запроса и заголовки.
        payload = {"phone": phone}
        r = await self._http.post(URL_SMS_REQUEST, json=payload)
        if r.status_code >= 400:
            raise WBAuthError(f"sms request failed: {r.status_code} {r.text[:200]}")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        # TODO(WB): обычно бэкенд возвращает токен/идентификатор попытки логина —
        # сохранить его, чтобы передать на verify-шаге.
        self._sms_token = data.get("token") or data.get("sticker") or data.get("sessionId")
        self._session = _Session(cookies=dict(r.cookies), headers_extra={}, phone=phone)

    async def verify_sms_code(self, code: str) -> None:
        """Шаг 2: подтвердить код. По успеху WB ставит cookies/JWT, сохраняем."""
        if not self._session:
            raise WBAuthError("call request_sms_code first")
        # TODO(WB): подставить реальное тело — обычно {token, code, phone}.
        payload = {"code": code, "token": self._sms_token, "phone": self._session.phone}
        r = await self._http.post(
            URL_SMS_VERIFY,
            json=payload,
            cookies=self._cookies(),
            headers=self._build_headers(),
        )
        if r.status_code >= 400:
            raise WBAuthError(f"sms verify failed: {r.status_code} {r.text[:200]}")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

        # WB обычно ставит cookies (WBToken, x-supplier-id, etc.) автоматически —
        # забираем их из r.cookies. Если же JWT приходит в теле, кладём в headers_extra.
        cookies = dict(self._session.cookies)
        cookies.update(dict(r.cookies))
        headers_extra: dict = {}
        if "token" in data:
            headers_extra["Authorization"] = f"Bearer {data['token']}"
        self._session = _Session(
            cookies=cookies, headers_extra=headers_extra, phone=self._session.phone
        )
        await self._persist()

    # --- Доступные склады ---------------------------------------------------

    async def fetch_warehouses(self) -> list[Warehouse]:
        """Тянет список складов, доступных для приёмки/перераспределения."""
        if not self._session:
            raise WBAuthError("not authenticated")
        r = await self._http.get(
            URL_WAREHOUSES,
            cookies=self._cookies(),
            headers=self._build_headers(),
        )
        if r.status_code == 401 or r.status_code == 403:
            raise WBAuthError(f"warehouses unauthorized: {r.status_code}")
        if r.status_code >= 400:
            raise WBError(f"warehouses fetch failed: {r.status_code} {r.text[:200]}")
        data = r.json()

        # TODO(WB): схема ответа зависит от эндпоинта. Подставить корректный парсинг.
        # Пример (на основе типичного формата suppliers-portal):
        items = data.get("data") or data.get("warehouses") or data
        result: list[Warehouse] = []
        for w in items:
            wid = w.get("id") or w.get("warehouseId")
            name = w.get("name") or w.get("warehouseName") or ""
            available = bool(w.get("isAvailable", w.get("available", True)))
            if wid is None:
                continue
            result.append(Warehouse(id=int(wid), name=str(name), available=available))
        return result

    # --- Перераспределение --------------------------------------------------

    async def submit_redistribution(
        self, from_warehouse_id: int, to_warehouse_id: int, article: str, quantity: int
    ) -> None:
        """Отправляет одну заявку на перераспределение.

        Кидает WBNotReadyError, если WB ещё не открыл слот (типично до 9:00 МСК
        либо все слоты к складу-приёмнику разобраны).
        """
        if not self._session:
            raise WBAuthError("not authenticated")

        # TODO(WB): подставить реальное тело запроса.
        payload = {
            "fromWarehouseId": from_warehouse_id,
            "toWarehouseId": to_warehouse_id,
            "article": article,
            "quantity": quantity,
        }
        r = await self._http.post(
            URL_REDISTRIBUTE,
            json=payload,
            cookies=self._cookies(),
            headers=self._build_headers(),
        )

        if r.status_code in (401, 403):
            raise WBAuthError(f"redistribute unauthorized: {r.status_code}")
        if r.status_code == 429:
            raise WBNotReadyError("rate limited")
        if r.status_code >= 500:
            raise WBNotReadyError(f"server {r.status_code}")
        if r.status_code >= 400:
            text = r.text[:300]
            # TODO(WB): по реальным кодам ошибок отличить "слот занят/закрыт"
            # от "невалидные данные". До этого считаем 4xx → not ready.
            lower = text.lower()
            if any(s in lower for s in ("not available", "closed", "недоступ", "закрыт", "занят")):
                raise WBNotReadyError(text)
            raise WBError(f"redistribute failed: {r.status_code} {text}")

        # Успех. Тело можно залогировать, но для логики не нужно.
        return None


_client: Optional[WBClient] = None


def get_wb_client() -> WBClient:
    global _client
    if _client is None:
        _client = WBClient(get_storage())
    return _client
