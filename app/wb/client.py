"""WB seller cabinet client на Playwright.

WB защищает кабинет слайдер-капчей и подписью X-Req-Sign на каждом
запросе. Голый HTTP не пройдёт — поэтому работаем через настоящий
Chromium: пользователь один раз логинится руками (в видимом окне),
бот сохраняет storage_state (cookies + localStorage), потом в headless
режиме открывает страницу перераспределения и кликает кнопки.
"""

from __future__ import annotations

import logging
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from app.db import Storage, get_storage
from app.db.storage import Warehouse

logger = logging.getLogger(__name__)


SELLER_AUTH_URL = "https://seller-auth.wildberries.ru/"
SELLER_HOME_URL = "https://seller.wildberries.ru/"
# TODO(WB): уточнить точный URL страницы перераспределения остатков.
REDISTRIBUTE_URL = "https://seller.wildberries.ru/stocks-redistribution"


class WBError(Exception):
    pass


class WBAuthError(WBError):
    pass


class WBNotReadyError(WBError):
    """Слот ещё закрыт (типично до 9:00 МСК)."""


class WBClient:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._pw: Optional[Playwright] = None

        # Видимое окно для интерактивного логина.
        self._login_browser: Optional[Browser] = None
        self._login_ctx: Optional[BrowserContext] = None
        self._login_page: Optional[Page] = None

        # Headless для фоновой работы.
        self._bg_browser: Optional[Browser] = None
        self._bg_ctx: Optional[BrowserContext] = None

    async def _start_pw(self) -> Playwright:
        if self._pw is None:
            self._pw = await async_playwright().start()
        return self._pw

    async def close(self) -> None:
        for ctx in (self._login_ctx, self._bg_ctx):
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
        for br in (self._login_browser, self._bg_browser):
            if br:
                try:
                    await br.close()
                except Exception:
                    pass
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def load(self) -> bool:
        """True, если есть сохранённая сессия."""
        return (await self.storage.load_session()) is not None

    # --- Интерактивный логин ----------------------------------------------

    async def login_open(self) -> None:
        """Открывает видимое окно с формой входа WB."""
        if self._login_browser is not None:
            await self.login_abort()
        pw = await self._start_pw()
        self._login_browser = await pw.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        self._login_ctx = await self._login_browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._login_page = await self._login_ctx.new_page()
        await self._login_page.goto(SELLER_AUTH_URL)

    async def login_finish(self) -> None:
        """Снимает storage_state из видимого окна и сохраняет в БД."""
        if not self._login_ctx:
            raise WBError("call login_open first")
        state = await self._login_ctx.storage_state()
        if not state.get("cookies"):
            raise WBAuthError("браузер пуст: похоже, ты ещё не залогинился")
        await self.storage.save_session(state)
        await self.login_abort()

    async def login_abort(self) -> None:
        if self._login_browser:
            try:
                await self._login_browser.close()
            except Exception:
                pass
        self._login_browser = None
        self._login_ctx = None
        self._login_page = None

    # --- Фоновый контекст --------------------------------------------------

    async def _ensure_bg(self) -> BrowserContext:
        if self._bg_ctx is not None:
            return self._bg_ctx
        state = await self.storage.load_session()
        if not state:
            raise WBAuthError("нет сохранённой сессии")
        pw = await self._start_pw()
        self._bg_browser = await pw.chromium.launch(headless=True)
        self._bg_ctx = await self._bg_browser.new_context(
            storage_state=state,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        return self._bg_ctx

    async def _refresh_state(self) -> None:
        """Перезаписать сохранённую сессию (cookies могут обновиться)."""
        if not self._bg_ctx:
            return
        try:
            state = await self._bg_ctx.storage_state()
            await self.storage.save_session(state)
        except Exception:
            logger.exception("failed to refresh state")

    # --- Склады ------------------------------------------------------------

    async def fetch_warehouses(self) -> list[Warehouse]:
        """Тянет список складов со страницы перераспределения."""
        ctx = await self._ensure_bg()
        page = await ctx.new_page()
        try:
            await page.goto(REDISTRIBUTE_URL, wait_until="networkidle")

            # Если WB кикнул на login — сессия мертва.
            if "auth" in page.url.lower() or "login" in page.url.lower():
                raise WBAuthError("redirected to login")

            # TODO(WB-UI): подставить реальные селекторы со страницы
            # перераспределения. Пример типичной стратегии:
            #   await page.click('button:has-text("Выбрать склад")')
            #   items = await page.query_selector_all('[role="option"]')
            #   for it in items:
            #       name = (await it.inner_text()).strip()
            #       wid = await it.get_attribute("data-id")
            # Пока возвращаем пусто, чтобы бот не падал.
            warehouses: list[Warehouse] = []

            await self._refresh_state()
            return warehouses
        finally:
            await page.close()

    # --- Перераспределение ------------------------------------------------

    async def submit_redistribution(
        self,
        from_warehouse_id: int,
        to_warehouse_id: int,
        article: str,
        quantity: int,
    ) -> None:
        ctx = await self._ensure_bg()
        page = await ctx.new_page()
        try:
            await page.goto(REDISTRIBUTE_URL, wait_until="networkidle")

            if "auth" in page.url.lower() or "login" in page.url.lower():
                raise WBAuthError("redirected to login")

            # TODO(WB-UI): подставить реальный сценарий клика по странице
            # перераспределения. Псевдокод:
            #   1) выбрать склад-источник по from_warehouse_id
            #   2) ввести/выбрать артикул
            #   3) ввести quantity
            #   4) выбрать склад-приёмник по to_warehouse_id
            #   5) кликнуть «Перераспределить»
            #   6) дождаться подтверждения / тоста / редиректа
            #   7) при ошибке «слот занят/закрыт» кинуть WBNotReadyError
            raise WBError(
                "submit_redistribution не реализован: нужны селекторы реальной страницы"
            )
        finally:
            await page.close()


_client: Optional[WBClient] = None


def get_wb_client() -> WBClient:
    global _client
    if _client is None:
        _client = WBClient(get_storage())
    return _client
