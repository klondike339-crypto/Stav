import asyncio
import html
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

BOT_TOKEN = os.environ["BOT_TOKEN"]
ODDS_API_KEY = os.environ["ODDS_API_KEY"]
REGIONS = os.getenv("REGIONS", "eu")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))

WINDOW_HOURS = float(os.getenv("WINDOW_HOURS", "12"))      # окно поиска по умолчанию, ч
SPORT_GROUPS = [g.strip() for g in os.getenv("SPORT_GROUPS", "Soccer,Basketball,Ice Hockey").split(",")]
MAX_SPORTS = int(os.getenv("MAX_SPORTS", "6"))             # лимит турниров за скан (экономит квоту API)

MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.5"))         # мин. прибыль вилки, %
MIN_EDGE = float(os.getenv("MIN_EDGE", "4"))               # мин. перевес value-ставки, %
MAX_EDGE = float(os.getenv("MAX_EDGE", "25"))              # выше - скорее ошибка в линии
MIN_BOOKS = int(os.getenv("MIN_BOOKS", "4"))               # мин. число букмекеров
SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "240"))
TOP_N = int(os.getenv("TOP_N", "5"))

BASE = "https://api.the-odds-api.com/v4"

subscribers: set[int] = set()
sent_keys: set[str] = set()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def get_json(s: aiohttp.ClientSession, url: str, params: dict):
    async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_events(hours: float) -> list[dict]:
    """События, которые начнутся в ближайшие `hours` часов от момента запроса."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    events: list[dict] = []
    async with aiohttp.ClientSession() as s:
        sports = await get_json(s, f"{BASE}/sports", {"apiKey": ODDS_API_KEY})
        keys = [
            x["key"] for x in sports
            if x.get("active") and not x.get("has_outrights") and x.get("group") in SPORT_GROUPS
        ][:MAX_SPORTS]
        for k in keys:
            try:
                data = await get_json(s, f"{BASE}/sports/{k}/odds", {
                    "apiKey": ODDS_API_KEY,
                    "regions": REGIONS,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "commenceTimeFrom": iso(now),
                    "commenceTimeTo": iso(end),
                })
            except aiohttp.ClientResponseError:
                continue
            events.extend(data)
    # подстраховка: оставляем только ещё не начавшиеся события внутри окна
    out = []
    for ev in events:
        t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if now <= t <= end:
            out.append(ev)
    return out


def h2h_markets(ev: dict) -> list[tuple[str, dict[str, float]]]:
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


def ru(name: str) -> str:
    return "Ничья" if name == "Draw" else name


def when(ev: dict) -> str:
    t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
    delta = t - datetime.now(timezone.utc)
    mins = max(int(delta.total_seconds() // 60), 0)
    h, m = divmod(mins, 60)
    left = f"через {h} ч {m} мин" if h else f"через {m} мин"
    return f"{t.astimezone(TZ).strftime('%d.%m %H:%M')} ({left})"


def analyze(events: list[dict]) -> tuple[list[dict], list[dict]]:
    arbs, values = [], []
    for ev in events:
        mk = h2h_markets(ev)
        if len(mk) < MIN_BOOKS:
            continue
        names = list(mk[0][1])
        best = {n: max(((odds[n], bk) for bk, odds in mk), key=lambda x: x[0]) for n in names}

        # вилка
        margin = sum(1 / best[n][0] for n in names)
        if margin < 1:
            profit = (1 / margin - 1) * 100
            if profit >= MIN_PROFIT:
                arbs.append({
                    "ev": ev, "profit": profit,
                    "legs": [(n, best[n][0], best[n][1], (1 / best[n][0]) / margin * 100) for n in names],
                })

        # value: справедливая вероятность = среднее по букмекерам без маржи
        fair = {n: 0.0 for n in names}
        for _, odds in mk:
            inv = {n: 1 / odds[n] for n in names}
            tot = sum(inv.values())
            for n in names:
                fair[n] += inv[n] / tot / len(mk)
        for n in names:
            price = best[n][0]
            edge = (fair[n] * price - 1) * 100
            if MIN_EDGE <= edge <= MAX_EDGE:
                kelly = (fair[n] * price - 1) / (price - 1)
                stake = min(0.25 * kelly, 0.03) * 100  # четверть Келли, не больше 3% банка
                values.append({
                    "ev": ev, "pick": n, "odds": price, "book": best[n][1],
                    "edge": edge, "prob": fair[n] * 100, "books": len(mk), "stake": stake,
                })

    arbs.sort(key=lambda x: x["profit"], reverse=True)
    values.sort(key=lambda x: x["edge"], reverse=True)
    return arbs, values


def title(ev: dict) -> str:
    return f"{html.escape(ev['home_team'])} — {html.escape(ev['away_team'])}"


def fmt_arb(a: dict) -> str:
    ev = a["ev"]
    lines = [
        f"🔒 <b>Вилка: гарантированно +{a['profit']:.2f}%</b>",
        title(ev),
        f"<i>{html.escape(ev['sport_title'])} · {when(ev)}</i>",
        "👉 <b>Ставьте на все исходы:</b>",
    ]
    for n, price, bk, share in a["legs"]:
        lines.append(f"• {html.escape(ru(n))} — {price} в {html.escape(bk)} ({share:.1f}% суммы)")
    lines.append("Ставки нужно сделать быстро, пока коэффициенты не изменились.")
    return "\n".join(lines)


def fmt_value(v: dict) -> str:
    ev = v["ev"]
    level = "🔥 сильная" if v["edge"] >= 8 else "✅ хорошая" if v["edge"] >= 5 else "🟡 умеренная"
    pick = html.escape(ru(v["pick"]))
    what = "ничью" if v["pick"] == "Draw" else f"победу «{pick}»"
    return (
        f"📈 <b>Рекомендация ({level})</b>\n"
        f"{title(ev)}\n"
        f"<i>{html.escape(ev['sport_title'])} · {when(ev)}</i>\n"
        f"👉 <b>Ставьте на {what}</b>\n"
        f"Коэффициент {v['odds']} в {html.escape(v['book'])}\n"
        f"Шанс по оценке рынка: {v['prob']:.1f}% (по {v['books']} букмекерам)\n"
        f"Ожидаемая выгода: +{v['edge']:.1f}% · сумма ставки: ~{v['stake']:.1f}% банка"
    )


async def build_report(hours: float, only_new: bool = False) -> list[str]:
    events = await fetch_events(hours)
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
    if not only_new:
        out.insert(0, f"Проверено событий: {len(events)} (начало в ближайшие {hours:g} ч)")
    return out


dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я ищу ставки с минимальным риском среди событий, "
        "которые скоро начнутся:\n"
        "🔒 вилки — прибыль при любом исходе\n"
        "📈 value-ставки — коэффициент выше справедливого\n\n"
        f"/scan — события в ближайшие {WINDOW_HOURS:g} ч\n"
        "/scan 3 — события в ближайшие 3 часа (любое число часов)\n"
        "/auto — вкл/выкл автоуведомления\n\n"
        "⚠️ Гарантий выигрыша нет: линии меняются быстро, а букмекеры режут лимиты."
    )


@dp.message(Command("scan"))
async def scan(m: Message, command: CommandObject):
    hours = WINDOW_HOURS
    if command.args:
        try:
            hours = min(max(float(command.args.replace(",", ".").split()[0]), 0.5), 72)
        except ValueError:
            await m.answer("Укажите число часов, например: /scan 3")
            return
    await m.answer("Анализирую события…")
    try:
        report = await build_report(hours)
    except Exception as e:
        await m.answer(f"Ошибка запроса: {html.escape(str(e))}")
        return
    if len(report) == 1:
        report.append("Подходящих ставок в этом окне нет. Попробуйте /scan 24")
    for text in report:
        await m.answer(text)


@dp.message(Command("auto"))
async def auto(m: Message):
    if m.chat.id in subscribers:
        subscribers.discard(m.chat.id)
        await m.answer("Автоуведомления выключены.")
    else:
        subscribers.add(m.chat.id)
        await m.answer(f"Автоуведомления включены: проверка каждые {SCAN_MINUTES} мин, окно {WINDOW_HOURS:g} ч.")


async def auto_loop(bot: Bot):
    while True:
        await asyncio.sleep(SCAN_MINUTES * 60)
        if not subscribers:
            continue
        try:
            report = await build_report(WINDOW_HOURS, only_new=True)
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
