"""Отдельный эксперимент: аномалии объёма на 15-минутных свечах (спот Binance).

  python bot15.py     историческая проверка (без фильтра по размеру свечи)

Ничего не отправляет в основной бот и не трогает его таблицы.
Результаты: history15_events.csv и history15_summary.txt
"""
import os
import csv
import time
import random
import bisect
import statistics
from datetime import datetime, timezone

from bot import build_universe, get_json, tg, kyiv_str, ts_str, rnd, sig, num, SPOT

# ======================= НАСТРОЙКИ (можно менять) =======================
HISTORY_START = os.environ.get("HISTORY15_START", "2026-01-01")  # с какой даты берём историю
CAP_MIN = 1_000_000          # капитализация от $1 млн
CAP_MAX = 400_000_000        # до $400 млн
CANDLE_S = 15 * 60           # 15-минутные свечи
NORM_N = 480                 # норма = медиана объёма за предыдущие 480 свечей (5 суток)
QUIET_C = 96                 # «тихо»: за предыдущие 24 часа (96 свечей) не было вспышки от ×3
MULT_MIN = 3.0               # вспышка от ×3 нормы (в сводке считаем ×3, ×5, ×10)
MIN_VOL_USD = 10_000         # объём сигнальной свечи не меньше $10 тыс.
MIN_NORM_USD = 1_000         # «норма» монеты не меньше $1 тыс. за свечу
HORIZON_C = 192              # следим 48 часов (192 свечи)
CONTROL_P = 0.0003           # доля обычных тихих свечей, берущихся в контрольную группу
LEVELS = (3, 5, 10)          # отмечаем, когда цена впервые ушла на ±3%, ±5%, ±10%
OUT_FILE = "history15_events.csv"
SUMMARY_FILE = "history15_summary.txt"
# =======================================================================

COLS = [
    "kind", "coin", "symbol", "time_utc", "time_kyiv", "candle_ts",
    "price", "candle_pct", "vol_usd", "norm_usd", "above_pct", "above_x",
    "peak96_x", "cap_usd",
    "up_pct", "up_hours", "dn_pct", "dn_hours",
    "move6_pct", "move24_pct", "move48_pct",
    "h_to_3", "dir_3", "h_to_5", "dir_5", "h_to_10", "dir_10", "complete",
]
RU = {
    "kind": "Тип", "coin": "Монета", "symbol": "Пара (спот)",
    "time_utc": "Свеча (UTC)", "time_kyiv": "Свеча (Киев)", "candle_ts": "Метка времени",
    "price": "Цена закрытия", "candle_pct": "Свеча (%)",
    "vol_usd": "Объём свечи ($)", "norm_usd": "Норма ($)",
    "above_pct": "Выше нормы (%)", "above_x": "Выше нормы (×)",
    "peak96_x": "Пик за 24ч до (×норма)", "cap_usd": "Капитализация ($)",
    "up_pct": "Макс. рост (%)", "up_hours": "Часов до макс. роста",
    "dn_pct": "Макс. падение (%)", "dn_hours": "Часов до макс. падения",
    "move6_pct": "Движение за 6ч (%)", "move24_pct": "Движение за 24ч (%)",
    "move48_pct": "Движение за 48ч (%)",
    "h_to_3": "Часов до ±3%", "dir_3": "Куда ±3%",
    "h_to_5": "Часов до ±5%", "dir_5": "Куда ±5%",
    "h_to_10": "Часов до ±10%", "dir_10": "Куда ±10%",
    "complete": "Наблюдение завершено",
}


def klines15(symbol, start_s):
    """Все 15-минутные свечи монеты начиная с start_s."""
    out, cursor = [], start_s
    while True:
        raw = get_json(SPOT + "/api/v3/klines", symbol=symbol, interval="15m",
                       limit=1000, startTime=int(cursor) * 1000)
        if not raw:
            break
        out.extend(
            {"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "qv": float(k[7])}
            for k in raw
        )
        if len(raw) < 1000:
            break
        cursor = out[-1]["t"] + CANDLE_S
        time.sleep(0.2)
    return out


def outcomes(cs, i):
    """Что было за 48 часов после закрытия свечи i."""
    entry = cs[i]["c"]
    fut = cs[i + 1:i + 1 + HORIZON_C]
    if not fut or entry <= 0:
        return None
    up = dn = 0.0
    up_k = dn_k = 0
    first = {lv: None for lv in LEVELS}
    moves = {}
    for k, c in enumerate(fut):
        hi = (c["h"] / entry - 1) * 100
        lo = (c["l"] / entry - 1) * 100
        if hi > up:
            up, up_k = hi, k
        if lo < dn:
            dn, dn_k = lo, k
        for lv in LEVELS:
            if first[lv] is None and (hi >= lv or lo <= -lv):
                first[lv] = (k, "вверх" if hi >= -lo else "вниз")
        if k + 1 in (24, 96, 192):
            moves[k + 1] = max(up, -dn)
    h = lambda k: round((k + 1) * CANDLE_S / 3600, 2)  # свеча k закрывается через k+1 шагов
    out = {
        "up_pct": rnd(up), "up_hours": h(up_k), "dn_pct": rnd(dn), "dn_hours": h(dn_k),
        "move6_pct": rnd(moves.get(24, max(up, -dn))),
        "move24_pct": rnd(moves.get(96, max(up, -dn))),
        "move48_pct": rnd(moves.get(192, max(up, -dn))),
        "complete": "да" if len(fut) >= HORIZON_C else "нет",
    }
    for lv in LEVELS:
        out[f"h_to_{lv}"] = h(first[lv][0]) if first[lv] else ""
        out[f"dir_{lv}"] = first[lv][1] if first[lv] else ""
    return out


def make_row(kind, sym, base, cap, cs, i, mult, vol, norm, peak, out):
    c = cs[i]
    row = {
        "kind": kind, "coin": base, "symbol": sym,
        "time_utc": ts_str(c["t"]), "time_kyiv": kyiv_str(c["t"]), "candle_ts": c["t"],
        "price": sig(c["c"]),
        "candle_pct": rnd((c["c"] / c["o"] - 1) * 100) if c["o"] else "",
        "vol_usd": int(vol), "norm_usd": int(norm),
        "above_pct": rnd((mult - 1) * 100, 0), "above_x": rnd(mult, 1),
        "peak96_x": rnd(peak, 1), "cap_usd": int(cap),
    }
    row.update(out if out else {"complete": "нет"})
    return row


def scan(sym, info, cs, rng):
    """Находит сигналы и контрольные свечи у одной монеты."""
    events, control = [], []
    n = len(cs)
    if n < NORM_N + 10:
        return events, control
    window = sorted(c["qv"] for c in cs[:NORM_N])      # отсортированные объёмы предыдущих NORM_N свечей
    last_spike = -10 ** 9
    for i in range(NORM_N, n):
        norm = (window[NORM_N // 2 - 1] + window[NORM_N // 2]) / 2   # медиана (NORM_N чётное)
        v = cs[i]["qv"]
        if norm >= MIN_NORM_USD:
            mult = v / norm
            if mult >= MULT_MIN:
                if i - last_spike >= QUIET_C and v >= MIN_VOL_USD:
                    peak = max(c["qv"] for c in cs[i - QUIET_C:i]) / norm
                    events.append(make_row("сигнал", sym, info["base"], info["cap"], cs, i,
                                           mult, v, norm, peak, outcomes(cs, i)))
                last_spike = i
            elif (mult < 1.5 and i - last_spike >= QUIET_C
                  and i + HORIZON_C < n and rng.random() < CONTROL_P):
                peak = max(c["qv"] for c in cs[i - QUIET_C:i]) / norm
                control.append(make_row("контроль", sym, info["base"], info["cap"], cs, i,
                                        mult, v, norm, peak, outcomes(cs, i)))
        # окно сдвигаем: убираем самую старую свечу, добавляем текущую
        old = cs[i - NORM_N]["qv"]
        del window[bisect.bisect_left(window, old)]
        bisect.insort(window, v)
    return events, control


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([RU[c] for c in COLS])
        for r in rows:
            w.writerow([r.get(c, "") for c in COLS])


def group_stats(label, rows):
    done = [r for r in rows if r.get("complete") == "да"]
    if not done:
        return f"{label}: завершённых наблюдений нет"
    n = len(done)

    def reached(lv, hours):
        k = sum(1 for r in done if num(r.get(f"h_to_{lv}")) is not None
                and num(r[f"h_to_{lv}"]) <= hours)
        return f"{k / n * 100:.0f}%"

    lines = [f"{label}: {n} шт."]
    for lv in (3, 5, 10):
        lines.append(f"  ±{lv}% к 6 / 24 / 48 часам: "
                     f"{reached(lv, 6)} / {reached(lv, 24)} / {reached(lv, 48)}")
    hrs = [num(r["h_to_5"]) for r in done if num(r.get("h_to_5")) is not None]
    if hrs:
        lines.append(f"  медиана часов до ±5%: {statistics.median(hrs):.1f}")
    b5 = [r for r in done if num(r.get("h_to_5")) is not None]
    if b5:
        up = sum(1 for r in b5 if r.get("dir_5") == "вверх")
        lines.append(f"  первым на ±5%: вверх {up}, вниз {len(b5) - up}")
    mv = [num(r["move48_pct"]) for r in done if num(r.get("move48_pct")) is not None]
    if mv:
        lines.append(f"  медиана макс. движения за 48ч: {statistics.median(mv):.1f}%")
    return "\n".join(lines)


def main():
    start = int(datetime.strptime(HISTORY_START, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp())
    now = int(time.time())
    uni = {s: i for s, i in build_universe(CAP_MIN).items() if i["cap"] <= CAP_MAX}
    print("монет в списке:", len(uni), "история с", HISTORY_START)
    rng = random.Random(42)
    events, control = [], []
    t0 = time.time()
    for n_coin, (sym, info) in enumerate(uni.items(), 1):
        try:
            cs = [c for c in klines15(sym, start) if c["t"] + CANDLE_S <= now]
        except Exception as e:
            print("пропускаю", sym, e)
            continue
        ev, ct = scan(sym, info, cs, rng)
        events += ev
        control += ct
        if n_coin % 10 == 0:
            print(f"монет обработано: {n_coin}/{len(uni)}, сигналов: {len(events)}, "
                  f"прошло {int((time.time() - t0) / 60)} мин")
        if n_coin % 25 == 0:       # промежуточное сохранение
            write_csv(OUT_FILE, sorted(events + control, key=lambda r: (r["kind"], r["candle_ts"])))
        time.sleep(0.2)

    write_csv(OUT_FILE, sorted(events + control, key=lambda r: (r["kind"], r["candle_ts"])))
    days = max((now - start) / 86400, 1)
    lines = [
        f"📜 15-минутки: история {HISTORY_START} … {ts_str(now)[:10]}",
        f"Монет: {len(uni)} (капа $1–400 млн). Сигналов: {len(events)}, контрольных: {len(control)}.",
        "Сигнал = первая вспышка объёма ≥×3 после 24 часов без вспышек. Фильтра по размеру свечи нет.",
        "Движение считается в любую сторону за 48 часов после закрытия свечи.",
        "",
        group_stats("Контроль (обычные тихие свечи)", control),
    ]
    for t in (3, 5, 10):
        grp = [r for r in events if (num(r["above_x"]) or 0) >= t]
        lines += ["", group_stats(f"Вспышка от ×{t}", grp),
                  f"  сигналов в сутки по всему рынку: ≈{len(grp) / days:.1f}"]
    text = "\n".join(lines)
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    tg(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ОШИБКА:", repr(e))
        tg(f"⚠️ 15-минутный тест: ошибка\n{e}")
        raise
