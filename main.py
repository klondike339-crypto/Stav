import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup
)
from analyzer import OddsAnalyzer, MOCK_PREDICTIONS

BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ИЗ_BOTFATHER")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Главное меню с категориями
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="⚽ Футбол"),
                KeyboardButton(text="🎯 CS2")
            ],
            [
                KeyboardButton(text="🎮 Dota 2"),
                KeyboardButton(text="🏒 Хоккей")
            ],
            [
                KeyboardButton(text="🔥 Все High-Confidence прогнозы")
            ]
        ],
        resize_keyboard=True
    )

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    await message.answer(
        "🤖 **BetBoom Analytics Bot**\n\n"
        "Бот автоматически проверяет котировки BetBoom, статистику HLTV, OpenDota и спортивные метрики.\n\n"
        "В выдачу попадают только матчи с рассчитанной вероятностью выигрыша **выше 72%**.\n"
        "Выберите дисциплину в меню ниже:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

async def send_sport_predictions(message: types.Message, sport_name: str):
    raw_preds = [p for p in MOCK_PREDICTIONS if p.sport.lower() == sport_name.lower()]
    filtered_preds = OddsAnalyzer.filter_high_probability_bets(raw_preds, min_prob=72.0)

    if not filtered_preds:
        await message.answer(f"На текущий момент нет прогнозов по дисциплине {sport_name} с нужным процентом вероятности.")
        return

    for pred in filtered_preds:
        text = (
            f"🏆 **{pred.sport} | Линия BetBoom**\n"
            f"⚔️ **Матч:** {pred.event}\n"
            f"📅 **Дата и время:** {pred.date_time}\n"
            f"───────────────────\n"
            f"🎯 **Прогноз:** {pred.pick}\n"
            f"📊 **Коэффициент:** `{pred.odds}`\n"
            f"📈 **Вероятность захода:** `{pred.win_probability}%`\n\n"
            f"💡 **Аналитика:** _{pred.analysis}_\n"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Сделать ставку на BetBoom", url="https://betboom.ru")
        ]])
        
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.message(F.text == "⚽ Футбол")
async def football_handler(message: types.Message):
    await send_sport_predictions(message, "Футбол")

@dp.message(F.text == "🎯 CS2")
async def cs2_handler(message: types.Message):
    await send_sport_predictions(message, "CS2")

@dp.message(F.text == "🎮 Dota 2")
async def dota2_handler(message: types.Message):
    await send_sport_predictions(message, "Dota 2")

@dp.message(F.text == "🏒 Хоккей")
async def hockey_handler(message: types.Message):
    await send_sport_predictions(message, "Хоккей")

@dp.message(F.text == "🔥 Все High-Confidence прогнозы")
async def all_predictions_handler(message: types.Message):
    filtered_preds = OddsAnalyzer.filter_high_probability_bets(MOCK_PREDICTIONS, min_prob=75.0)
    for pred in filtered_preds:
        text = (
            f"🔥 **ТОП ПРОГНОЗ ({pred.win_probability}% проходимости)**\n\n"
            f"🏆 **Дисциплина:** {pred.sport}\n"
            f"⚔️ **Матч:** {pred.event}\n"
            f"📅 **Время:** {pred.date_time}\n"
            f"🎯 **Исход:** `{pred.pick}` (Кэф: {pred.odds})\n"
        )
        await message.answer(text, parse_mode="Markdown")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
