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
MAX_SPORTS = int(os.getenv("MAX_SPORTS", "6"))             # лимит турниров за скан

MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.5"))         # мин. прибыль вилки, %
MIN_EDGE = float(os.getenv("MIN_EDGE", "4"))               # мин. перевес value-ставки, %
MAX_EDGE = float(os.getenv("MAX_EDGE", "25"))              # выше - скорее ошибка в линии
MIN_BOOKS = int(os.getenv("MIN_BOOKS", "4"))               # мин. число букмекеров
SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "240"))
TOP_N = int(os.getenv("TOP_N", "5"))

EXTRA_EVENTS = int(os.getenv("EXTRA_EVENTS", "3"))         # для скольких матчей искать прогноз и допы
MIN_EXTRA_BOOKS = int(os.getenv("MIN_EXTRA_BOOKS", "2"))   # мин. букмекеров на линию допа
MIN_EXTRA_EDGE = float(os.getenv("MIN_EXTRA_EDGE", "2"))   # мин. перевес для допа, %

BASE = "https://api.the-odds-api.com/v4"

subscribers: set[int] = set()
sent_keys: set[str] = set()


def esc(x) -> str:
    return html.escape(str(x))


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
    out = []
    for ev in events:
        t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if now <= t <= end:
            out.append(ev)
    return out


# ---------- дополнительные рынки (фора, тотал, угловые) ----------

async def fetch_market(s: aiohttp.ClientSession, ev: dict, market: str) -> list:
    try:
        data = await get_json(s, f"{BASE}/sports/{ev['sport_key']}/events/{ev['id']}/odds", {
            "apiKey": ODDS_API_KEY,
            "regions": REGIONS,
            "markets": market,
            "oddsFormat": "decimal",
        })
    except Exception:
        return []
    out = []
    for bk in data.get("bookmakers", []):
        for m in bk.get("markets", []):
            if m["key"] == market:
                out.append((bk["title"], m["outcomes"]))
    return out


async def fetch_extras(ev: dict) -> dict[str, list]:
    markets = ["spreads", "totals"]
    if ev["sport_key"].startswith("soccer"):
        markets.append("alternate_totals_corners")
    async with aiohttp.ClientSession() as s:
        res = await asyncio.gather(*(fetch_market(s, ev, m) for m in markets))
    return dict(zip(markets, res))


def collect_lines(market: str, entries: list, ev: dict) -> dict:
    """{линия: [(букмекер, {исход: коэффициент})]}"""
    lines: dict = {}
    for bk, outs in entries:
        if market == "spreads":
            home = next((o for o in outs if o["name"] == ev["home_team"]), None)
            away = next((o for o in outs if o["name"] == ev["away_team"]), None)
            if not home or not away or "point" not in home or "point" not in away:
                continue
            if abs(home["point"] + away["point"]) > 1e-9:
                continue
            lines.setdefault(home["point"], []).append(
                (bk, {home["name"]: home["price"], away["name"]: away["price"]}))
        else:
            for ov in [o for o in outs if o["name"] == "Over" and "point" in o]:
                un = next((o for o in outs if o["name"] == "Under" and o.get("point") == ov["point"]), None)
                if un:
                    lines.setdefault(ov["point"], []).append((bk, {"Over": ov["price"], "Under": un["price"]}))
    return lines


def best_pick(lines: dict):
    """-> (лучший вариант с перевесом или None, (линия, вероятности) самой сбалансированной линии или None)"""
    cands = []
    main = None
    for point, rows in lines.items():
        sides = list(rows[0][1])
        rows = [r for r in rows if set(r[1]) == set(sides)]
        if len(rows) < MIN_EXTRA_BOOKS:
            continue
        fair = {s: 0.0 for s in sides}
        for _, pr in rows:
            inv = {s: 1 / pr[s] for s in sides}
            t = sum(inv.values())
            for s in sides:
                fair[s] += inv[s] / t / len(rows)
        balance = abs(fair[sides[0]] - 0.5)
        if main is None or balance < main[0]:
            main = (balance, point, fair)
        for s in sides:
            price, bk = max(((pr[s], b) for b, pr in rows), key=lambda x: x[0])
            edge = (fair[s] * price - 1) * 100
            cands.append({"point": point, "side": s, "odds": price, "book": bk,
                          "prob": fair[s] * 100, "edge": edge})
    good = [c for c in cands if MIN_EXTRA_EDGE <= c["edge"] <= MAX_EDGE and c["prob"] >= 40]
    best = max(good, key=lambda c: c["edge"]) if good else None
    return best, ((main[1], main[2]) if main else None)


def pick_name(market: str, c: dict, ev: dict) -> str:
    p, s = c["point"], c["side"]
    if market == "spreads":
        sp = p if s == ev["home_team"] else -p
        return f"{esc(s)} ({sp:+g})"
    return f"{'больше' if s == 'Over' else 'меньше'} {p:g}"


def describe(market: str, label: str, ev: dict, entries: list) -> str:
    best, main = best_pick(collect_lines(market, entries, ev))
    if best:
        return (f"• {label}: <b>{pick_name(market, best, ev)}</b> — {best['odds']} в {esc(best['book'])}, "
                f"шанс {best['prob']:.0f}%, выгода +{best['edge']:.1f}%")
    if main:
        point, fair = main
        side = max(fair, key=fair.get)
        name = pick_name(market, {"point": point, "side": side}, ev)
        return f"• {label}: скорее {name} ({fair[side] * 100:.0f}%) — явного перевеса в коэффициентах нет"
    return f"• {label}: нет данных у букмекеров"


def extras_text(ev: dict, extras: dict) -> str:
    parts = ["🔮 <b>Прогноз и допы</b>"]
    mk = h2h_markets(ev)
    if mk:
        fair = fair_h2h(mk)
        ranked = sorted(fair.items(), key=lambda x: -x[1])
        top, p = ranked[0]
        head = "ничья" if top == "Draw" else f"победа «{esc(top)}»"
        rest = " · ".join(f"{esc(ru(n))} {q * 100:.0f}%" for n, q in ranked[1:])
        parts.append(f"Наиболее вероятно: <b>{head}</b> ({p * 100:.0f}%)\nОстальные исходы: {rest}")
    for market, label in (("spreads", "Фора"), ("totals", "Тотал"), ("alternate_totals_corners", "Угловые")):
        if market in extras:
            parts.append(describe(market, label, ev, extras[market]))
    parts.append("<i>Прогноз построен по рынку (среднее мнение букмекеров), а не по статистике команд.</i>")
    return "\n".join(parts)


# ---------- основной рынок ----------

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


def fair_h2h(mk: list) -> dict[str, float]:
    """Справедливые вероятности: среднее по букмекерам после удаления маржи."""
    names = list(mk[0][1])
    fair = {n: 0.0 for n in names}
    for _, odds in mk:
        inv = {n: 1 / odds[n] for n in names}
        tot = sum(inv.values())
        for n in names:
            fair[n] += inv[n] / tot / len(mk)
    return fair


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

        margin = sum(1 / best[n][0] for n in names)
        if margin < 1:
            profit = (1 / margin - 1) * 100
            if profit >= MIN_PROFIT:
                arbs.append({
                    "ev": ev, "profit": profit,
                    "legs": [(n, best[n][0], best[n][1], (1 / best[n][0]) / margin * 100) for n in names],
                })

        fair = fair_h2h(mk)
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
    return f"{esc(ev['home_team'])} — {esc(ev['away_team'])}"


def fmt_arb(a: dict) -> str:
    ev = a["ev"]
    lines = [
        f"🔒 <b>Вилка: гарантированно +{a['profit']:.2f}%</b>",
        title(ev),
        f"<i>{esc(ev['sport_title'])} · {when(ev)}</i>",
        "👉 <b>Ставьте на все исходы:</b>",
    ]
    for n, price, bk, share in a["legs"]:
        lines.append(f"• {esc(ru(n))} — {price} в {esc(bk)} ({share:.1f}% суммы)")
    lines.append("Ставки нужно сделать быстро, пока коэффициенты не изменились.")
    return "\n".join(lines)


def fmt_value(v: dict) -> str:
    ev = v["ev"]
    level = "🔥 сильная" if v["edge"] >= 8 else "✅ хорошая" if v["edge"] >= 5 else "🟡 умеренная"
    what = "ничью" if v["pick"] == "Draw" else f"победу «{esc(v['pick'])}»"
    return (
        f"📈 <b>Рекомендация ({level})</b>\n"
        f"{title(ev)}\n"
        f"<i>{esc(ev['sport_title'])} · {when(ev)}</i>\n"
        f"👉 <b>Ставьте на {what}</b>\n"
        f"Коэффициент {v['odds']} в {esc(v['book'])}\n"
        f"Шанс по оценке рынка: {v['prob']:.1f}% (по {v['books']} букмекерам)\n"
        f"Ожидаемая выгода: +{v['edge']:.1f}% · сумма ставки: ~{v['stake']:.1f}% банка"
    )


async def build_report(hours: float, only_new: bool = False) -> list[str]:
    events = await fetch_events(hours)
    arbs, values = analyze(events)

    items: list[tuple[dict, str]] = []
    for a in arbs[:TOP_N]:
        key = f"arb:{a['ev']['id']}:{round(a['profit'], 1)}"
        if only_new and key in sent_keys:
            continue
        sent_keys.add(key)
        items.append((a["ev"], fmt_arb(a)))
    for v in values[:TOP_N]:
        key = f"val:{v['ev']['id']}:{v['pick']}:{round(v['edge'])}"
        if only_new and key in sent_keys:
            continue
        sent_keys.add(key)
        items.append((v["ev"], fmt_value(v)))

    # прогноз и допы - для первых EXTRA_EVENTS уникальных матчей
    uniq: list[dict] = []
    for ev, _ in items:
        if all(ev["id"] != u["id"] for u in uniq):
            uniq.append(ev)
    uniq = uniq[:EXTRA_EVENTS]
    extras = dict(zip([e["id"] for e in uniq],
                      await asyncio.gather(*(fetch_extras(e) for e in uniq))))

    out, shown = [], set()
    for ev, text in items:
        if ev["id"] in extras and ev["id"] not in shown:
            shown.add(ev["id"])
            text += "\n\n" + extras_text(ev, extras[ev["id"]])
        out.append(text)
    if not only_new:
        out.insert(0, f"Проверено событий: {len(events)} (начало в ближайшие {hours:g} ч)")
    return out


dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я ищу ставки с минимальным риском среди событий, которые скоро начнутся:\n"
        "🔒 вилки — прибыль при любом исходе\n"
        "📈 value-ставки — коэффициент выше справедливого\n"
        "🔮 по лучшим матчам даю прогноз и допы: фора, тотал, угловые\n\n"
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
        await m.answer(f"Ошибка запроса: {esc(e)}")
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
