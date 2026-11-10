"""Добавляет к историческим сигналам (mid15_events.csv) данные о фьючерсах и рынке на момент закрытия сигнальной свечи:
ОИ (сейчас, изменение за 4ч / 24ч / 7 суток, ОИ к капе), фандинг (последний и средний за 24ч),
соотношение лонг/шорт (и его изменение за 24ч), ликвидации лонгов и шортов (за свечу и за 24ч),
движение BTC (4ч, 24ч, 7 суток, цена к средней за 30 суток).
Coinalyze хранит 4-часовые данные примерно за 11 месяцев, поэтому фьючерсные колонки будут только у сигналов с ~декабря 2025.
BTC считается для всех сигналов. Бота не трогает. Запуск: python extra.py   Результат: extra_events.csv"""
import csv
import time

from bot import CANDLE_S, SPOT, STABLES, cz, get_json, num, rnd, tg

SRC = "mid15_events.csv"
OUT = "extra_events.csv"
DAYS_BACK = 330
BATCH = 10


def read_src():
    with open(SRC, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def fut_map():
    m = {}
    for x in cz("/future-markets"):
        s = x.get("symbol", "")
        if not s.endswith(".A") or not x.get("is_perpetual") or x.get("quote_asset") != "USDT":
            continue
        b = x.get("base_asset", "").upper()
        if b in STABLES:
            continue
        m.setdefault(b, s)
        if b.startswith("1000"):
            m.setdefault(b[4:], s)
    return m


def fetch(endpoint, syms, t_from, t_to, add=None):
    out = {}
    for k in range(0, len(syms), BATCH):
        params = {"symbols": ",".join(syms[k:k + BATCH]), "interval": "4hour", "from": int(t_from), "to": int(t_to)}
        if add:
            params.update(add)
        for attempt in range(3):
            try:
                for item in cz("/" + endpoint, **params) or []:
                    out[item["symbol"]] = item.get("history", [])
                break
            except Exception as e:
                print("ошибка", endpoint, str(e)[:120])
                time.sleep(5 * (attempt + 1))
        time.sleep(1.6)
    print(endpoint, "получено монет:", sum(1 for v in out.values() if v))
    return out


def at(h, T, key="c"):
    """Значение 4-часовой свечи, закрывшейся ровно к моменту T (или раньше)."""
    ok = [x for x in h if x["t"] + CANDLE_S <= T and x.get(key) is not None]
    return float(max(ok, key=lambda x: x["t"])[key]) if ok else None


def btc_klines(t_from, t_to):
    out, cur = [], t_from
    while cur < t_to:
        raw = get_json(SPOT + "/api/v3/klines", symbol="BTCUSDT", interval="4h", limit=1000, startTime=int(cur) * 1000)
        if not raw:
            break
        out += [{"t": int(k[0]) // 1000, "o": float(k[1]), "c": float(k[4])} for k in raw]
        cur = out[-1]["t"] + CANDLE_S
        if len(raw) < 1000:
            break
        time.sleep(0.2)
    return {c["t"]: c for c in out}


def pct(a, b):
    return rnd((a / b - 1) * 100) if a is not None and b else ""


COLS = ["Монета", "Пара", "Свеча (Киев)", "Метка свечи", "Фьючерс", "ОИ ($)", "ОИ / капа (%)", "ОИ за 4ч (%)", "ОИ за 24ч (%)",
        "ОИ за 7 сут (%)", "Фандинг (%)", "Фандинг ср. 24ч (%)", "Лонг/шорт", "Лонг/шорт за 24ч (изм.)",
        "Ликвид. лонгов свеча ($)", "Ликвид. шортов свеча ($)", "Ликвид. лонгов 24ч ($)", "Ликвид. шортов 24ч ($)",
        "BTC 4ч (%)", "BTC 24ч (%)", "BTC 7 сут (%)", "BTC к средней 30 сут (%)"]


def main():
    now = int(time.time())
    rows = read_src()
    print("сигналов в файле:", len(rows))
    fm = fut_map()
    t_min = now - DAYS_BACK * 86400
    for r in rows:
        base = r["Монета"].upper()
        r["_f"] = fm.get(base) or fm.get("1000" + base) or ""
        r["_T"] = int(num(r["Метка свечи"])) + CANDLE_S
    syms = sorted({r["_f"] for r in rows if r["_f"] and r["_T"] > t_min})
    print("фьючерсов для запроса:", len(syms))
    oi = fetch("open-interest-history", syms, t_min - 8 * 86400, now, {"convert_to_usd": "true"})
    fund = fetch("funding-rate-history", syms, t_min - 2 * 86400, now)
    ls = fetch("long-short-ratio-history", syms, t_min - 2 * 86400, now)
    liq = fetch("liquidation-history", syms, t_min - 2 * 86400, now, {"convert_to_usd": "true"})
    t0 = min(r["_T"] for r in rows) - 40 * 86400
    btc = btc_klines(t0, now)
    print("BTC свечей:", len(btc))

    out = []
    for r in rows:
        T, f = r["_T"], r["_f"]
        o = {"Монета": r["Монета"], "Пара": r.get("Пара", ""), "Свеча (Киев)": r.get("Свеча (Киев)", ""),
             "Метка свечи": r["Метка свечи"], "Фьючерс": f}
        try:
            if f:
                h = oi.get(f, [])
                v = at(h, T)
                if v:
                    o["ОИ ($)"] = int(v)
                    cap = num(r.get("Капитализация ($)"))
                    o["ОИ / капа (%)"] = rnd(v / cap * 100) if cap else ""
                    o["ОИ за 4ч (%)"] = pct(v, at(h, T - CANDLE_S))
                    o["ОИ за 24ч (%)"] = pct(v, at(h, T - 6 * CANDLE_S))
                    o["ОИ за 7 сут (%)"] = pct(v, at(h, T - 42 * CANDLE_S))
                fh = fund.get(f, [])
                fv = at(fh, T)
                if fv is not None:
                    o["Фандинг (%)"] = round(fv, 5)
                    last6 = [float(x["c"]) for x in fh if T - 6 * CANDLE_S < x["t"] + CANDLE_S <= T and x.get("c") is not None]
                    if last6:
                        o["Фандинг ср. 24ч (%)"] = round(sum(last6) / len(last6), 5)
                lh = ls.get(f, [])
                lv = at(lh, T, "r")
                if lv is not None:
                    o["Лонг/шорт"] = rnd(lv, 3)
                    l24 = at(lh, T - 6 * CANDLE_S, "r")
                    if l24 is not None:
                        o["Лонг/шорт за 24ч (изм.)"] = rnd(lv - l24, 3)
                qh = liq.get(f, [])
                c1 = [x for x in qh if x["t"] + CANDLE_S == T]
                c6 = [x for x in qh if T - 6 * CANDLE_S < x["t"] + CANDLE_S <= T]
                if c6:
                    o["Ликвид. лонгов свеча ($)"] = int(sum(x.get("l") or 0 for x in c1))
                    o["Ликвид. шортов свеча ($)"] = int(sum(x.get("s") or 0 for x in c1))
                    o["Ликвид. лонгов 24ч ($)"] = int(sum(x.get("l") or 0 for x in c6))
                    o["Ликвид. шортов 24ч ($)"] = int(sum(x.get("s") or 0 for x in c6))
            b = btc.get(T - CANDLE_S)
            if b:
                o["BTC 4ч (%)"] = pct(b["c"], b["o"])
                b6 = btc.get(T - 6 * CANDLE_S)
                b42 = btc.get(T - 42 * CANDLE_S)
                o["BTC 24ч (%)"] = pct(b["c"], b6["o"]) if b6 else ""
                o["BTC 7 сут (%)"] = pct(b["c"], b42["o"]) if b42 else ""
                last = [btc[t]["c"] for t in range(T - 180 * CANDLE_S, T, CANDLE_S) if t in btc]
                if len(last) > 150:
                    o["BTC к средней 30 сут (%)"] = pct(b["c"], sum(last) / len(last))
        except Exception as e:
            print("пропуск", r["Монета"], e)
        out.append(o)

    with open(OUT, "w", newline="", encoding="utf-8-sig") as fo:
        w = csv.DictWriter(fo, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    n_oi = sum(1 for o in out if o.get("ОИ ($)"))
    msg = f"✅ extra: готово, сигналов {len(out)}, с данными ОИ {n_oi}, файл {OUT}"
    print(msg)
    tg(msg)


if __name__ == "__main__":
    main()
