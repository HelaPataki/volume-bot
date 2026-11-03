"""Проба Coinalyze: как далеко в прошлое отдаёт 15-минутные (и другие) свечи фьючерсов.
Запуск: python probe.py   Результат: probe_result.txt и сообщение в Telegram."""
import time
from datetime import datetime, timezone, timedelta

from bot import cz, tg, kyiv_str

SYMS = ["BTCUSDT_PERP.A", "ETHUSDT_PERP.A", "WIFUSDT_PERP.A"]
INTERVALS = ["15min", "1hour", "4hour"]
START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def fetch(sym, interval, t_from, t_to):
    data = cz("/ohlcv-history", symbols=sym, interval=interval,
              **{"from": int(t_from), "to": int(t_to)})
    return data[0].get("history", []) if data else []


def main():
    now = datetime.now(timezone.utc)
    lines = ["🔬 Проба Coinalyze: как далеко есть история", ""]
    for sym in SYMS:
        lines.append(sym)
        for iv in INTERVALS:
            try:
                h = fetch(sym, iv, START.timestamp(), now.timestamp())
                time.sleep(1.6)
                if h:
                    first, last = min(x["t"] for x in h), max(x["t"] for x in h)
                    lines.append(f"  {iv}: одним запросом с 2025 пришло {len(h)} свечей, "
                                 f"с {kyiv_str(first)} по {kyiv_str(last)} (Киев)")
                else:
                    lines.append(f"  {iv}: пусто")
            except Exception as e:
                lines.append(f"  {iv}: ошибка {str(e)[:120]}")
        # помесячно, чтобы понять, откуда данные реально есть (только 15 минут)
        have = []
        cur = START
        while cur < now:
            nxt = (cur + timedelta(days=32)).replace(day=1)
            try:
                h = fetch(sym, "15min", cur.timestamp(), min(nxt, now).timestamp())
                have.append(f"{cur:%Y-%m}:{len(h)}")
            except Exception as e:
                have.append(f"{cur:%Y-%m}:ошибка")
            time.sleep(1.6)
            cur = nxt
        lines.append("  15min по месяцам (число свечей): " + ", ".join(have))
        lines.append("")
    text = "\n".join(lines)
    with open("probe_result.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    tg(text)


if __name__ == "__main__":
    main()
