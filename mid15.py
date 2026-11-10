"""Тест идеи «вход от середины сигнальной свечи» на истории.

Для каждого 4-часового сигнала основного фильтра (вспышка от ×3, свеча от 4%, капа $1–400 млн; берутся из history_events.csv):
1) смотрим, обновила ли сигнальная свеча максимум / минимум предыдущих N свечей (N = 1, 6, 30, 90);
2) ставим условный ордер в трёх вариантах: середина свечи (между максимумом и минимумом),
   середина тела (между открытием и закрытием) и POC свечи (цена, где прошёл наибольший объём, по минуткам);
   для каждого считаем ОБА варианта, и лонг, и шорт (какой брать, решим уже при анализе по правилу);
3) ордер срабатывает, когда цена после закрытия сигнальной свечи доходит до середины (по 15-минуткам);
4) после входа смотрим 10 суток: когда достигнуты тейки +5/7/10/12/15%, какая была максимальная просадка
   против позиции до тейка (стоп не ставим), и чем всё закончилось через 10 суток.

Запуск: python mid15.py   Результат: mid15_events.csv"""
import csv
import time

from bot import read_csv, get_json, kyiv_str, num, rnd, tg, SPOT, HISTORY_FILE, CANDLE_S

OUT = "mid15_events.csv"
MULT_MIN, CANDLE_MIN, CAP_MIN, CAP_MAX = 3.0, 4.0, 1_000_000, 400_000_000
LOOKS = (1, 6, 30, 90)            # сколько предыдущих 4-часовых свечей считаем «предыдущим максимумом/минимумом»
PIVOTS = (2, 3, 5)                # «пик»/«дно» структуры: максимум (минимум) выше (ниже) K свечей слева и справа
TPS = (5, 7, 10, 12, 15)          # тейки в процентах
FILL_MAX_C = 480                  # ждём касания середины не дольше 5 суток (480 свечей по 15 минут)
HOLD_C = 960                      # после входа следим 10 суток
FOLLOW_C = FILL_MAX_C + HOLD_C    # всего скачиваем 15 суток 15-минуток
STEP = 900
ENTRIES = ("mid", "body", "poc")  # середина свечи (с тенями) / середина тела / POC свечи (цена с наибольшим объёмом)
POC_BINS = 50


def klines(symbol, interval, start_s, limit):
    raw = get_json(SPOT + "/api/v3/klines", symbol=symbol, interval=interval,
                   limit=limit, startTime=int(start_s) * 1000)
    return [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
            for k in raw]


def klines15(symbol, start_s):
    out, cur = [], start_s
    while len(out) < FOLLOW_C:
        part = klines(symbol, "15m", cur, min(1000, FOLLOW_C - len(out)))
        if not part:
            break
        out.extend(part)
        if len(part) < min(1000, FOLLOW_C - len(out) + len(part)):
            break
        cur = part[-1]["t"] + STEP
        time.sleep(0.15)
    return out


def flags(prev, sig):
    """Обновила ли сигнальная свеча максимум/минимум последних N 4-часовых свечей."""
    out = {}
    for n in LOOKS:
        p = prev[-n:]
        if len(p) < n:
            out[f"hi{n}"] = out[f"lo{n}"] = out[f"ph{n}"] = out[f"pl{n}"] = ""
            continue
        ph, pl = max(x["h"] for x in p), min(x["l"] for x in p)
        out[f"ph{n}"], out[f"pl{n}"] = ph, pl          # предыдущий максимум / минимум (для расчёта по закрытию и «ближе к вершине/низу»)
        out[f"hi{n}"] = "да" if sig["h"] > ph else "нет"   # обновила хвостом
        out[f"lo{n}"] = "да" if sig["l"] < pl else "нет"
    return out


def swings(prev):
    """Последний подтверждённый пик и последнее подтверждённое дно до сигнальной свечи (уровни структуры)."""
    out = {}
    n = len(prev)
    for k in PIVOTS:
        sh = sl = ""
        for j in range(n - 1 - k, k - 1, -1):       # справа от точки должно быть K свечей, и всё это до сигнала
            win = prev[j - k:j + k + 1]
            if sh == "" and prev[j]["h"] == max(x["h"] for x in win):
                sh = prev[j]["h"]
            if sl == "" and prev[j]["l"] == min(x["l"] for x in win):
                sl = prev[j]["l"]
            if sh != "" and sl != "":
                break
        out[f"sh{k}"], out[f"sl{k}"] = sh, sl
    return out


def poc_of(symbol, ts, lo, hi):
    """POC сигнальной свечи: по минутным свечам раскладываем объём по ценам и берём уровень с наибольшим объёмом."""
    m1 = get_json(SPOT + "/api/v3/klines", symbol=symbol, interval="1m", limit=240, startTime=int(ts) * 1000)
    if not m1 or hi <= lo:
        return ""
    step = (hi - lo) / POC_BINS
    vol = [0.0] * POC_BINS
    for k in m1:
        h, l, v = float(k[2]), float(k[3]), float(k[7])
        a = min(POC_BINS - 1, max(0, int((l - lo) / step)))
        b = min(POC_BINS - 1, max(0, int((h - lo) / step)))
        share = v / (b - a + 1)
        for j in range(a, b + 1):
            vol[j] += share
    j = max(range(POC_BINS), key=lambda x: vol[x])
    return lo + (j + 0.5) * step


def find_fill(cs, close, mid):
    """Индекс первой 15-минутки, где цена дошла до середины (в том числе через гэп)."""
    if close == mid:
        return 0
    above = close > mid           # цена выше середины: ждём, когда опустится до неё
    for i, c in enumerate(cs[:FILL_MAX_C]):
        if (above and c["l"] <= mid) or (not above and c["h"] >= mid):
            return i
    return None


def simulate(cs, i_fill, entry, side, now, pre=""):
    """Что было после входа. side = 'L' или 'S'. Следим 10 суток, стопа нет."""
    after = [c for c in cs[i_fill + 1:i_fill + 1 + HOLD_C] if c["t"] + STEP <= now]
    fill_c = cs[i_fill]
    sgn = 1 if side == "L" else -1
    fav = lambda c: ((c["h"] / entry - 1) if side == "L" else (1 - c["l"] / entry)) * 100
    adv = lambda c: ((c["l"] / entry - 1) if side == "L" else (1 - c["h"] / entry)) * 100
    res = {}
    P = pre + side
    # свеча входа: порядок внутри неё неизвестен, учитываем только её закрытие как возможный минус
    run_adv = min(0.0, (fill_c["c"] / entry - 1) * 100 * sgn)
    hit = {}
    best = 0.0
    for k, c in enumerate(after):
        run_adv = min(run_adv, adv(c))
        f = fav(c)
        best = max(best, f)
        for tp in TPS:
            if tp not in hit and f >= tp:
                hit[tp] = (k + 1, run_adv)   # просадка включает свечу, где взят тейк (консервативно)
    for tp in TPS:
        if tp in hit:
            res[f"{P}_tp{tp}_h"] = round((hit[tp][0]) * STEP / 3600, 2)
            res[f"{P}_mae{tp}"] = rnd(hit[tp][1])
        else:
            res[f"{P}_tp{tp}_h"] = ""
            res[f"{P}_mae{tp}"] = rnd(run_adv)   # тейка не было: худшая просадка за весь период
    res[f"{P}_best"] = rnd(best)
    res[f"{P}_worst"] = rnd(run_adv)
    if after:
        res[f"{P}_end"] = rnd((after[-1]["c"] / entry - 1) * 100 * sgn)
    res[f"{P}_full"] = "да" if len(after) >= HOLD_C else "нет"
    return res


def analyze(row, now):
    ts = int(num(row["candle_ts"]))
    sym = row["symbol"]
    need = max(LOOKS)
    k4 = klines(sym, "4h", ts - need * CANDLE_S, need + 1)
    if not k4 or k4[-1]["t"] != ts:
        return None
    sig, prev = k4[-1], k4[:-1]
    out = {"coin": row["coin"], "symbol": sym, "time_kyiv": kyiv_str(ts), "candle_ts": ts,
           "candle_pct": row.get("candle_pct", ""), "above_x": row.get("above_x", ""),
           "cap_usd": row.get("cap_usd", ""), "close": sig["c"], "hi": sig["h"], "lo": sig["l"]}
    out["open"] = sig["o"]
    levels = {"mid": (sig["h"] + sig["l"]) / 2, "body": (sig["o"] + sig["c"]) / 2}
    try:
        levels["poc"] = poc_of(sym, ts, sig["l"], sig["h"])
    except Exception as e:
        print("POC не посчитан", sym, e)
        levels["poc"] = ""
    for e in ENTRIES:
        out[e] = levels[e]
    out.update(flags(prev, sig))
    out.update(swings(prev))
    cs = [c for c in klines15(sym, ts + CANDLE_S) if c["t"] + STEP <= now]
    for e in ENTRIES:
        lv = levels[e]
        if not cs or lv == "":
            out[f"{e}_filled"] = ""
            continue
        i = find_fill(cs, sig["c"], lv)
        if i is None:
            out[f"{e}_filled"] = "нет"
            continue
        out[f"{e}_filled"] = "да"
        out[f"{e}_fill_h"] = round((i + 1) * STEP / 3600, 2)
        for side in ("L", "S"):
            out.update(simulate(cs, i, lv, side, now, pre=e + "_"))
    return out


def columns():
    cols = ["coin", "symbol", "time_kyiv", "candle_ts", "candle_pct", "above_x", "cap_usd", "open", "close", "hi", "lo",
            "mid", "body", "poc"]
    for n in LOOKS:
        cols += [f"ph{n}", f"pl{n}", f"hi{n}", f"lo{n}"]
    for k in PIVOTS:
        cols += [f"sh{k}", f"sl{k}"]
    for e in ENTRIES:
        cols += [f"{e}_filled", f"{e}_fill_h"]
        for side in ("L", "S"):
            for tp in TPS:
                cols += [f"{e}_{side}_tp{tp}_h", f"{e}_{side}_mae{tp}"]
            cols += [f"{e}_{side}_best", f"{e}_{side}_worst", f"{e}_{side}_end", f"{e}_{side}_full"]
    return cols


def ru(c):
    names = {"coin": "Монета", "symbol": "Пара", "time_kyiv": "Свеча (Киев)", "candle_ts": "Метка свечи",
             "candle_pct": "Свеча (%)", "above_x": "Выше нормы (×)", "cap_usd": "Капитализация ($)",
             "open": "Открытие свечи", "close": "Закрытие свечи", "hi": "Максимум свечи", "lo": "Минимум свечи",
             "mid": "Вход: середина свечи", "body": "Вход: середина тела", "poc": "Вход: POC свечи"}
    if c in names:
        return names[c]
    EN = {"mid": "СЕРЕДИНА", "body": "ТЕЛО", "poc": "POC"}
    e0 = c.split("_")[0]
    if e0 in EN:
        rest = c[len(e0) + 1:]
        if rest == "filled":
            return f"{EN[e0]}: ордер сработал"
        if rest == "fill_h":
            return f"{EN[e0]}: часов до входа"
        return f"{EN[e0]} " + ru(rest)
    if c[:2] == "sh":
        return f"Последний пик структуры (K={c[2:]})"
    if c[:2] == "sl":
        return f"Последнее дно структуры (K={c[2:]})"
    if c[:2] == "ph":
        return f"Предыдущий максимум {c[2:]} св."
    if c[:2] == "pl":
        return f"Предыдущий минимум {c[2:]} св."
    if c[:2] == "hi":
        return f"Обновила максимум {c[2:]} св."
    if c[:2] == "lo":
        return f"Обновила минимум {c[2:]} св."
    side = "ЛОНГ" if c[0] == "L" else "ШОРТ"
    k = c[2:]
    if k.startswith("tp") and k.endswith("_h"):
        return f"{side}: часов до +{k[2:-2]}%"
    if k.startswith("mae"):
        return f"{side}: макс. минус до +{k[3:]}% (%)"
    return {"best": f"{side}: макс. плюс за 10 сут (%)", "worst": f"{side}: макс. минус за 10 сут (%)",
            "end": f"{side}: итог через 10 сут (%)", "full": f"{side}: 10 суток прошли"}[k]


def main():
    now = int(time.time())
    rows = [r for r in read_csv(HISTORY_FILE)
            if r.get("kind") == "сигнал" and (num(r.get("above_x")) or 0) >= MULT_MIN
            and abs(num(r.get("candle_pct")) or 0) >= CANDLE_MIN
            and CAP_MIN <= (num(r.get("cap_usd")) or 0) < CAP_MAX]
    print("сигналов для проверки:", len(rows))
    cols = columns()
    res, t0 = [], time.time()
    for n, r in enumerate(rows, 1):
        if int(num(r["candle_ts"])) + CANDLE_S + 900 > now:
            continue
        try:
            o = analyze(r, now)
            if o:
                res.append(o)
        except Exception as e:
            print("пропускаю", r.get("symbol"), e)
        if n % 100 == 0:
            print(f"{n}/{len(rows)}, прошло {int((time.time() - t0) / 60)} мин")
            _save(cols, res)
        time.sleep(0.15)
    _save(cols, res)
    msg = f"✅ mid15: готово, сигналов {len(res)}, файл {OUT}"
    print(msg)
    tg(msg)


def _save(cols, res):
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([ru(c) for c in cols])
        for r in res:
            w.writerow([r.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
