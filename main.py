import asyncio
import html
import os
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

BOT_TOKEN = os.environ["BOT_TOKEN"]
ODDS_API_KEY = os.environ["ODDS_API_KEY"]
REGIONS = os.getenv("REGIONS", "eu")
MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.5"))   # мин. прибыль вилки, %
MIN_EDGE = float(os.getenv("MIN_EDGE", "4"))         # мин. перевес value-ставки, %
MAX_EDGE = float(os.getenv("MAX_EDGE", "25"))        # выше - скорее ошибка в линии
MIN_BOOKS = int(os.getenv("MIN_BOOKS", "4"))         # мин. число букмекеров
SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "60"))
TOP_N = int(os.getenv("TOP_N", "5"))

API_URL = "https://api.the-odds-api.com/v4/sports/upcoming/odds"

subscribers: set[int] = set()
sent_keys: set[str] = set()


async def fetch_events() -> list[dict]:
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(API_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            return await r.json()


def h2h_markets(ev: dict) -> list[tuple[str, dict[str, float]]]:
    """[(букмекер, {исход: коэффициент})] только полные рынки с одинаковым набором исходов."""
    res = []
    for bk in ev.get("bookmakers", []):
        for m in bk.get("markets", []):
            if m["key"] == "h2h":
                odds = {o["name"]: o["price"] for o in m["outcomes"]}
                if len(odds) >= 2:
                    res.append((bk["title"], odds))
    if not res:
        return []
    names = set(res[0][1])
    return [x for x in res if set(x[1]) == names]


def when(ev: dict) -> str:
    try:
        return datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).strftime("%d.%m %H:%M UTC")
    except Exception:
        return ""


def analyze(events: list[dict]) -> tuple[list[dict], list[dict]]:
    arbs, values = [], []
    for ev in events:
        mk = h2h_markets(ev)
        if len(mk) < MIN_BOOKS:
            continue
        names = list(mk[0][1])

        # лучший коэффициент по каждому исходу
        best = {n: max(((odds[n], bk) for bk, odds in mk), key=lambda x: x[0]) for n in names}

        # 1) вилка: сумма обратных коэффициентов < 1
        margin = sum(1 / best[n][0] for n in names)
        if margin < 1:
            profit = (1 / margin - 1) * 100
            if profit >= MIN_PROFIT:
                arbs.append({
                    "ev": ev, "profit": profit,
                    "legs": [(n, best[n][0], best[n][1], (1 / best[n][0]) / margin * 100) for n in names],
                })

        # 2) value: справедливая вероятность = среднее по букмекерам без маржи
        fair = {n: 0.0 for n in names}
        for _, odds in mk:
            inv = {n: 1 / odds[n] for n in names}
            tot = sum(inv.values())
            for n in names:
                fair[n] += inv[n] / tot / len(mk)
        for n in names:
            edge = (fair[n] * best[n][0] - 1) * 100
            if MIN_EDGE <= edge <= MAX_EDGE:
                values.append({
                    "ev": ev, "pick": n, "odds": best[n][0], "book": best[n][1],
                    "edge": edge, "prob": fair[n] * 100, "books": len(mk),
                })

    arbs.sort(key=lambda x: x["profit"], reverse=True)
    values.sort(key=lambda x: x["edge"], reverse=True)
    return arbs, values


def title(ev: dict) -> str:
    return f"{html.escape(ev['home_team'])} — {html.escape(ev['away_team'])}"


def fmt_arb(a: dict) -> str:
    ev = a["ev"]
    lines = [f"🔒 <b>Вилка +{a['profit']:.2f}%</b>", f"{title(ev)}", f"<i>{html.escape(ev['sport_title'])}, {when(ev)}</i>"]
    for n, price, bk, share in a["legs"]:
        lines.append(f"• {html.escape(n)} @ {price} ({html.escape(bk)}) — {share:.1f}% банка")
    return "\n".join(lines)


def fmt_value(v: dict) -> str:
    ev = v["ev"]
    return (
        f"📈 <b>Value +{v['edge']:.1f}%</b>\n{title(ev)}\n"
        f"<i>{html.escape(ev['sport_title'])}, {when(ev)}</i>\n"
        f"• {html.escape(v['pick'])} @ {v['odds']} ({html.escape(v['book'])})\n"
        f"Оценка вероятности: {v['prob']:.1f}% (по {v['books']} букмекерам)"
    )


async def build_report(only_new: bool = False) -> list[str]:
    events = await fetch_events()
    arbs, values = analyze(events)
    out = []
    for a in arbs[:TOP_N]:
        key = f"arb:{a['ev']['id']}:{round(a['profit'], 1)}"
        if only_new and key in sent_keys:
            continue
        sent_keys.add(key)
        out.append(fmt_arb(a))
    for v in values[:TOP_N]:
        key = f"val:{v['ev']['id']}:{v['pick']}:{round(v['edge'])}"
        if only_new and key in sent_keys:
            continue
        sent_keys.add(key)
        out.append(fmt_value(v))
    return out


dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я ищу ставки с минимальным риском:\n"
        "🔒 вилки (арбитраж) — прибыль при любом исходе\n"
        "📈 value-ставки — коэффициент выше справедливого\n\n"
        "/scan — проверить рынок сейчас\n"
        "/auto — вкл/выкл автоуведомления\n\n"
        "⚠️ Гарантий выигрыша нет: линии меняются быстро, а букмекеры режут лимиты."
    )


@dp.message(Command("scan"))
async def scan(m: Message):
    await m.answer("Анализирую события…")
    try:
        report = await build_report()
    except Exception as e:
        await m.answer(f"Ошибка запроса: {html.escape(str(e))}")
        return
    if not report:
        await m.answer("Сейчас подходящих ставок нет.")
        return
    for text in report:
        await m.answer(text)


@dp.message(Command("auto"))
async def auto(m: Message):
    if m.chat.id in subscribers:
        subscribers.discard(m.chat.id)
        await m.answer("Автоуведомления выключены.")
    else:
        subscribers.add(m.chat.id)
        await m.answer(f"Автоуведомления включены. Проверка каждые {SCAN_MINUTES} мин.")


async def auto_loop(bot: Bot):
    while True:
        await asyncio.sleep(SCAN_MINUTES * 60)
        if not subscribers:
            continue
        try:
            report = await build_report(only_new=True)
        except Exception:
            continue
        for chat_id in list(subscribers):
            for text in report:
                try:
                    await bot.send_message(chat_id, text)
                except Exception:
                    pass


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    asyncio.create_task(auto_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
