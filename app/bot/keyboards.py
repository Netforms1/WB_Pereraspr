from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.storage import Warehouse


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
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
