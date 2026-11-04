"""Точный порядок событий для 4-часовых сигналов: по 15-минутным свечам смотрим, что было раньше,
рост до +X% или падение до −Y%. Берёт сигналы из history_events.csv (основной фильтр: вспышка от ×3,
свеча от 4%, капа $1–400 млн), результат: path15_events.csv.
Запуск: python path15.py"""
import csv
import time

from bot import read_csv, get_json, kyiv_str, num, rnd, tg, SPOT, HISTORY_FILE

OUT = "path15_events.csv"
MULT_MIN, CANDLE_MIN, CAP_MIN, CAP_MAX = 3.0, 4.0, 1_000_000, 400_000_000
FOLLOW_C = 960                      # 10 суток по 15 минут
UP = (2, 3, 4, 5, 7, 10, 15)
DN = (2, 3, 5, 7, 10, 15, 20)

COLS = (["coin", "symbol", "time_kyiv", "candle_ts", "entry_price", "candle_pct", "above_x", "cap_usd"]
        + [f"up{x}_h" for x in UP] + [f"dn{x}_h" for x in DN] + ["up_pct", "dn_pct", "complete"])
RU = {"coin": "Монета", "symbol": "Пара", "time_kyiv": "Свеча (Киев)", "candle_ts": "Метка свечи",
      "entry_price": "Цена закрытия", "candle_pct": "Свеча (%)", "above_x": "Выше нормы (×)",
      "cap_usd": "Капитализация ($)", "up_pct": "Макс. рост 10 сут (%)", "dn_pct": "Макс. падение 10 сут (%)",
      "complete": "10 суток прошли"}
RU.update({f"up{x}_h": f"Часов до +{x}%" for x in UP})
RU.update({f"dn{x}_h": f"Часов до −{x}%" for x in DN})


def fetch(symbol, entry_ts):
    raw = get_json(SPOT + "/api/v3/klines", symbol=symbol, interval="15m",
                   limit=FOLLOW_C, startTime=int(entry_ts) * 1000)
    return [{"t": int(k[0]) // 1000, "h": float(k[2]), "l": float(k[3])} for k in raw]


def analyze(row, cs, now):
    entry = num(row["price"])
    entry_ts = int(num(row["candle_ts"])) + 4 * 3600
    cs = [c for c in cs if c["t"] + 900 <= now]
    out = {"coin": row["coin"], "symbol": row["symbol"], "time_kyiv": kyiv_str(int(num(row["candle_ts"]))),
           "candle_ts": int(num(row["candle_ts"])), "entry_price": row["price"],
           "candle_pct": row.get("candle_pct", ""), "above_x": row.get("above_x", ""),
           "cap_usd": row.get("cap_usd", "")}
    if not cs or not entry:
        out["complete"] = "нет"
        return out
    up = dn = 0.0
    tu, td = {}, {}
    for c in cs:
        hi, lo = (c["h"] / entry - 1) * 100, (c["l"] / entry - 1) * 100
        up, dn = max(up, hi), min(dn, lo)
        for x in UP:
            if x not in tu and hi >= x:
                tu[x] = c["t"] + 900
        for x in DN:
            if x not in td and lo <= -x:
                td[x] = c["t"] + 900
    for x in UP:
        out[f"up{x}_h"] = round((tu[x] - entry_ts) / 3600, 2) if x in tu else ""
    for x in DN:
        out[f"dn{x}_h"] = round((td[x] - entry_ts) / 3600, 2) if x in td else ""
    out["up_pct"], out["dn_pct"] = rnd(up), rnd(dn)
    out["complete"] = "да" if len(cs) >= FOLLOW_C else "нет"
    return out


def main():
    now = int(time.time())
    rows = [r for r in read_csv(HISTORY_FILE)
            if r.get("kind") == "сигнал" and (num(r.get("above_x")) or 0) >= MULT_MIN
            and abs(num(r.get("candle_pct")) or 0) >= CANDLE_MIN
            and CAP_MIN <= (num(r.get("cap_usd")) or 0) < CAP_MAX]
    print("сигналов для проверки:", len(rows))
    res = []
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        entry_ts = int(num(r["candle_ts"])) + 4 * 3600
        if entry_ts + 900 > now:
            continue
        try:
            res.append(analyze(r, fetch(r["symbol"], entry_ts), now))
        except Exception as e:
            print("пропускаю", r.get("symbol"), e)
        if i % 100 == 0:
            print(f"{i}/{len(rows)}, прошло {int((time.time() - t0) / 60)} мин")
        time.sleep(0.2)
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([RU[c] for c in COLS])
        for r in res:
            w.writerow([r.get(c, "") for c in COLS])
    msg = f"✅ path15: готово, {len(res)} сигналов, файл {OUT}"
    print(msg)
    tg(msg)


if __name__ == "__main__":
    main()
