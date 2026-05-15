from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.db.storage import Request, Warehouse


BTN_LOGIN = "🔑 Войти"
BTN_WAREHOUSES = "📦 Склады"
BTN_ADD = "➕ Заявка"
BTN_QUEUE = "📋 Очередь"
BTN_RUN = "▶️ Запуск"
BTN_STATUS = "⚙️ Статус"
BTN_CANCEL_FSM = "✖️ Отмена"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LOGIN), KeyboardButton(text=BTN_WAREHOUSES)],
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_QUEUE)],
            [KeyboardButton(text=BTN_RUN), KeyboardButton(text=BTN_STATUS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def fsm_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL_FSM)]],
        resize_keyboard=True,
        is_persistent=False,
    )


def warehouses_kb(warehouses: list[Warehouse], prefix: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for w in warehouses:
        row.append(InlineKeyboardButton(text=w.name, callback_data=f"{prefix}:{w.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_kb(requests: list[Request]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in requests:
        if r.status != "pending":
            continue
        label = f"❌ #{r.id} {r.article} ×{r.quantity}"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"qcancel:{r.id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="queue:refresh")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text="✖️ Нет", callback_data=f"{prefix}:no"),
            ]
        ]
    )
