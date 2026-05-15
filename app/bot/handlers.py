import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import warehouses_kb
from app.config import settings
from app.db import get_storage
from app.scheduler import run_attack_now
from app.wb import WBAuthError, WBError, get_wb_client

logger = logging.getLogger(__name__)

router = Router()


def _is_owner(uid: int) -> bool:
    return uid == settings.tg_owner_id


class LoginSG(StatesGroup):
    phone = State()
    code = State()


class AddSG(StatesGroup):
    from_wh = State()
    to_wh = State()
    article = State()
    quantity = State()


@router.message(CommandStart())
async def start(msg: Message) -> None:
    if not _is_owner(msg.from_user.id):
        return
    await msg.answer(
        "Бот перераспределения остатков WB.\n\n"
        "Команды:\n"
        "/login — авторизация по телефону\n"
        "/warehouses — обновить и показать склады\n"
        "/add — создать заявку\n"
        "/queue — список заявок\n"
        "/cancel <id> — отменить заявку\n"
        "/run — запустить штурм вручную\n"
        "/status — состояние сессии"
    )


# ---------- /login --------------------------------------------------------

@router.message(Command("login"))
async def cmd_login(msg: Message, state: FSMContext) -> None:
    if not _is_owner(msg.from_user.id):
        return
    await state.set_state(LoginSG.phone)
    await msg.answer("Введите телефон в формате 79991234567")


@router.message(LoginSG.phone)
async def login_phone(msg: Message, state: FSMContext) -> None:
    phone = msg.text.strip() if msg.text else ""
    if not phone.isdigit() or len(phone) < 10:
        await msg.answer("Похоже на не-номер. Попробуйте ещё раз.")
        return
    wb = get_wb_client()
    try:
        await wb.request_sms_code(phone)
    except WBError as e:
        await msg.answer(f"WB отказал: {e}")
        await state.clear()
        return
    await state.set_state(LoginSG.code)
    await msg.answer("Код из SMS пришлите сюда.")


@router.message(LoginSG.code)
async def login_code(msg: Message, state: FSMContext) -> None:
    code = msg.text.strip() if msg.text else ""
    wb = get_wb_client()
    try:
        await wb.verify_sms_code(code)
    except WBError as e:
        await msg.answer(f"Не приняло код: {e}\nПовторите /login")
        await state.clear()
        return
    await state.clear()
    await msg.answer("Сессия сохранена. Можно /warehouses.")


# ---------- /warehouses ---------------------------------------------------

@router.message(Command("warehouses"))
async def cmd_warehouses(msg: Message) -> None:
    if not _is_owner(msg.from_user.id):
        return
    storage = get_storage()
    wb = get_wb_client()
    if not await wb.load():
        await msg.answer("Сначала /login.")
        return
    try:
        items = await wb.fetch_warehouses()
    except WBAuthError:
        await msg.answer("Сессия истекла. /login.")
        return
    except WBError as e:
        await msg.answer(f"Не удалось получить склады: {e}")
        return
    await storage.upsert_warehouses(items)
    if not items:
        await msg.answer("Складов нет.")
        return
    lines = [f"• {w.name} — id {w.id}{' (нет)' if not w.available else ''}" for w in items]
    await msg.answer("Склады:\n" + "\n".join(lines))


# ---------- /add ----------------------------------------------------------

@router.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext) -> None:
    if not _is_owner(msg.from_user.id):
        return
    storage = get_storage()
    warehouses = await storage.list_warehouses()
    if not warehouses:
        await msg.answer("Сначала обновите склады: /warehouses")
        return
    await state.set_state(AddSG.from_wh)
    await msg.answer("Откуда перераспределить?", reply_markup=warehouses_kb(warehouses, "from"))


@router.callback_query(AddSG.from_wh, F.data.startswith("from:"))
async def add_from(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(cb.from_user.id):
        return
    val = cb.data.split(":", 1)[1]
    if val == "cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
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
    if not _is_owner(cb.from_user.id):
        return
    val = cb.data.split(":", 1)[1]
    if val == "cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        return
    await state.update_data(to_wh=int(val))
    await state.set_state(AddSG.article)
    await cb.message.edit_text("Артикул WB (nmId или supplierArticle):")
    await cb.answer()


@router.message(AddSG.article)
async def add_article(msg: Message, state: FSMContext) -> None:
    article = msg.text.strip() if msg.text else ""
    if not article:
        await msg.answer("Пустой артикул.")
        return
    await state.update_data(article=article)
    await state.set_state(AddSG.quantity)
    await msg.answer("Сколько единиц перераспределить?")


@router.message(AddSG.quantity)
async def add_qty(msg: Message, state: FSMContext) -> None:
    text = msg.text.strip() if msg.text else ""
    if not text.isdigit() or int(text) <= 0:
        await msg.answer("Нужно положительное число.")
        return
    data = await state.get_data()
    storage = get_storage()
    rid = await storage.add_request(
        from_id=data["from_wh"],
        to_id=data["to_wh"],
        article=data["article"],
        quantity=int(text),
    )
    await state.clear()
    await msg.answer(f"Заявка #{rid} добавлена в очередь.")


# ---------- /queue, /cancel ----------------------------------------------

@router.message(Command("queue"))
async def cmd_queue(msg: Message) -> None:
    if not _is_owner(msg.from_user.id):
        return
    storage = get_storage()
    items = await storage.list_requests()
    if not items:
        await msg.answer("Очередь пуста.")
        return
    warehouses = {w.id: w.name for w in await storage.list_warehouses()}
    lines = []
    for r in items:
        f_name = warehouses.get(r.from_warehouse_id, str(r.from_warehouse_id))
        t_name = warehouses.get(r.to_warehouse_id, str(r.to_warehouse_id))
        line = (
            f"#{r.id} [{r.status}] {r.article} ×{r.quantity}: {f_name} → {t_name} "
            f"(попыток: {r.attempts})"
        )
        if r.last_error:
            line += f"\n   └ {r.last_error}"
        lines.append(line)
    await msg.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message) -> None:
    if not _is_owner(msg.from_user.id):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Использование: /cancel <id>")
        return
    storage = get_storage()
    ok = await storage.cancel_request(int(parts[1]))
    await msg.answer("Отменено." if ok else "Не нашёл pending-заявку с таким id.")


# ---------- /run, /status -------------------------------------------------

@router.message(Command("run"))
async def cmd_run(msg: Message, bot: Bot) -> None:
    if not _is_owner(msg.from_user.id):
        return
    await msg.answer("Запускаю штурм…")
    await run_attack_now(bot)


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    if not _is_owner(msg.from_user.id):
        return
    storage = get_storage()
    has_session = (await storage.load_session()) is not None
    pending = len(await storage.list_requests(status="pending"))
    done = len(await storage.list_requests(status="done"))
    failed = len(await storage.list_requests(status="failed"))
    await msg.answer(
        f"Сессия: {'есть' if has_session else 'нет (/login)'}\n"
        f"Pending: {pending}\nDone: {done}\nFailed: {failed}"
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp
