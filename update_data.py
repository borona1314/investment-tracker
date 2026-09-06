"""
ดึงราคาปิดล่าสุดจาก Yahoo Finance (แหล่งข้อมูลราคาหุ้นมาตรฐาน ไม่ต้องใช้ API key)
คำนวณ MA50/MA200 และตรวจสัญญาณผิดปกติ แล้วเขียนผลลัพธ์เป็น JSON
ดึงข่าวประกอบจาก Google News RSS เฉพาะตอนพบสัญญาณ (ลดการเรียก API โดยไม่จำเป็น)
"""
import json
import os
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PORTFOLIO_PATH = os.path.join(BASE_DIR, "portfolio.json")
HISTORY_PATH = os.path.join(BASE_DIR, "price-history.json")
ALERTS_PATH = os.path.join(BASE_DIR, "alerts.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (investment-tracker script)"}

# ---- threshold settings (ปรับได้ตามต้องการ) ----
COST_DEVIATION_PCT = 0.10   # ราคาห่างจากต้นทุน +/- 10%
DAILY_MOVE_PCT = 0.05       # เปลี่ยนแปลง +/- 5% ในวันเดียว


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_yahoo_history(symbol, range_="1y", interval="1d"):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
        f"?range={range_}&interval={interval}"
    )
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    result = payload["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]

    series = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        series.append({"date": date, "close": round(close, 4)})
    return series


def fetch_news(query, max_items=3):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "th", "gl": "TH", "ceid": "TH:th"}
    )
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
    except Exception:
        return []

    items = []
    for item in root.findall(".//item")[:max_items]:
        title = item.findtext("title", default="")
        link = item.findtext("link", default="")
        pub_date = item.findtext("pubDate", default="")
        items.append({"title": title, "link": link, "date": pub_date})
    return items


def moving_average(series, window):
    if len(series) < window:
        return None
    closes = [p["close"] for p in series[-window:]]
    return sum(closes) / len(closes)


def pct_change(series, days_back):
    if len(series) <= days_back:
        return None
    latest = series[-1]["close"]
    past = series[-1 - days_back]["close"]
    if past == 0:
        return None
    return (latest - past) / past


def main():
    portfolio = load_json(PORTFOLIO_PATH, [])
    history = load_json(HISTORY_PATH, {})
    alerts = load_json(ALERTS_PATH, [])

    today = datetime.date.today().isoformat()
    new_alerts = []

    for holding in portfolio:
        symbol = holding.get("symbol")
        name = holding.get("name")
        cost = holding.get("cost")

        if not symbol:
            # กองทุนที่ไม่มี symbol อัตโนมัติ - ข้ามการดึงราคา
            continue

        try:
            series = fetch_yahoo_history(symbol)
        except Exception as e:
            print(f"[warn] ดึงราคา {symbol} ไม่สำเร็จ: {e}")
            continue

        if not series:
            continue

        history[symbol] = series
        latest_close = series[-1]["close"]

        reasons = []

        # 1) ห่างจากต้นทุนเกินเกณฑ์
        if cost:
            deviation = (latest_close - cost) / cost
            if abs(deviation) >= COST_DEVIATION_PCT:
                direction = "สูงกว่า" if deviation > 0 else "ต่ำกว่า"
                reasons.append(
                    f"ราคาปัจจุบัน {direction} ต้นทุน {abs(deviation)*100:.1f}%"
                )

        # 2) ตัดเส้นค่าเฉลี่ย 50/200 วัน
        ma50 = moving_average(series, 50)
        ma50_prev = moving_average(series[:-1], 50)
        if ma50 and ma50_prev:
            prev_close = series[-2]["close"] if len(series) >= 2 else None
            if prev_close is not None:
                if prev_close < ma50_prev and latest_close > ma50:
                    reasons.append("ราคาตัดขึ้นเหนือเส้นค่าเฉลี่ย 50 วัน")
                elif prev_close > ma50_prev and latest_close < ma50:
                    reasons.append("ราคาตัดลงใต้เส้นค่าเฉลี่ย 50 วัน")

        # 3) เปลี่ยนแปลงมากใน 1 วัน
        change_1d = pct_change(series, 1)
        if change_1d is not None and abs(change_1d) >= DAILY_MOVE_PCT:
            reasons.append(f"ราคาเปลี่ยนแปลง {change_1d*100:+.1f}% ในวันเดียว")

        if reasons:
            already_logged = any(
                a["symbol"] == symbol and a["date"] == today for a in alerts
            )
            if not already_logged:
                news = fetch_news(name)
                new_alerts.append(
                    {
                        "date": today,
                        "symbol": symbol,
                        "name": name,
                        "price": latest_close,
                        "reasons": reasons,
                        "news": news,
                    }
                )

    alerts.extend(new_alerts)
    # เก็บ alert ย้อนหลังไว้ 180 วันพอ ไม่ให้ไฟล์ใหญ่เกินไป
    cutoff = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    alerts = [a for a in alerts if a["date"] >= cutoff]

    save_json(HISTORY_PATH, history)
    save_json(ALERTS_PATH, alerts)

    print(f"อัปเดตราคาแล้ว {len(history)} รายการ, พบสัญญาณใหม่ {len(new_alerts)} รายการ")


if __name__ == "__main__":
    main()
