import asyncio
import html
import os
import time
from datetime import datetime, timedelta, timezone
from math import comb
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, KeyboardButton, MenuButtonCommands,
                           Message, ReplyKeyboardMarkup)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
PANDA_TOKEN = os.getenv("PANDASCORE_TOKEN", "")
REGIONS = os.getenv("REGIONS", "eu")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))

# --- обычный спорт ---
WINDOW_HOURS = float(os.getenv("WINDOW_HOURS", "12"))
SPORT_GROUPS = [g.strip() for g in os.getenv("SPORT_GROUPS", "Soccer,Basketball,Ice Hockey").split(",")]
MAX_SPORTS = int(os.getenv("MAX_SPORTS", "6"))
MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.5"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "4"))
MAX_EDGE = float(os.getenv("MAX_EDGE", "25"))
MIN_BOOKS = int(os.getenv("MIN_BOOKS", "4"))
SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "240"))
TOP_N = int(os.getenv("TOP_N", "5"))
EXTRA_EVENTS = int(os.getenv("EXTRA_EVENTS", "3"))
MIN_EXTRA_BOOKS = int(os.getenv("MIN_EXTRA_BOOKS", "2"))
MIN_EXTRA_EDGE = float(os.getenv("MIN_EXTRA_EDGE", "2"))

# --- киберспорт ---
CYBER_HOURS = float(os.getenv("CYBER_HOURS", "24"))        # окно поиска матчей, ч
CYBER_TOP = int(os.getenv("CYBER_TOP", "5"))               # сколько матчей показывать
CYBER_MIN_P = float(os.getenv("CYBER_MIN_P", "0.58"))      # мин. шанс фаворита в матче
RATING_PAGES = int(os.getenv("RATING_PAGES", "5"))         # страниц по 100 прошедших матчей для рейтинга
MIN_HISTORY = int(os.getenv("MIN_HISTORY", "6"))           # мин. матчей в истории команды
ELO_K = float(os.getenv("ELO_K", "16"))

BASE = "https://api.the-odds-api.com/v4"
PANDA = "https://api.pandascore.co"
GAMES = {"cs2": ("csgo", "CS2"), "dota": ("dota2", "Dota 2")}

subscribers: set[int] = set()
sent_keys: set[str] = set()


def esc(x) -> str:
    return html.escape(str(x))


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def when_iso(s: str) -> str:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    mins = max(int((t - datetime.now(timezone.utc)).total_seconds() // 60), 0)
    h, m = divmod(mins, 60)
    left = f"через {h} ч {m} мин" if h else f"через {m} мин"
    return f"{t.astimezone(TZ).strftime('%d.%m %H:%M')} ({left})"


def when(ev: dict) -> str:
    return when_iso(ev["commence_time"])


async def get_json(s, url, params, headers=None):
    async with s.get(url, params=params, headers=headers,
                     timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        return await r.json()


# =====================================================================
#                       ОБЫЧНЫЙ СПОРТ (The Odds API)
# =====================================================================

async def fetch_events(hours: float) -> list[dict]:
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
                    "apiKey": ODDS_API_KEY, "regions": REGIONS, "markets": "h2h",
                    "oddsFormat": "decimal",
                    "commenceTimeFrom": iso(now), "commenceTimeTo": iso(end),
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


async def fetch_market(s, ev: dict, market: str) -> list:
    try:
        data = await get_json(s, f"{BASE}/sports/{ev['sport_key']}/events/{ev['id']}/odds", {
            "apiKey": ODDS_API_KEY, "regions": REGIONS, "markets": market, "oddsFormat": "decimal",
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
                stake = min(0.25 * kelly, 0.03) * 100
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


# =====================================================================
#                 КИБЕРСПОРТ: CS2 и Dota 2 (PandaScore)
# =====================================================================

_rating_cache: dict[str, tuple[float, dict, dict]] = {}


async def panda_get(s, path: str, params: dict):
    return await get_json(s, PANDA + path, params, headers={"Authorization": f"Bearer {PANDA_TOKEN}"})


def compute_elo(matches: list[dict]) -> tuple[dict, dict]:
    """Рейтинг Elo по картам + история каждой команды (по возрастанию времени)."""
    elo: dict[int, float] = {}
    hist: dict[int, list] = {}
    done = [m for m in matches if m.get("winner_id") and not m.get("forfeit")
            and len(m.get("opponents", [])) == 2]
    done.sort(key=lambda m: m.get("scheduled_at") or m.get("begin_at") or "")
    for m in done:
        a = m["opponents"][0]["opponent"]["id"]
        b = m["opponents"][1]["opponent"]["id"]
        sc = {r["team_id"]: r.get("score") or 0 for r in m.get("results", [])}
        ma, mb = sc.get(a, 0), sc.get(b, 0)
        if ma + mb == 0:
            ma, mb = (1, 0) if m["winner_id"] == a else (0, 1)
        ra, rb = elo.get(a, 1500.0), elo.get(b, 1500.0)
        e = 1 / (1 + 10 ** (-(ra - rb) / 400))
        delta = ELO_K * (ma - (ma + mb) * e)
        elo[a], elo[b] = ra + delta, rb - delta
        ts = m.get("scheduled_at") or ""
        hist.setdefault(a, []).append({"ts": ts, "win": int(ma > mb), "opp": b, "mw": ma, "ml": mb})
        hist.setdefault(b, []).append({"ts": ts, "win": int(mb > ma), "opp": a, "mw": mb, "ml": ma})
    return elo, hist


async def get_ratings(s, slug: str) -> tuple[dict, dict]:
    cached = _rating_cache.get(slug)
    if cached and time.time() - cached[0] < 3 * 3600:
        return cached[1], cached[2]
    matches: list[dict] = []
    for page in range(1, RATING_PAGES + 1):
        data = await panda_get(s, f"/{slug}/matches/past", {
            "sort": "-scheduled_at", "page[size]": 100, "page[number]": page})
        if not data:
            break
        matches.extend(data)
    elo, hist = compute_elo(matches)
    _rating_cache[slug] = (time.time(), elo, hist)
    return elo, hist


async def fetch_upcoming_cyber(s, slug: str, hours: float) -> list[dict]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    data = await panda_get(s, f"/{slug}/matches/upcoming", {
        "sort": "scheduled_at", "page[size]": 50,
        "range[scheduled_at]": f"{iso(now)},{iso(end)}",
    })
    return [m for m in data if len(m.get("opponents", [])) == 2 and m.get("scheduled_at")]


def series_dist(p: float, n: int) -> dict[tuple[int, int], float]:
    """Распределение счёта в серии из n карт; p - шанс выиграть карту, счёт (карты фаворита, карты соперника)."""
    q = 1 - p
    d: dict[tuple[int, int], float] = {}
    if n % 2 == 0:  # фиксированное число карт (BO2): возможна ничья
        for a in range(n + 1):
            d[(a, n - a)] = comb(n, a) * p ** a * q ** (n - a)
    else:
        k = (n + 1) // 2
        for j in range(k):
            c = comb(k - 1 + j, j)
            d[(k, j)] = c * p ** k * q ** j
            d[(j, k)] = c * q ** k * p ** j
    return d


def prob(d: dict, cond) -> float:
    return sum(v for (a, b), v in d.items() if cond(a, b))


def esports_predict(elo: dict, hist: dict, a: int, b: int, n: int) -> dict:
    ra, rb = elo.get(a, 1500.0), elo.get(b, 1500.0)
    ha, hb = hist.get(a, []), hist.get(b, [])

    def wform(h):
        last = h[-10:][::-1]
        if not last:
            return 0.5
        w = [0.85 ** i for i in range(len(last))]
        return sum(x["win"] * wi for x, wi in zip(last, w)) / sum(w)

    form_adj = (wform(ha) - wform(hb)) * 50
    meets = [x for x in ha if x["opp"] == b][-5:]
    h2h_w = sum(x["win"] for x in meets)
    h2h_l = len(meets) - h2h_w
    h2h_adj = max(-24, min(24, (h2h_w - h2h_l) * 8))

    diff = (ra - rb + form_adj + h2h_adj) * 0.75  # сжатие: киберспорт шумный
    p_a = min(max(1 / (1 + 10 ** (-diff / 400)), 0.15), 0.85)
    fav_is_a = p_a >= 0.5
    pf = max(p_a, 1 - p_a)
    dist = series_dist(pf, n)
    pw = prob(dist, lambda x, y: x > y)

    def last10(h):
        last = h[-10:]
        maps_w = sum(x["mw"] for x in last)
        maps_t = maps_w + sum(x["ml"] for x in last)
        return sum(x["win"] for x in last), len(last), (maps_w / maps_t * 100 if maps_t else 0)

    return {
        "fav_is_a": fav_is_a, "pf": pf, "pw": pw, "dist": dist, "n": n,
        "ra": ra, "rb": rb, "form_a": last10(ha), "form_b": last10(hb),
        "h2h": (h2h_w, h2h_l, len(meets)), "hist_a": len(ha), "hist_b": len(hb),
    }


def minodds(p: float) -> float:
    return 1.05 / p


def score_label(a: int, b: int, fav: str, dog: str) -> str:
    if a > b:
        return f"{fav} {a}:{b}"
    if b > a:
        return f"{dog} {b}:{a}"
    return f"ничья {a}:{b}"


def fmt_cyber(m: dict, gtitle: str, pr: dict) -> str:
    A = m["opponents"][0]["opponent"]["name"]
    B = m["opponents"][1]["opponent"]["name"]
    n, d, pw = pr["n"], pr["dist"], pr["pw"]
    fav, dog = (A, B) if pr["fav_is_a"] else (B, A)
    fa, da = esc(fav), esc(dog)
    low = min(pr["hist_a"], pr["hist_b"]) < MIN_HISTORY
    tag = "🔥" if pw >= 0.72 else "✅" if pw >= 0.62 else "🟡" if pw >= 0.55 else "⚪"
    if low:
        tag = "⚠️"
    league = (m.get("league") or {}).get("name", "")

    L = [
        f"🎮 <b>{gtitle}</b> · {esc(league)}",
        f"{esc(A)} vs {esc(B)} · BO{n}",
        f"<i>{when_iso(m['scheduled_at'])}</i>",
        f"{tag} <b>Прогноз: победа «{fa}» — {pw * 100:.0f}%</b>",
        f"👉 Брать при коэффициенте от {minodds(pw):.2f}"
        + (" (у букмекера он, скорее всего, ниже — смотрите допы)" if minodds(pw) < 1.3 else ""),
        "",
        "📊 <b>Анализ</b>",
        f"• Рейтинг: {esc(A)} {pr['ra']:.0f} · {esc(B)} {pr['rb']:.0f}",
        f"• Форма (до 10 матчей): {esc(A)} {pr['form_a'][0]}/{pr['form_a'][1]} побед · "
        f"{esc(B)} {pr['form_b'][0]}/{pr['form_b'][1]}",
        f"• Карты: {esc(A)} {pr['form_a'][2]:.0f}% · {esc(B)} {pr['form_b'][2]:.0f}%",
    ]
    hw, hl, ht = pr["h2h"]
    if ht:
        L.append(f"• Личные встречи (посл. {ht}): {esc(A)} {hw}:{hl} {esc(B)}")
    if low:
        L.append("⚠️ Мало истории у одной из команд — прогноз ненадёжный.")

    if n == 1:
        L.append("ℹ️ BO1 — высокая дисперсия, допов по картам нет.")
        return "\n".join(L)

    L += ["", "🎯 <b>Точный счёт (самые вероятные)</b>"]
    for (x, y), v in sorted(d.items(), key=lambda kv: -kv[1])[:3]:
        L.append(f"• {score_label(x, y, fa, da)} — {v * 100:.0f}% (к. от {minodds(v):.2f})")

    if n % 2 == 1:
        k = (n + 1) // 2
        L += ["", "📐 <b>Фора по картам</b>"]
        for h in [x for x in (1.5, 2.5) if x < k]:
            pf_h = prob(d, lambda x, y: x - y > h)
            ok1 = " ✅" if pf_h >= 0.6 else ""
            ok2 = " ✅" if (1 - pf_h) >= 0.6 else ""
            L.append(f"• {fa} (−{h:g}) — {pf_h * 100:.0f}% (к. от {minodds(pf_h):.2f}){ok1}")
            L.append(f"• {da} (+{h:g}) — {(1 - pf_h) * 100:.0f}% (к. от {minodds(1 - pf_h):.2f}){ok2}")
        L += ["", "📏 <b>Тотал карт</b>"]
        t = k + 0.5
        while t <= n - 0.5:
            po = prob(d, lambda x, y: x + y > t)
            L.append(f"• Больше {t:g} — {po * 100:.0f}% (к. от {minodds(po):.2f})"
                     f"{' ✅' if po >= 0.6 else ''}")
            L.append(f"• Меньше {t:g} — {(1 - po) * 100:.0f}% (к. от {minodds(1 - po):.2f})"
                     f"{' ✅' if (1 - po) >= 0.6 else ''}")
            t += 1
    else:
        pd_ = prob(d, lambda x, y: x == y)
        L.append(f"• Ничья по картам — {pd_ * 100:.0f}% (к. от {minodds(pd_):.2f})")
    L.append("")
    L.append("<i>Коэффициент «от» — минимум, при котором ставка по модели выгодна (с запасом 5%). "
             "Сравните с линией своего букмекера.</i>")
    return "\n".join(L)


async def cyber_report(games: list[str], hours: float, only_new: bool = False) -> list[str]:
    cards = []
    total = 0
    async with aiohttp.ClientSession() as s:
        for g in games:
            slug, gtitle = GAMES[g]
            matches = await fetch_upcoming_cyber(s, slug, hours)
            total += len(matches)
            if not matches:
                continue
            elo, hist = await get_ratings(s, slug)
            for m in matches:
                a = m["opponents"][0]["opponent"]["id"]
                b = m["opponents"][1]["opponent"]["id"]
                n = m.get("number_of_games") or 3
                pr = esports_predict(elo, hist, a, b, n)
                low = min(pr["hist_a"], pr["hist_b"]) < MIN_HISTORY
                strong = pr["pw"] >= CYBER_MIN_P or (n % 2 == 0 and pr["pf"] >= 0.65)
                if not strong:
                    continue
                score = pr["pw"] * (0.92 if low else 1.0)
                cards.append((score, m, gtitle, pr))
    cards.sort(key=lambda c: -c[0])
    out = []
    for _, m, gtitle, pr in cards[:CYBER_TOP]:
        key = f"cy:{m['id']}:{round(pr['pw'] * 20)}"
        if only_new and key in sent_keys:
            continue
        sent_keys.add(key)
        out.append(fmt_cyber(m, gtitle, pr))
    if not only_new:
        names = " + ".join(GAMES[g][1] for g in games)
        head = f"{names}: матчей в ближайшие {hours:g} ч — {total}, с явным фаворитом — {len(cards)}"
        out.insert(0, head)
    return out


# =====================================================================
#                             TELEGRAM
# =====================================================================

BTN_SPORT, BTN_CYBER = "🔍 Спорт: вилки и value", "🎮 Киберспорт"
BTN_CS2, BTN_DOTA = "🔫 CS2", "🐉 Dota 2"
BTN_AUTO, BTN_HELP = "🔔 Автоуведомления", "ℹ️ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_SPORT), KeyboardButton(text=BTN_CYBER)],
        [KeyboardButton(text=BTN_CS2), KeyboardButton(text=BTN_DOTA)],
        [KeyboardButton(text=BTN_AUTO), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

HELP = (
    "<b>Что я умею</b>\n"
    "🔍 /scan [часов] — спорт: вилки и value-ставки + прогноз, фора, тотал, угловые\n"
    "🎮 /cyber [часов] — киберспорт: CS2 и Dota 2\n"
    "🔫 /cs2 [часов] — только CS2\n"
    "🐉 /dota [часов] — только Dota 2\n"
    "🧮 /ev коэф шанс% — проверить ставку, например /ev 1.9 60\n"
    "🔔 /auto — автоуведомления вкл/выкл\n\n"
    "Пример: <code>/cs2 6</code> — матчи CS2 в ближайшие 6 часов.\n"
    "⚠️ Гарантий выигрыша нет. Прогнозы — оценка модели, ставьте только то, что готовы потерять."
)

NEED_PANDA = ("Для киберспорта нужен бесплатный ключ PandaScore (pandascore.co). "
              "Добавьте его в переменную окружения PANDASCORE_TOKEN на BotHost и перезапустите бота.")

dp = Dispatcher()


def parse_hours(args: str | None, default: float) -> float:
    if not args:
        return default
    return min(max(float(args.replace(",", ".").split()[0]), 0.5), 72)


async def run_scan(m: Message, hours: float):
    if not ODDS_API_KEY:
        await m.answer("Для спорта нужен ключ The Odds API: переменная ODDS_API_KEY.")
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


async def run_cyber(m: Message, games: list[str], hours: float):
    if not PANDA_TOKEN:
        await m.answer(NEED_PANDA)
        return
    await m.answer("Анализирую команды и историю матчей…")
    try:
        report = await cyber_report(games, hours)
    except aiohttp.ClientResponseError as e:
        await m.answer(f"PandaScore вернул ошибку {e.status}. Проверьте ключ PANDASCORE_TOKEN.")
        return
    except Exception as e:
        await m.answer(f"Ошибка запроса: {esc(e)}")
        return
    if len(report) == 1:
        report.append("Уверенных фаворитов нет — матчи равные. Попробуйте /cyber 48")
    for text in report:
        await m.answer(text)


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я ищу ставки с минимальным риском и делаю прогнозы:\n"
        "🔒 вилки и 📈 value-ставки по обычному спорту\n"
        "🎮 глубокий анализ команд CS2 и Dota 2: счёт, фора, тотал карт\n\n"
        "Меню внизу — кнопки команд. Справка: /help",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def help_cmd(m: Message):
    await m.answer(HELP, reply_markup=MAIN_KB)


@dp.message(Command("scan"))
async def scan_cmd(m: Message, command: CommandObject):
    try:
        hours = parse_hours(command.args, WINDOW_HOURS)
    except ValueError:
        await m.answer("Укажите число часов, например: /scan 3")
        return
    await run_scan(m, hours)


@dp.message(F.text == BTN_SPORT)
async def scan_btn(m: Message):
    await run_scan(m, WINDOW_HOURS)


async def cyber_cmd_impl(m: Message, command: CommandObject | None, games: list[str]):
    try:
        hours = parse_hours(command.args if command else None, CYBER_HOURS)
    except ValueError:
        await m.answer("Укажите число часов, например: /cyber 6")
        return
    await run_cyber(m, games, hours)


@dp.message(Command("cyber"))
async def cyber_cmd(m: Message, command: CommandObject):
    await cyber_cmd_impl(m, command, ["cs2", "dota"])


@dp.message(Command("cs2"))
async def cs2_cmd(m: Message, command: CommandObject):
    await cyber_cmd_impl(m, command, ["cs2"])


@dp.message(Command("dota"))
async def dota_cmd(m: Message, command: CommandObject):
    await cyber_cmd_impl(m, command, ["dota"])


@dp.message(F.text == BTN_CYBER)
async def cyber_btn(m: Message):
    await cyber_cmd_impl(m, None, ["cs2", "dota"])


@dp.message(F.text == BTN_CS2)
async def cs2_btn(m: Message):
    await cyber_cmd_impl(m, None, ["cs2"])


@dp.message(F.text == BTN_DOTA)
async def dota_btn(m: Message):
    await cyber_cmd_impl(m, None, ["dota"])


@dp.message(Command("ev"))
async def ev_cmd(m: Message, command: CommandObject):
    try:
        odds_s, p_s = (command.args or "").replace(",", ".").replace("%", "").split()[:2]
        odds, p = float(odds_s), float(p_s) / 100
        assert odds > 1 and 0 < p < 1
    except Exception:
        await m.answer("Формат: /ev коэффициент шанс%\nПример: /ev 1.9 60")
        return
    edge = (p * odds - 1) * 100
    if edge <= 0:
        await m.answer(f"Ожидаемая выгода {edge:+.1f}% — ставка невыгодна. "
                       f"Брать стоит от коэффициента {minodds(p):.2f}.")
        return
    kelly = (p * odds - 1) / (odds - 1)
    stake = min(0.25 * kelly, 0.03) * 100
    await m.answer(f"✅ Ожидаемая выгода: {edge:+.1f}%\nСумма ставки: ~{stake:.1f}% банка "
                   f"(четверть Келли, не больше 3%).")


@dp.message(Command("auto"))
@dp.message(F.text == BTN_AUTO)
async def auto(m: Message):
    if m.chat.id in subscribers:
        subscribers.discard(m.chat.id)
        await m.answer("Автоуведомления выключены.")
    else:
        subscribers.add(m.chat.id)
        await m.answer(f"Автоуведомления включены: проверка каждые {SCAN_MINUTES} мин "
                       f"(спорт — окно {WINDOW_HOURS:g} ч, киберспорт — {CYBER_HOURS:g} ч).")


async def auto_loop(bot: Bot):
    while True:
        await asyncio.sleep(SCAN_MINUTES * 60)
        if not subscribers:
            continue
        reports: list[str] = []
        if ODDS_API_KEY:
            try:
                reports += await build_report(WINDOW_HOURS, only_new=True)
            except Exception:
                pass
        if PANDA_TOKEN:
            try:
                reports += await cyber_report(["cs2", "dota"], CYBER_HOURS, only_new=True)
            except Exception:
                pass
        for chat_id in list(subscribers):
            for text in reports:
                try:
                    await bot.send_message(chat_id, text)
                except Exception:
                    pass


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота и меню"),
        BotCommand(command="scan", description="Спорт: вилки, value, прогноз и допы"),
        BotCommand(command="cyber", description="Киберспорт: CS2 + Dota 2"),
        BotCommand(command="cs2", description="Прогнозы CS2: счёт, фора, тотал"),
        BotCommand(command="dota", description="Прогнозы Dota 2: счёт, фора, тотал"),
        BotCommand(command="ev", description="Проверить ставку: /ev коэф шанс%"),
        BotCommand(command="auto", description="Автоуведомления вкл/выкл"),
        BotCommand(command="help", description="Справка по командам"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    asyncio.create_task(auto_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
