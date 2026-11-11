"""Тест идеи «вход по тренду на POC предыдущего дня» на истории спота Binance (с 2020 года: есть и растущий 2021, и падения).

Для каждой монеты и каждого дня (сутки по UTC, то есть с 02:00 до 02:00 по Киеву зимой):
1) считаем POC вчерашнего дня (цена, где прошёл наибольший объём, по 15-минуткам);
2) определяем тренд на начало дня тремя способами (всё только по прошлым данным):
   - глобальный: дневное закрытие выше / ниже средней за 50 и за 200 дней;
   - локальный: структура пиков и днищ на 4-часовом графике (выше-выше = вверх, ниже-ниже = вниз, иначе боковик);
   - средний: та же структура на дневном графике;
3) сегодня цена касается вчерашнего POC? Тогда вход (лонг или шорт решаем при анализе по тренду);
   ордер живёт только в течение этого дня;
4) после входа 7 суток следим, через сколько часов цена прошла +1/2/3/5% и −1/2/3/5% от входа,
   и где была через 24 часа, 3 и 7 суток. Из этого потом считаются любые тейки и стопы.

Запуск: python trendpoc.py   Результат: trendpoc_events.csv"""
import csv
import os
import time
from datetime import datetime, timezone

from bot import SPOT, get_json, rnd, tg

COINS = os.environ.get("COINS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,LTCUSDT").split(",")
START = os.environ.get("START", "2020-01-01")
OUT = "trendpoc_events.csv"
STEP = 900
DAY_C = 96                 # 15-минуток в сутках
HOLD_C = 7 * DAY_C         # следим 7 суток после входа
LEVELS = (1, 2, 3, 5)
POC_BINS = 100
K4, KD = 3, 2              # «пик» = выше K свечей слева и справа (4ч и дневной график)


def k15(symbol, start_s, end_s):
    out, cur = [], start_s
    while cur < end_s:
        raw = get_json(SPOT + "/api/v3/klines", symbol=symbol, interval="15m", limit=1000, startTime=int(cur) * 1000)
        if not raw:
            break
        out += [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                 "v": float(k[7])} for k in raw]
        cur = out[-1]["t"] + STEP
        if len(raw) < 1000:
            break
        time.sleep(0.08)
    return out


def agg(cs, n):
    """Склеивает 15-минутки в свечи по n штук (4ч = 16, сутки = 96), выравнивание по UTC."""
    res, cur = [], None
    for c in cs:
        key = c["t"] - c["t"] % (n * STEP)
        if cur is None or cur["t"] != key:
            cur = {"t": key, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"], "parts": [c]}
            res.append(cur)
        else:
            cur["h"] = max(cur["h"], c["h"]); cur["l"] = min(cur["l"], c["l"]); cur["c"] = c["c"]
            cur["v"] += c["v"]; cur["parts"].append(c)
    return res


def poc(parts, lo, hi):
    if hi <= lo:
        return lo
    step = (hi - lo) / POC_BINS
    vol = [0.0] * POC_BINS
    for c in parts:
        a = min(POC_BINS - 1, max(0, int((c["l"] - lo) / step)))
        b = min(POC_BINS - 1, max(0, int((c["h"] - lo) / step)))
        sh = c["v"] / (b - a + 1)
        for j in range(a, b + 1):
            vol[j] += sh
    j = max(range(POC_BINS), key=lambda x: vol[x])
    return lo + (j + 0.5) * step


def structure(cs, k):
    """По последним двум подтверждённым пикам и днищам: 'вверх', 'вниз' или 'боковик'."""
    highs, lows = [], []
    for j in range(len(cs) - 1 - k, k - 1, -1):
        win = cs[j - k:j + k + 1]
        if len(highs) < 2 and cs[j]["h"] == max(x["h"] for x in win):
            highs.append(cs[j]["h"])
        if len(lows) < 2 and cs[j]["l"] == min(x["l"] for x in win):
            lows.append(cs[j]["l"])
        if len(highs) == 2 and len(lows) == 2:
            break
    if len(highs) < 2 or len(lows) < 2:
        return ""
    if highs[0] > highs[1] and lows[0] > lows[1]:
        return "вверх"
    if highs[0] < highs[1] and lows[0] < lows[1]:
        return "вниз"
    return "боковик"


def hours_to(after, entry):
    """Через сколько часов после входа цена впервые прошла каждый уровень вверх и вниз."""
    up = {x: "" for x in LEVELS}
    dn = {x: "" for x in LEVELS}
    for k, c in enumerate(after):
        hrs = round((k + 1) * STEP / 3600, 2)
        for x in LEVELS:
            if up[x] == "" and c["h"] >= entry * (1 + x / 100):
                up[x] = hrs
            if dn[x] == "" and c["l"] <= entry * (1 - x / 100):
                dn[x] = hrs
    return up, dn


COLS = ["Монета", "День (UTC)", "POC вчера", "Открытие дня", "Подход к POC", "Часов до входа",
        "Глоб. тренд 50д", "Глоб. тренд 200д", "Структура 4ч", "Структура дневка",
        "BTC выше 50д", "BTC выше 200д"] + \
       [f"Часов до +{x}%" for x in LEVELS] + [f"Часов до −{x}%" for x in LEVELS] + \
       ["Цена через 24ч (%)", "Цена через 3 сут (%)", "Цена через 7 сут (%)", "7 суток прошли"]


def run_coin(sym, start, now, btc_tr):
    cs = k15(sym, start - 210 * 86400, now)
    cs = [c for c in cs if c["t"] + STEP <= now]
    if len(cs) < 300 * DAY_C:
        print(sym, "мало данных", len(cs))
        return []
    idx = {c["t"]: i for i, c in enumerate(cs)}
    days = agg(cs, DAY_C)
    h4 = agg(cs, 16)
    rows, j4 = [], 0
    for d in range(201, len(days)):
        day, prev = days[d], days[d - 1]
        if day["t"] < start or len(day["parts"]) < DAY_C * 0.9 or len(prev["parts"]) < DAY_C * 0.9:
            continue
        closes = [x["c"] for x in days[d - 200:d]]
        g50 = "вверх" if prev["c"] > sum(closes[-50:]) / 50 else "вниз"
        g200 = "вверх" if prev["c"] > sum(closes) / 200 else "вниз"
        while j4 < len(h4) and h4[j4]["t"] < day["t"]:
            j4 += 1
        st4 = structure(h4[max(0, j4 - 240):j4], K4)
        std = structure(days[max(0, d - 120):d], KD)
        p = poc(prev["parts"], prev["l"], prev["h"])
        side = "сверху" if day["o"] > p else "снизу"
        fill = None
        for k, c in enumerate(day["parts"]):
            if c["l"] <= p <= c["h"]:
                fill = k
                break
        if fill is None:
            continue
        i0 = idx[day["parts"][fill]["t"]]
        after = cs[i0 + 1:i0 + 1 + HOLD_C]
        up, dn = hours_to(after, p)
        def ret(n):
            return rnd((after[n - 1]["c"] / p - 1) * 100) if len(after) >= n else ""
        bt = btc_tr.get(day["t"], ("", ""))
        r = {"Монета": sym.replace("USDT", ""), "День (UTC)": datetime.fromtimestamp(day["t"], timezone.utc).strftime("%Y-%m-%d"),
             "POC вчера": p, "Открытие дня": day["o"], "Подход к POC": side,
             "Часов до входа": round((fill + 1) * STEP / 3600, 2),
             "Глоб. тренд 50д": g50, "Глоб. тренд 200д": g200, "Структура 4ч": st4, "Структура дневка": std,
             "BTC выше 50д": bt[0], "BTC выше 200д": bt[1],
             "Цена через 24ч (%)": ret(DAY_C), "Цена через 3 сут (%)": ret(3 * DAY_C), "Цена через 7 сут (%)": ret(HOLD_C),
             "7 суток прошли": "да" if len(after) >= HOLD_C else "нет"}
        for x in LEVELS:
            r[f"Часов до +{x}%"] = up[x]
            r[f"Часов до −{x}%"] = dn[x]
        rows.append(r)
    if sym == "BTCUSDT":
        for d in range(201, len(days)):
            closes = [x["c"] for x in days[d - 200:d]]
            prev = days[d - 1]
            btc_tr[days[d]["t"]] = ("да" if prev["c"] > sum(closes[-50:]) / 50 else "нет",
                                    "да" if prev["c"] > sum(closes) / 200 else "нет")
        for r in rows:
            t = int(datetime.strptime(r["День (UTC)"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            r["BTC выше 50д"], r["BTC выше 200д"] = btc_tr.get(t, ("", ""))
    print(sym, "дней с касанием POC:", len(rows))
    return rows


def main():
    now = int(time.time())
    start = int(datetime.strptime(START, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    coins = ["BTCUSDT"] + [c for c in COINS if c != "BTCUSDT"]
    btc_tr, rows = {}, []
    for sym in coins:
        try:
            rows += run_coin(sym, start, now, btc_tr)
        except Exception as e:
            print("пропускаю", sym, e)
        with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
    msg = f"✅ trendpoc: готово, монет {len(coins)}, дней с входом {len(rows)}, файл {OUT}"
    print(msg)
    tg(msg)


if __name__ == "__main__":
    main()
