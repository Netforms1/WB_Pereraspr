import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    BTN_ADD,
    BTN_CANCEL_FSM,
    BTN_LOGIN,
    BTN_QUEUE,
    BTN_RUN,
    BTN_STATUS,
    BTN_WAREHOUSES,
    confirm_kb,
    fsm_cancel_kb,
    main_menu_kb,
    queue_kb,
    warehouses_kb,
)
from app.config import settings
from app.db import get_storage
from app.scheduler import run_attack_now
from app.wb import WBAuthError, WBError, get_wb_client

logger = logging.getLogger(__name__)

router = Router()


def _is_owner(uid: int) -> bool:
    return uid == settings.tg_owner_id


@router.message(F.from_user.id != settings.tg_owner_id)
async def _block_others(msg: Message) -> None:
    return


class LoginSG(StatesGroup):
    phone = State()
    code = State()


class AddSG(StatesGroup):
    from_wh = State()
    to_wh = State()
    article = State()
    quantity = State()
    confirm = State()


# ---------- /start --------------------------------------------------------

@router.message(CommandStart())
async def start(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await msg.answer(
        "Бот перераспределения остатков WB.\nВыбирайте действие на клавиатуре ниже.",
        reply_markup=main_menu_kb(),
    )


# ---------- Глобальная «✖️ Отмена» внутри FSM -----------------------------

@router.message(F.text == BTN_CANCEL_FSM)
async def fsm_cancel(msg: Message, state: FSMContext) -> None:
    cur = await state.get_state()
    await state.clear()
    if cur:
        await msg.answer("Отменено.", reply_markup=main_menu_kb())
    else:
        await msg.answer("Меню:", reply_markup=main_menu_kb())


# ---------- Логин ---------------------------------------------------------

@router.message(Command("login"))
@router.message(F.text == BTN_LOGIN)
async def cmd_login(msg: Message, state: FSMContext) -> None:
    await state.set_state(LoginSG.phone)
    await msg.answer(
        "Введите телефон в формате 79991234567",
        reply_markup=fsm_cancel_kb(),
    )


@router.message(LoginSG.phone)
async def login_phone(msg: Message, state: FSMContext) -> None:
    phone = (msg.text or "").strip()
    if not phone.isdigit() or len(phone) < 10:
        await msg.answer("Похоже на не-номер. Попробуйте ещё раз.")
        return
    wb = get_wb_client()
    try:
        await wb.request_sms_code(phone)
    except WBError as e:
        await state.clear()
        await msg.answer(f"WB отказал: {e}", reply_markup=main_menu_kb())
        return
    await state.set_state(LoginSG.code)
    await msg.answer("Код из SMS пришлите сюда.", reply_markup=fsm_cancel_kb())


@router.message(LoginSG.code)
async def login_code(msg: Message, state: FSMContext) -> None:
    code = (msg.text or "").strip()
    wb = get_wb_client()
    try:
        await wb.verify_sms_code(code)
    except WBError as e:
        await state.clear()
        await msg.answer(
            f"Не приняло код: {e}\nПопробуйте ещё раз.",
            reply_markup=main_menu_kb(),
        )
        return
    await state.clear()
    await msg.answer("Сессия сохранена.", reply_markup=main_menu_kb())


# ---------- Склады --------------------------------------------------------

@router.message(Command("warehouses"))
@router.message(F.text == BTN_WAREHOUSES)
async def cmd_warehouses(msg: Message) -> None:
    storage = get_storage()
    wb = get_wb_client()
    if not await wb.load():
        await msg.answer("Сначала войдите.", reply_markup=main_menu_kb())
        return
    await msg.answer("Обновляю список складов…")
    try:
        items = await wb.fetch_warehouses()
    except WBAuthError:
        await msg.answer("Сессия истекла. Нажмите «🔑 Войти».", reply_markup=main_menu_kb())
        return
    except WBError as e:
        await msg.answer(f"Не удалось получить склады: {e}", reply_markup=main_menu_kb())
        return
    await storage.upsert_warehouses(items)
    if not items:
        await msg.answer("Складов нет.", reply_markup=main_menu_kb())
        return
    lines = [
        f"• {w.name} — id {w.id}{' (недоступен)' if not w.available else ''}"
        for w in items
    ]
    await msg.answer("Склады:\n" + "\n".join(lines), reply_markup=main_menu_kb())


# ---------- Создание заявки ----------------------------------------------

@router.message(Command("add"))
@router.message(F.text == BTN_ADD)
async def cmd_add(msg: Message, state: FSMContext) -> None:
    storage = get_storage()
    warehouses = await storage.list_warehouses()
    if not warehouses:
        await msg.answer("Сначала обновите склады.", reply_markup=main_menu_kb())
        return
    await state.set_state(AddSG.from_wh)
    await msg.answer(
        "Откуда перераспределить?",
        reply_markup=fsm_cancel_kb(),
    )
    await msg.answer(
        "Выберите склад-источник:",
        reply_markup=warehouses_kb(warehouses, "from"),
    )


@router.callback_query(AddSG.from_wh, F.data.startswith("from:"))
async def add_from(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    if val == "cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        await cb.message.answer("Меню:", reply_markup=main_menu_kb())
        await cb.answer()
        return
    await state.update_data(from_wh=int(val))
    storage = get_storage()
    warehouses = await storage.list_warehouses(only_available=True)
    warehouses = [w for w in warehouses if w.id != int(val)]
    await state.set_state(AddSG.to_wh)
    await cb.message.edit_text("Куда?", reply_markup=warehouses_kb(warehouses, "to"))
    await cb.answer()


@router.callback_query(AddSG.to_wh, F.data.startswith("to:"))
async def add_to(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    if val == "cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        await cb.message.answer("Меню:", reply_markup=main_menu_kb())
        await cb.answer()
        return
    await state.update_data(to_wh=int(val))
    await state.set_state(AddSG.article)
    await cb.message.edit_text("Артикул WB (nmId или supplierArticle):")
    await cb.answer()


@router.message(AddSG.article)
async def add_article(msg: Message, state: FSMContext) -> None:
    article = (msg.text or "").strip()
    if not article:
        await msg.answer("Пустой артикул.")
        return
    await state.update_data(article=article)
    await state.set_state(AddSG.quantity)
    await msg.answer("Сколько единиц перераспределить?", reply_markup=fsm_cancel_kb())


@router.message(AddSG.quantity)
async def add_qty(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await msg.answer("Нужно положительное число.")
        return
    await state.update_data(quantity=int(text))
    data = await state.get_data()
    storage = get_storage()
    f_w = await storage.get_warehouse(data["from_wh"])
    t_w = await storage.get_warehouse(data["to_wh"])
    f_name = f_w.name if f_w else str(data["from_wh"])
    t_name = t_w.name if t_w else str(data["to_wh"])
    await state.set_state(AddSG.confirm)
    await msg.answer(
        f"Подтвердить заявку?\n\n"
        f"Артикул: {data['article']}\n"
        f"Кол-во: {data['quantity']}\n"
        f"Откуда: {f_name}\n"
        f"Куда: {t_name}",
        reply_markup=confirm_kb("addok"),
    )


@router.callback_query(AddSG.confirm, F.data.startswith("addok:"))
async def add_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    choice = cb.data.split(":", 1)[1]
    if choice != "yes":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        await cb.message.answer("Меню:", reply_markup=main_menu_kb())
        await cb.answer()
        return
    data = await state.get_data()
    storage = get_storage()
    rid = await storage.add_request(
        from_id=data["from_wh"],
        to_id=data["to_wh"],
        article=data["article"],
        quantity=data["quantity"],
    )
    await state.clear()
    await cb.message.edit_text(f"Заявка #{rid} добавлена в очередь.")
    await cb.message.answer("Меню:", reply_markup=main_menu_kb())
    await cb.answer()


# ---------- Очередь / отмена ---------------------------------------------

async def _render_queue() -> tuple[str, list]:
    storage = get_storage()
    items = await storage.list_requests()
    warehouses = {w.id: w.name for w in await storage.list_warehouses()}
    if not items:
        return "Очередь пуста.", []
    lines = []
    for r in items:
        f_name = warehouses.get(r.from_warehouse_id, str(r.from_warehouse_id))
        t_name = warehouses.get(r.to_warehouse_id, str(r.to_warehouse_id))
        icon = {"pending": "⏳", "done": "✅", "failed": "❌", "cancelled": "🚫"}.get(
            r.status, "•"
        )
        line = (
            f"{icon} #{r.id} {r.article} ×{r.quantity}: {f_name} → {t_name}"
            f" (попыток: {r.attempts})"
        )
        if r.last_error:
            line += f"\n   └ {r.last_error}"
        lines.append(line)
    return "\n".join(lines), items


@router.message(Command("queue"))
@router.message(F.text == BTN_QUEUE)
async def cmd_queue(msg: Message) -> None:
    text, items = await _render_queue()
    kb = queue_kb(items) if items else None
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data == "queue:refresh")
async def queue_refresh(cb: CallbackQuery) -> None:
    text, items = await _render_queue()
    kb = queue_kb(items) if items else None
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("Обновлено")


@router.callback_query(F.data.startswith("qcancel:"))
async def queue_cancel(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    storage = get_storage()
    ok = await storage.cancel_request(rid)
    text, items = await _render_queue()
    kb = queue_kb(items) if items else None
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await cb.answer("Отменено" if ok else "Уже не pending")


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message) -> None:
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Использование: /cancel <id> либо кнопка ❌ в /queue.")
        return
    storage = get_storage()
    ok = await storage.cancel_request(int(parts[1]))
    await msg.answer("Отменено." if ok else "Не нашёл pending-заявку с таким id.")


# ---------- Запуск штурма / статус ---------------------------------------

@router.message(Command("run"))
@router.message(F.text == BTN_RUN)
async def cmd_run(msg: Message, bot: Bot, state: FSMContext) -> None:
    await state.set_state(None)
    await msg.answer(
        "Запустить штурм сейчас? Это начнёт долбить WB немедленно.",
        reply_markup=confirm_kb("run"),
    )


@router.callback_query(F.data.startswith("run:"))
async def run_confirm(cb: CallbackQuery, bot: Bot) -> None:
    choice = cb.data.split(":", 1)[1]
    if choice != "yes":
        await cb.message.edit_text("Отменено.")
        await cb.answer()
        return
    await cb.message.edit_text("🚀 Штурм запущен.")
    await cb.answer()
    await run_attack_now(bot)


@router.message(Command("status"))
@router.message(F.text == BTN_STATUS)
async def cmd_status(msg: Message) -> None:
    storage = get_storage()
    has_session = (await storage.load_session()) is not None
    pending = len(await storage.list_requests(status="pending"))
    done = len(await storage.list_requests(status="done"))
    failed = len(await storage.list_requests(status="failed"))
    wh = len(await storage.list_warehouses())
    await msg.answer(
        f"Сессия: {'✅ есть' if has_session else '⛔ нет — нажмите «🔑 Войти»'}\n"
        f"Складов в кэше: {wh}\n"
        f"⏳ Pending: {pending}\n"
        f"✅ Done: {done}\n"
        f"❌ Failed: {failed}",
        reply_markup=main_menu_kb(),
    )


# ---------- fallback ------------------------------------------------------

@router.message()
async def fallback(msg: Message) -> None:
    await msg.answer("Не понял. Воспользуйтесь кнопками меню.", reply_markup=main_menu_kb())


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp
