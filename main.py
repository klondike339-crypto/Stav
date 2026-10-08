import asyncio
import html
import os
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Импорт библиотеки HLTV
try:
    from hltv_async_api import Hltv
    HLTV_AVAILABLE = True
except ImportError:
    HLTV_AVAILABLE = False

BOT_TOKEN = os.environ["BOT_TOKEN"]
ODDS_API_KEY = os.environ["ODDS_API_KEY"]
REGIONS = os.getenv("REGIONS", "eu")
MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.5"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "4"))
MAX_EDGE = float(os.getenv("MAX_EDGE", "25"))
MIN_BOOKS = int(os.getenv("MIN_BOOKS", "2"))
SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "60"))
TOP_N = int(os.getenv("TOP_N", "5"))

API_BASE = "https://api.the-odds-api.com/v4/sports"

ESPORTS_SPORTS = {
    "cs2": {"key": "esports_csgo", "title": "🔫 CS2 (Counter-Strike)"},
    "dota2": {"key": "esports_dota2", "title": "⚔️ Dota 2"},
}

subscribers: set[int] = set()
sent_keys: set[str] = set()


# ---------------------------------------------------------
# Глубокий анализ HLTV.org (Карты и Стороны)
# ---------------------------------------------------------
async def get_hltv_deep_stats(team_name: str) -> dict:
    """Загружает углубленную статистику: винрейт карт и сторон (CT/T)."""
    if not HLTV_AVAILABLE:
        return {}

    try:
        async with Hltv() as hltv:
            teams = await hltv.get_teams(team_name)
            if not teams:
                return {}

            best_match = teams[0]
            team_id = best_match.get("id")
            if not team_id:
                return {}

            # Базовая информация и статистика
            info = await hltv.get_team_info(team_id)
            stats = await hltv.get_team_stats(team_id) or {}

            # Винрейты по картам (map pool)
            maps_data = stats.get("maps", {})
            maps_summary = {}
            if isinstance(maps_data, dict):
                for map_name, mdata in maps_data.items():
                    if isinstance(mdata, dict) and "winrate" in mdata:
                        maps_summary[map_name] = mdata["winrate"]

            # Статистика сторон (CT / T side winrate)
            ct_winrate = stats.get("ct_winrate", "Н/Д")
            t_winrate = stats.get("t_winrate", "Н/Д")

            return {
                "name": info.get("name", team_name),
                "rank": info.get("rank", "Н/Д"),
                "maps": maps_summary,
                "ct_side": ct_winrate,
                "t_side": t_winrate,
            }
    except Exception:
        return {}


async def get_hltv_top_teams(limit: int = 10) -> list[dict]:
    if not HLTV_AVAILABLE:
        return []
    try:
        async with Hltv() as hltv:
            return await hltv.get_top_teams(max_teams=limit)
    except Exception:
        return []


# ---------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔫 CS2 + HLTV Глубокий Анализ", callback_data="esports_cs2"),
                InlineKeyboardButton(text="⚔️ Dota 2 Анализ", callback_data="esports_dota2"),
            ],
            [
                InlineKeyboardButton(text="🏆 HLTV Топ Команд", callback_data="hltv_top"),
                InlineKeyboardButton(text="🔒 Вилки & Value", callback_data="scan_all"),
            ],
            [
                InlineKeyboardButton(text="🔔 Автоуведомления", callback_data="toggle_auto"),
            ],
        ]
    )


def esports_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔫 CS2 Матчи", callback_data="esports_cs2"),
                InlineKeyboardButton(text="⚔️ Dota 2 Матчи", callback_data="esports_dota2"),
            ],
            [
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"),
            ],
        ]
    )


# ---------------------------------------------------------
# Запросы к Odds API
# ---------------------------------------------------------
async def fetch_odds(sport_key: str = "upcoming", markets: str = "h2h,spreads,totals") -> list[dict]:
    url = f"{API_BASE}/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                return []
            return await r.json()


def when(ev: dict) -> str:
    try:
        return datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).strftime("%d.%m %H:%M UTC")
    except Exception:
        return ""


def title(ev: dict) -> str:
    return f"<b>{html.escape(ev['home_team'])}</b> vs <b>{html.escape(ev['away_team'])}</b>"


# ---------------------------------------------------------
# Аналитика Киберспорта с детальной проверкой HLTV
# ---------------------------------------------------------
async def analyze_esports_match(ev: dict, fetch_hltv: bool = False) -> dict:
    home = ev["home_team"]
    away = ev["away_team"]

    h2h_odds = {home: [], away: []}
    spreads = []
    totals = []

    for bk in ev.get("bookmakers", []):
        for m in bk.get("markets", []):
            m_key = m.get("key")
            if m_key == "h2h":
                for o in m.get("outcomes", []):
                    if o["name"] in h2h_odds:
                        h2h_odds[o["name"]].append(o["price"])
            elif m_key == "spreads":
                for o in m.get("outcomes", []):
                    spreads.append((o["name"], o.get("point", 0), o["price"], bk["title"]))
            elif m_key == "totals":
                for o in m.get("outcomes", []):
                    totals.append((o["name"], o.get("point", 0), o["price"], bk["title"]))

    if not h2h_odds[home] or not h2h_odds[away]:
        return {}

    avg_home = sum(h2h_odds[home]) / len(h2h_odds[home])
    avg_away = sum(h2h_odds[away]) / len(h2h_odds[away])

    inv_home = 1 / avg_home
    inv_away = 1 / avg_away
    margin_sum = inv_home + inv_away

    prob_home = (inv_home / margin_sum) * 100
    prob_away = (inv_away / margin_sum) * 100

    max_prob = max(prob_home, prob_away)
    is_high_probability = max_prob >= 60.0  # Высокая вероятность победы (от 60%)

    if prob_home >= 70:
        predicted_score = "2 : 0"
        confidence = "🔥 Высокая уверенность (Фаворит)"
    elif prob_home >= 58:
        predicted_score = "2 : 1"
        confidence = "⚔️ Упорная борьба (Преимущество)"
    elif prob_away >= 70:
        predicted_score = "0 : 2"
        confidence = "🔥 Высокая уверенность (Фаворит)"
    elif prob_away >= 58:
        predicted_score = "1 : 2"
        confidence = "⚔️ Упорная борьба (Преимущество)"
    else:
        predicted_score = "2 : 1 / 1 : 2"
        confidence = "🎲 Равные шансы (50/50)"

    # Для событий с высокой вероятностью запрашиваем глубокую статистику HLTV
    hltv_home, hltv_away = {}, {}
    if fetch_hltv and HLTV_AVAILABLE:
        hltv_home, hltv_away = await asyncio.gather(
            get_hltv_deep_stats(home),
            get_hltv_deep_stats(away)
        )

    return {
        "event": ev,
        "home": home,
        "away": away,
        "prob_home": prob_home,
        "prob_away": prob_away,
        "max_prob": max_prob,
        "is_high_prob": is_high_probability,
        "best_home_odds": max(h2h_odds[home]),
        "best_away_odds": max(h2h_odds[away]),
        "predicted_score": predicted_score,
        "confidence": confidence,
        "spreads": spreads,
        "totals": totals,
        "hltv_home": hltv_home,
        "hltv_away": hltv_away,
    }


def fmt_esports_analysis(analysis: dict) -> str:
    if not analysis:
        return ""

    ev = analysis["event"]
    hltv_h = analysis.get("hltv_home", {})
    hltv_a = analysis.get("hltv_away", {})

    high_prob_badge = " ⭐ <b>[ВЫСОКАЯ ВЕРОЯТНОСТЬ]</b>" if analysis.get("is_high_prob") else ""

    lines = [
        f"🎮 <b>{html.escape(ev['sport_title'])}</b>{high_prob_badge}",
        f"🔥 {title(ev)}",
        f"📅 <i>{when(ev)}</i>\n",
    ]

    # Анализ рейтинга HLTV
    if hltv_h or hltv_a:
        lines.append("🌐 <b>Статистика HLTV.org:</b>")
        rank_h = f"#{hltv_h.get('rank')}" if hltv_h.get('rank') else "Н/Д"
        rank_a = f"#{hltv_a.get('rank')}" if hltv_a.get('rank') else "Н/Д"
        lines.append(f"• {html.escape(analysis['home'])}: Рейтинг <b>{rank_h}</b>")
        lines.append(f"• {html.escape(analysis['away'])}: Рейтинг <b>{rank_a}</b>\n")

    # Если это матч с высокой вероятностью — выводим глубокий разбор карт и сторон
    if analysis.get("is_high_prob") and (hltv_h.get("maps") or hltv_a.get("maps") or hltv_h.get("ct_side")):
        lines.append("🗺️ <b>Глубокий анализ HLTV (Карты & Стороны):</b>")

        for team_name, hltv_data in [(analysis['home'], hltv_h), (analysis['away'], hltv_a)]:
            if not hltv_data:
                continue
            
            lines.append(f"<b>{html.escape(team_name)}:</b>")
            
            # Стороны CT / T
            ct = hltv_data.get("ct_side", "Н/Д")
            t = hltv_data.get("t_side", "Н/Д")
            if ct != "Н/Д" or t != "Н/Д":
                lines.append(f"  • Защита (CT): <b>{ct}</b> | Атака (T): <b>{t}</b>")

            # Винрейты по картам
            maps = hltv_data.get("maps", {})
            if maps:
                top_maps = [f"{m}: {wr}" for m, wr in list(maps.items())[:3]]
                lines.append(f"  • Топ карты: <code>{', '.join(top_maps)}</code>")
            lines.append("")

    lines.extend([
        f"📊 <b>Вероятности победы:</b>",
        f"• {html.escape(analysis['home'])}: <b>{analysis['prob_home']:.1f}%</b> (Макс. кэф: {analysis['best_home_odds']})",
        f"• {html.escape(analysis['away'])}: <b>{analysis['prob_away']:.1f}%</b> (Макс. кэф: {analysis['best_away_odds']})\n",
        f"🎯 <b>Прогноз точного счёта (BO3):</b> <code>{analysis['predicted_score']}</code>",
        f"💡 <i>Анализ: {analysis['confidence']}</i>\n",
    ])

    if analysis["spreads"]:
        lines.append("🛡️ <b>Фора (Spreads):</b>")
        for team, point, price, bk in analysis["spreads"][:2]:
            sign = "+" if point > 0 else ""
            lines.append(f"• {html.escape(team)} ({sign}{point}): @<b>{price}</b> ({html.escape(bk)})")
        lines.append("")

    if analysis["totals"]:
        lines.append("📈 <b>Тотал карт (Totals):</b>")
        for name, point, price, bk in analysis["totals"][:2]:
            lines.append(f"• {html.escape(name)} {point}: @<b>{price}</b> ({html.escape(bk)})")

    return "\n".join(lines)


# ---------------------------------------------------------
# Поиск Вилок и Value
# ---------------------------------------------------------
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


def analyze(events: list[dict]) -> tuple[list[dict], list[dict]]:
    arbs, values = [], []
    for ev in events:
        mk = h2h_markets(ev)
        if len(mk) < MIN_BOOKS:
            continue
        names = list(mk[0][1])

        best = {n: max(((odds[n], bk) for bk, odds in mk), key=lambda x: x[0]) for n in names}

        margin = sum(1 / best[n][0] for n in names)
        if margin < 1:
            profit = (1 / margin - 1) * 100
            if profit >= MIN_PROFIT:
                arbs.append({
                    "ev": ev, "profit": profit,
                    "legs": [(n, best[n][0], best[n][1], (1 / best[n][0]) / margin * 100) for n in names],
                })

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
    events = await fetch_odds("upcoming")
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


# ---------------------------------------------------------
# Обработчики Aiogram
# ---------------------------------------------------------
dp = Dispatcher()


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_start(m: Message):
    text = (
        "👋 <b>Киберспортивная аналитическая система с HLTV</b>\n\n"
        "⚡ <b>Особенности:</b>\n"
        "• 🔫 <b>CS2:</b> Автоматический глубокий разбор карт и сторон (CT/T) для матчей с максимальной вероятностью выигрыша.\n"
        "• ⚔️ <b>Dota 2:</b> Оценка вероятностей победы, точные счета и форы.\n"
        "• 🏆 <b>HLTV Top:</b> Мировой рейтинг профессиональных команд.\n"
        "• 🔒 <b>Сканер:</b> Поиск вилок и валуйных ставок.\n\n"
        "Выберите интересующий раздел:"
    )
    await m.answer(text, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    await call.message.edit_text("Главное меню бота:", reply_markup=main_menu_keyboard())
    await call.answer()


@dp.callback_query(F.data.in_({"esports_cs2", "esports_dota2"}))
async def cb_esports(call: CallbackQuery):
    is_cs2 = call.data == "esports_cs2"
    sport_type = "cs2" if is_cs2 else "dota2"
    sport_info = ESPORTS_SPORTS[sport_type]

    await call.message.answer(f"⏳ Сканирую линии BК и провожу детальныйHLTV-анализ по {sport_info['title']}…")
    await call.answer()

    events = await fetch_odds(sport_info["key"])
    if not events:
        await call.message.answer(f"Сейчас нет активных матчей по {sport_info['title']}.")
        return

    # Сортируем события так, чтобы матчи с наибольшей вероятностью выводились первыми
    analyzed_events = []
    for ev in events[:TOP_N]:
        analysis = await analyze_esports_match(ev, fetch_hltv=is_cs2)
        if analysis:
            analyzed_events.append(analysis)

    analyzed_events.sort(key=lambda x: x.get("max_prob", 0), reverse=True)

    for analysis in analyzed_events:
        formatted_text = fmt_esports_analysis(analysis)
        if formatted_text:
            await call.message.answer(formatted_text, reply_markup=esports_menu_keyboard())


@dp.callback_query(F.data == "hltv_top")
async def cb_hltv_top(call: CallbackQuery):
    await call.message.answer("🌐 Загружаю актуальный рейтинг HLTV.org…")
    await call.answer()

    top_teams = await get_hltv_top_teams(limit=15)
    if not top_teams:
        await call.message.answer("Не удалось получить рейтинг с HLTV.org.")
        return

    lines = ["🏆 <b>Мировой рейтинг команд (HLTV.org):</b>\n"]
    for idx, team in enumerate(top_teams, 1):
        name = html.escape(team.get("title", team.get("name", "Team")))
        points = team.get("points", "")
        pts_str = f" ({points})" if points else ""
        lines.append(f"<b>{idx}.</b> {name}{pts_str}")

    await call.message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "scan_all")
async def cb_scan_all(call: CallbackQuery):
    await call.message.answer("🔍 Поиск арбитражных ситуаций...")
    await call.answer()
    report = await build_report()
    if not report:
        await call.message.answer("На данный момент вилок и value-ставок не найдено.")
        return
    for text in report:
        await call.message.answer(text)


@dp.callback_query(F.data == "toggle_auto")
async def cb_toggle_auto(call: CallbackQuery):
    if call.message.chat.id in subscribers:
        subscribers.discard(call.message.chat.id)
        await call.answer("Автоуведомления выключены.", show_alert=True)
    else:
        subscribers.add(call.message.chat.id)
        await call.answer(f"Автоуведомления включены (интервал: {SCAN_MINUTES} мин).", show_alert=True)


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
