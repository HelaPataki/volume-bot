"""Бот аномальных объёмов (4-часовые свечи, спот Binance).

  python bot.py history   разовая проверка идеи по истории всего рынка
  python bot.py monitor   поиск аномалий на последних свечах (каждые 4 часа)
  python bot.py check     раз в сутки: что было после сигналов, по понедельникам сводка
"""
import os
import sys
import csv
import json
import time
import random
import statistics
import requests
from datetime import datetime, timezone

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")
KEY = os.environ.get("COINALYZE_KEY", "")

CZ = "https://api.coinalyze.net/v1"
SPOT = "https://data-api.binance.vision"

# ======================= НАСТРОЙКИ (можно менять) =======================
MCAP_MIN = 20_000_000    # минимальная капитализация монеты
LIVE_MULT = 5.0          # порог для живых алертов: объём свечи в N раз выше нормы
MULT_MIN = 3.0           # минимальный порог в исторической проверке
NORM_N = 30              # норма = медиана объёма за последние 30 свечей (5 суток)
LONG_N = 180             # длинная база (30 суток), нужна для столбца «Спячка»
CALM_MAX = 2.5           # «тихо»: за 30 свечей до сигнала не было свечи выше 2.5 норм
MIN_VOL_USD = 100_000    # объём сигнальной свечи не меньше $100k
MIN_NORM_USD = 10_000    # «норма» монеты не меньше $10k за свечу (отсев совсем мёртвых)
COOLDOWN_C = 60          # 10 суток (60 свечей) без повторных сигналов по монете
HORIZON_C = 60           # окно слежки: 10 суток (60 свечей)
HISTORY_START = "2025-01-01"
CONTROL_P = 0.01         # доля «тихих» свечей, берущихся в контрольную группу
# =======================================================================

CANDLE_S = 4 * 3600
UNIVERSE_FILE = "universe.json"
STATE_FILE = "state.json"
SIGNALS_FILE = "signals.csv"
HISTORY_FILE = "history_events.csv"
HISTORY_SUMMARY = "history_summary.txt"

ST_OPEN = "в процессе"
ST_DONE = "завершено"

COLS = [
    "kind", "coin", "symbol", "time_utc", "candle_ts",
    "price", "candle_pct",
    "vol_usd", "norm_usd", "above_pct", "above_x", "peak30_x", "sleep", "cap_usd",
    "up_pct", "up_days", "dn_pct", "dn_days", "move_pct",
    "big10_days", "big10_dir", "big20_days", "big20_dir",
    "volmax_x", "volmax_days", "complete", "status", "checked_utc",
]
RU = {
    "kind": "Тип", "coin": "Монета", "symbol": "Пара (спот)",
    "time_utc": "Свеча (UTC)", "candle_ts": "Метка времени",
    "price": "Цена закрытия", "candle_pct": "Свеча (%)",
    "vol_usd": "Объём свечи ($)", "norm_usd": "Норма ($)",
    "above_pct": "Выше нормы (%)", "above_x": "Выше нормы (×)",
    "peak30_x": "Пик за 30 св. (×норма)", "sleep": "Спячка (норма 30 / норма 180)",
    "cap_usd": "Капитализация ($)",
    "up_pct": "Макс. рост (%)", "up_days": "Дней до макс. роста",
    "dn_pct": "Макс. падение (%)", "dn_days": "Дней до макс. падения",
    "move_pct": "Макс. движение (%)",
    "big10_days": "Дней до ±10%", "big10_dir": "Куда ±10%",
    "big20_days": "Дней до ±20%", "big20_dir": "Куда ±20%",
    "volmax_x": "Макс. объём после (×норма)", "volmax_days": "Дней до макс. объёма",
    "complete": "10 суток прошли", "status": "Статус", "checked_utc": "Проверено (UTC)",
}


# ----------------------------- утилиты -----------------------------

def ts_str(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def sig(x, digits=6):
    return "" if x is None else float(f"{x:.{digits}g}")


def rnd(x, n=2):
    return "" if x is None else round(x, n)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    if x >= 1e9:
        return f"${x / 1e9:.1f}B"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    return f"${x / 1e3:.0f}k"


def tg(text):
    """Отправка в Telegram: 3 попытки; при сбое сети бот не падает."""
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT, "text": text[:4000]},
                timeout=30,
            )
            print("telegram:", r.status_code, r.text[:150])
            if r.status_code == 200:
                return True
            if r.status_code != 429 and 400 <= r.status_code < 500:
                return False
        except requests.RequestException as e:
            print(f"telegram: ошибка отправки (попытка {attempt + 1}):", e)
        time.sleep(5 * (attempt + 1))
    print("telegram: сообщение не отправлено")
    return False


def get_json(url, headers=None, **params):
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", "20"))
            except ValueError:
                wait = 20
            time.sleep(wait + 1)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{url} -> {r.status_code}: {r.text[:150]}")
        return r.json()
    raise RuntimeError(f"{url}: не получилось после нескольких попыток")


def cz(path, **params):
    return get_json(CZ + path, headers={"api_key": KEY}, **params)


# ----------------------- список монет (вселенная) -----------------------

def mcaps():
    res = {}
    for page in range(1, 9):
        data = get_json(
            "https://api.coingecko.com/api/v3/coins/markets",
            vs_currency="usd", order="market_cap_desc", per_page=250, page=page,
        )
        for c in data:
            s = c["symbol"].upper()
            if c.get("market_cap") and s not in res:
                res[s] = c["market_cap"]
        if not data or (data[-1].get("market_cap") or 0) < MCAP_MIN:
            break
        time.sleep(3)
    return res


def build_universe():
    """Монеты с бессрочным фьючерсом на Binance, капитализацией от порога и спот-парой."""
    bases = []
    for m in cz("/future-markets"):
        if not m.get("symbol", "").endswith(".A"):
            continue
        if not m.get("is_perpetual") or m.get("quote_asset") != "USDT":
            continue
        bases.append(m.get("base_asset", "").upper())
    caps = mcaps()
    valid = {x["symbol"] for x in get_json(SPOT + "/api/v3/ticker/price")}
    uni = {}
    for base in dict.fromkeys(bases):
        cap = caps.get(base) or caps.get(base.removeprefix("1000"))
        if not cap or cap < MCAP_MIN:
            continue
        for cand in (base + "USDT", base.removeprefix("1000") + "USDT"):
            if cand in valid and cand.isascii():
                uni[cand] = {"base": base, "cap": cap}
                break
    return uni


def save_universe(uni):
    with open(UNIVERSE_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": int(time.time()), "coins": uni}, f, ensure_ascii=False)


def load_universe():
    """Список монет обновляется раз в сутки, в остальное время берётся из файла."""
    try:
        with open(UNIVERSE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - d["ts"] < 24 * 3600 and d["coins"]:
            return d["coins"]
    except Exception:
        pass
    uni = build_universe()
    save_universe(uni)
    return uni


# ----------------------------- свечи -----------------------------

def klines(symbol, start_s=None, limit=1000):
    params = {"symbol": symbol, "interval": "4h", "limit": limit}
    if start_s is not None:
        params["startTime"] = int(start_s) * 1000
    raw = get_json(SPOT + "/api/v3/klines", **params)
    return [
        {"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4]), "qv": float(k[7])}
        for k in raw
    ]


def klines_since(symbol, start_s):
    out, cursor = [], start_s
    while True:
        part = klines(symbol, start_s=cursor, limit=1000)
        if not part:
            break
        out.extend(part)
        if len(part) < 1000:
            break
        cursor = part[-1]["t"] + CANDLE_S
        time.sleep(0.2)
    return out


def closed_only(cs, now):
    return [c for c in cs if c["t"] + CANDLE_S <= now]


# ----------------------- детектор и результаты -----------------------

def spike_at(cs, i):
    """Объём свечи i относительно нормы (медиана 30 предыдущих свечей)."""
    if i < LONG_N:
        return None
    prev30 = [c["qv"] for c in cs[i - NORM_N:i]]
    n30 = statistics.median(prev30)
    if n30 <= 0:
        return None
    n180 = statistics.median([c["qv"] for c in cs[i - LONG_N:i]])
    vol = cs[i]["qv"]
    return {
        "vol": vol, "norm": n30, "mult": vol / n30,
        "calm": max(prev30) / n30,
        "sleep": (n30 / n180) if n180 > 0 else None,
    }


def is_signal(s, mult_min):
    return (bool(s) and s["mult"] >= mult_min and s["calm"] <= CALM_MAX
            and s["vol"] >= MIN_VOL_USD and s["norm"] >= MIN_NORM_USD)


def is_quiet(s):
    return bool(s) and s["mult"] < 1.5 and s["calm"] <= CALM_MAX and s["norm"] >= MIN_NORM_USD


def outcomes(cs, i, norm):
    """Что было после свечи i (вход = закрытие этой свечи) в пределах 10 суток."""
    entry = cs[i]["c"]
    fut = cs[i + 1:i + 1 + HORIZON_C]
    if not fut or entry <= 0:
        return None
    up = dn = 0.0
    up_k = dn_k = 0
    first = {10: None, 20: None}
    vmax, vmax_k = 0.0, 0
    for k, c in enumerate(fut):
        hi = (c["h"] / entry - 1) * 100
        lo = (c["l"] / entry - 1) * 100
        if hi > up:
            up, up_k = hi, k
        if lo < dn:
            dn, dn_k = lo, k
        for thr in (10, 20):
            if first[thr] is None and (hi >= thr or lo <= -thr):
                first[thr] = (k, "вверх" if hi >= -lo else "вниз")
        if norm > 0 and c["qv"] / norm > vmax:
            vmax, vmax_k = c["qv"] / norm, k
    d = lambda k: round(k * CANDLE_S / 86400, 2)
    out = {
        "up_pct": rnd(up), "up_days": d(up_k), "dn_pct": rnd(dn), "dn_days": d(dn_k),
        "move_pct": rnd(max(up, -dn)),
        "big10_days": d(first[10][0]) if first[10] else "", "big10_dir": first[10][1] if first[10] else "",
        "big20_days": d(first[20][0]) if first[20] else "", "big20_dir": first[20][1] if first[20] else "",
        "volmax_x": rnd(vmax, 1), "volmax_days": d(vmax_k),
        "complete": "да" if len(fut) >= HORIZON_C else "нет",
    }
    return out


def make_row(kind, sym, base, cap, cs, i, s, out, status=""):
    c = cs[i]
    row = {
        "kind": kind, "coin": base, "symbol": sym,
        "time_utc": ts_str(c["t"]), "candle_ts": c["t"],
        "price": sig(c["c"]),
        "candle_pct": rnd((c["c"] / c["o"] - 1) * 100) if c["o"] else "",
        "vol_usd": int(s["vol"]), "norm_usd": int(s["norm"]),
        "above_pct": rnd((s["mult"] - 1) * 100, 0), "above_x": rnd(s["mult"], 1),
        "peak30_x": rnd(s["calm"], 1), "sleep": rnd(s["sleep"], 2),
        "cap_usd": int(cap), "status": status,
    }
    row.update(out if out else {"complete": "нет"})
    return row


# ----------------------------- csv -----------------------------

def read_csv(path):
    if not os.path.exists(path):
        return []
    back = {v: k for k, v in RU.items()}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{back.get(k, k): v for k, v in row.items()} for row in csv.DictReader(f)]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([RU[c] for c in COLS])
        for r in rows:
            w.writerow([r.get(c, "") for c in COLS])


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, sort_keys=True)


# ----------------------------- статистика -----------------------------

def group_stats(label, rows):
    done = [r for r in rows if r.get("complete") == "да"]
    if not done:
        return f"{label}: завершённых наблюдений пока нет"
    n = len(done)
    moves = [num(r["move_pct"]) for r in done if num(r["move_pct"]) is not None]
    b10 = [r for r in done if num(r.get("big10_days")) is not None]
    b20 = [r for r in done if num(r.get("big20_days")) is not None]
    up10 = sum(1 for r in b10 if r.get("big10_dir") == "вверх")
    lines = [
        f"{label}: {n} шт.",
        f"  медиана макс. движения за 10 сут.: {statistics.median(moves):.1f}%",
        f"  было ±10%: {len(b10) / n * 100:.0f}% (вверх {up10}, вниз {len(b10) - up10})",
        f"  было ±20%: {len(b20) / n * 100:.0f}%",
    ]
    if b10:
        days = [num(r["big10_days"]) for r in b10]
        lines.append(f"  медиана дней до ±10%: {statistics.median(days):.1f}")
    return "\n".join(lines)


# ----------------------------- команды -----------------------------

def cmd_history():
    start = int(datetime.strptime(HISTORY_START, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    now = int(time.time())
    uni = build_universe()
    save_universe(uni)
    print("монет в списке:", len(uni))
    rng = random.Random(42)
    events, control = [], []
    for n_coin, (sym, info) in enumerate(uni.items(), 1):
        try:
            cs = closed_only(klines_since(sym, start), now)
        except Exception as e:
            print("пропускаю", sym, e)
            continue
        last_event = -10 ** 9
        for i in range(LONG_N, len(cs) - 1):
            s = spike_at(cs, i)
            if not s:
                continue
            if is_signal(s, MULT_MIN):
                if i - last_event >= COOLDOWN_C:
                    out = outcomes(cs, i, s["norm"])
                    events.append(make_row("сигнал", sym, info["base"], info["cap"], cs, i, s, out, "история"))
                    last_event = i
            elif is_quiet(s) and i + HORIZON_C < len(cs) and rng.random() < CONTROL_P:
                out = outcomes(cs, i, s["norm"])
                control.append(make_row("контроль", sym, info["base"], info["cap"], cs, i, s, out, "история"))
        if n_coin % 25 == 0:
            print(f"обработано монет: {n_coin}/{len(uni)}, сигналов: {len(events)}")
        time.sleep(0.2)

    rows = sorted(events + control, key=lambda r: (r["kind"], r["candle_ts"]))
    write_csv(HISTORY_FILE, rows)

    days = (now - start) / 86400
    lines = [
        f"📜 Историческая проверка: {HISTORY_START} … {ts_str(now)[:10]}",
        f"Монет: {len(uni)}. Сигналов найдено: {len(events)}, контрольных свечей: {len(control)}.",
        "Сигнал = первая вспышка объёма после тихого периода (10 суток тишины после неё).",
        "Движение считается в любую сторону за 10 суток после закрытия свечи.",
        "",
        group_stats("Контроль (обычные тихие свечи)", control),
    ]
    for t in (3, 5, 10):
        grp = [r for r in events if (num(r["above_x"]) or 0) >= t]
        lines.append("")
        lines.append(group_stats(f"Вспышка от ×{t}", grp))
        lines.append(f"  сигналов в сутки по всему рынку: ≈{len(grp) / days:.1f}")
    text = "\n".join(lines)
    with open(HISTORY_SUMMARY, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    tg(text)


def cmd_monitor():
    if os.environ.get("TEST") == "1":
        tg("✅ Тест: бот аномальных объёмов на связи")
    now = int(time.time())
    uni = load_universe()
    state = load_state()
    hits = []
    for sym, info in uni.items():
        try:
            cs = closed_only(klines(sym, limit=LONG_N + 12), now)
        except Exception as e:
            print("пропускаю", sym, e)
            continue
        time.sleep(0.12)
        n = len(cs)
        # две последние закрытые свечи: на случай, если запуск задержался
        for i in (n - 2, n - 1):
            if i < LONG_N:
                continue
            s = spike_at(cs, i)
            if not is_signal(s, LIVE_MULT):
                continue
            if cs[i]["t"] - state.get(sym, 0) < COOLDOWN_C * CANDLE_S:
                continue
            state[sym] = cs[i]["t"]
            hits.append((sym, info, cs, i, s))
    print("монет проверено:", len(uni), "сигналов:", len(hits))
    save_state(state)
    if not hits:
        return
    rows = read_csv(SIGNALS_FILE)
    lines = []
    for sym, info, cs, i, s in hits:
        rows.append(make_row("сигнал", sym, info["base"], info["cap"], cs, i, s, None, ST_OPEN))
        c = cs[i]
        pct = (c["c"] / c["o"] - 1) * 100 if c["o"] else 0
        lines.append(
            f"{info['base']}: объём {money(s['vol'])} | норма {money(s['norm'])} | "
            f"+{(s['mult'] - 1) * 100:.0f}% (×{s['mult']:.1f}) | свеча {pct:+.1f}% | кап {money(info['cap'])}"
        )
    write_csv(SIGNALS_FILE, rows)
    tg("🔊 Аномальный объём (последняя 4ч свеча):\n" + "\n".join(lines)
       + "\n\nНаправление неизвестно: смотри график и ликвидность.")


def cmd_check():
    now = int(time.time())
    rows = read_csv(SIGNALS_FILE)
    pending = [r for r in rows if (r.get("status") or ST_OPEN) == ST_OPEN]
    print("сигналов в обработке:", len(pending))
    changed = False
    for r in pending:
        ts = int(num(r.get("candle_ts")) or 0)
        norm = num(r.get("norm_usd")) or 0
        if not ts or not r.get("symbol"):
            continue
        try:
            cs = closed_only(klines(r["symbol"], start_s=ts, limit=HORIZON_C + 3), now)
        except Exception as e:
            print("пропускаю", r["symbol"], e)
            continue
        time.sleep(0.12)
        if not cs or cs[0]["t"] != ts:
            continue
        out = outcomes(cs, 0, norm)
        if not out:
            continue
        r.update(out)
        r["status"] = ST_DONE if out["complete"] == "да" else ST_OPEN
        r["checked_utc"] = ts_str(now)
        changed = True
    if changed:
        write_csv(SIGNALS_FILE, rows)

    is_monday = datetime.fromtimestamp(now, timezone.utc).weekday() == 0
    if is_monday or os.environ.get("FORCE_SUMMARY") == "1":
        week = [r for r in rows if now - (num(r.get("candle_ts")) or 0) <= 7 * 86400]
        text = (f"📊 Сводка по аномальным объёмам\nСигналов за 7 суток: {len(week)}\n"
                f"Всего в таблице: {len(rows)}\n\n"
                + group_stats("Завершённые наблюдения (10 суток прошли)", rows))
        tg(text)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    funcs = {"history": cmd_history, "monitor": cmd_monitor, "check": cmd_check}
    if cmd not in funcs:
        print(__doc__)
        sys.exit(2)
    try:
        funcs[cmd]()
    except Exception as e:
        print("ОШИБКА:", repr(e))
        tg(f"⚠️ Бот аномальных объёмов ({cmd}): ошибка\n{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
