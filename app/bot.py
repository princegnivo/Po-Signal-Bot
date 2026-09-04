from __future__ import annotations

import asyncio
from typing import Dict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.logger import log
from app.market_data import PocketOptionWSClient
from app.models import Candle
from app.pairs_display import display_pair
from app.strategies.strategy_1m import analyze_1m
from app.strategies.strategy_2m import analyze_2m
from app.strategies.strategy_5m import analyze_5m

router = Router()

STRATEGY_MAP = {1: analyze_1m, 2: analyze_2m, 5: analyze_5m}
ALL_TIMEFRAMES = [1, 2, 5]

# état de scan par chat (activé/désactivé, timeframes suivis)
scan_state: Dict[int, Dict] = {}


def main_menu_kb(chat_id: int) -> InlineKeyboardMarkup:
    state = scan_state.get(chat_id, {"active": False, "timeframes": set(ALL_TIMEFRAMES)})
    active = state.get("active", False)
    tfs = state.get("timeframes", set(ALL_TIMEFRAMES))
    toggle_label = "⏸ Arrêter le scan" if active else "▶️ Démarrer le scan"

    def tf_label(tf: int) -> str:
        mark = "✅" if tf in tfs else "⬜️"
        return f"{mark} {tf} min"

    buttons = [
        [InlineKeyboardButton(text=toggle_label, callback_data="toggle_scan")],
        [
            InlineKeyboardButton(text=tf_label(1), callback_data="tf_1"),
            InlineKeyboardButton(text=tf_label(2), callback_data="tf_2"),
            InlineKeyboardButton(text=tf_label(5), callback_data="tf_5"),
        ],
        [InlineKeyboardButton(text="📊 Statut", callback_data="status")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _is_allowed(chat_id: int) -> bool:
    return not settings.allowed_chat_ids or chat_id in settings.allowed_chat_ids


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message.chat.id):
        await message.answer("⛔ Accès non autorisé.")
        return

    scan_state.setdefault(message.chat.id, {"active": False, "timeframes": set(ALL_TIMEFRAMES)})
    await message.answer(
        "🤖 *Bot de signaux Pocket Option*\n\n"
        "Stratégies disponibles:\n"
        "• *M1* — Heikin Ashi + Bollinger(20,2) + SMA(2)/SMA(5) + RSI(8)\n"
        "• *M2* — Heikin Ashi + Bollinger(6,1.3) + MACD(6,19,6)\n"
        "• *M5* — Heikin Ashi + Bollinger(20,2) + SMA(5)/SMA(10) + RSI(14)\n\n"
        "Coche/décoche les timeframes ci-dessous, puis démarre le scan.\n"
        "⚠️ Ce bot n'exécute *aucun* trade automatiquement : il envoie "
        "uniquement des signaux informatifs.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(message.chat.id),
    )


@router.callback_query(F.data == "toggle_scan")
async def cb_toggle_scan(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id
    state = scan_state.setdefault(chat_id, {"active": False, "timeframes": set(ALL_TIMEFRAMES)})
    state["active"] = not state["active"]

    if state["active"]:
        tfs = ", ".join(f"{t}min" for t in sorted(state["timeframes"])) or "aucun"
        await callback.message.answer(f"🔍 Analyse en cours…⏳ (timeframes: {tfs})")
    else:
        await callback.message.answer("⏸ Scan mis en pause.")

    await callback.message.edit_reply_markup(reply_markup=main_menu_kb(chat_id))
    await callback.answer()


@router.callback_query(F.data.startswith("tf_"))
async def cb_toggle_timeframe(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id
    tf = int(callback.data.split("_")[1])
    state = scan_state.setdefault(chat_id, {"active": False, "timeframes": set(ALL_TIMEFRAMES)})

    if tf in state["timeframes"]:
        state["timeframes"].discard(tf)
        await callback.answer(f"Timeframe {tf}min désactivé")
    else:
        state["timeframes"].add(tf)
        await callback.answer(f"Timeframe {tf}min activé")

    await callback.message.edit_reply_markup(reply_markup=main_menu_kb(chat_id))


@router.callback_query(F.data == "status")
async def cb_status(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id
    state = scan_state.get(chat_id, {"active": False, "timeframes": set(ALL_TIMEFRAMES)})
    active = "✅ actif" if state.get("active") else "⏸ en pause"
    tfs = ", ".join(f"{t}min" for t in sorted(state.get("timeframes", []))) or "aucun"
    pairs_str = ", ".join(display_pair(p) for p in settings.pairs)
    await callback.message.answer(
        f"📊 Statut du scan: {active}\n⏱ Timeframes suivis: {tfs}\n💱 Paires: {pairs_str}"
    )
    await callback.answer()


async def scanning_loop(bot: Bot, provider: PocketOptionWSClient) -> None:
    """Boucle périodique : recalcule les indicateurs et envoie les signaux
    aux chats ayant le scan actif."""
    while True:
        await asyncio.sleep(settings.scan_interval_seconds)

        for chat_id, state in list(scan_state.items()):
            if not state.get("active"):
                continue
            if not _is_allowed(chat_id):
                continue

            for pair in settings.pairs:
                payout = provider.get_payout(pair)
                if payout is not None and payout < settings.min_payout:
                    continue

                for tf in state.get("timeframes", ALL_TIMEFRAMES):
                    candles = provider.get_candles(pair, tf)
                    if len(candles) < 25:
                        continue
                    strategy_fn = STRATEGY_MAP.get(tf)
                    if strategy_fn is None:
                        continue
                    signal = strategy_fn(pair, candles, payout=payout)
                    if signal:
                        try:
                            await bot.send_message(chat_id, signal.to_message())
                        except Exception as exc:  # noqa: BLE001
                            log.error(f"Échec envoi signal à {chat_id}: {exc}")


async def start_bot() -> None:
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    provider = PocketOptionWSClient()

    def _on_candle(pair: str, timeframe: int, candle: Candle) -> None:
        log.debug(f"Nouvelle bougie {pair} {timeframe}min: close={candle.close}")

    market_task = asyncio.create_task(provider.run(_on_candle))
    scan_task = asyncio.create_task(scanning_loop(bot, provider))

    try:
        await dp.start_polling(bot)
    finally:
        market_task.cancel()
        scan_task.cancel()
        await bot.session.close()
