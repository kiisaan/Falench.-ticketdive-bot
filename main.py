from datetime import datetime
import json
import os
import re
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://ticketdive.com/artist/falench"
DATA_FILE = "seen_events.json"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_GROUP_ID = os.getenv("LINE_GROUP_ID")


def clean_title(raw_title):
    """タイトルから不要な接尾辞や画面上の汎用文字列を除去"""
    if not raw_title:
        return ""

    title = re.sub(r"[\r\n]+", " ", str(raw_title)).strip()

    ng_words = [
        "お気に入り",
        "シェア",
        "チケットの分配",
        "チケット分配",
        "販売情報",
        "チケット情報",
        "TicketDive",
        "ログイン",
        "マイページ",
        "新規会員登録",
        "チケット購入",
        "名前",
        "氏名",
    ]
    for ng in ng_words:
        if title == ng:
            return ""
        title = re.sub(re.escape(ng), "", title, flags=re.IGNORECASE).strip()

    title = title.split("｜")[0].split(" - ")[0].strip()
    title = re.split(
        r"(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])", title
    )[0].strip()
    title = re.sub(r"^[\s:：\-–|]+|[\s\-/／:：]+$", "", title).strip()

    return title


def parse_datetime_value(val):
    """ISO文字列またはUnixタイムスタンプ(数値/文字列)から (日付, 時刻) を抽出"""
    if not val:
        return None, None

    # エスケープ文字を除去
    clean_val = str(val).replace("\\", "").replace('"', "").strip()

    # 1. 数値（ミリ秒タイムスタンプ）の場合
    if clean_val.isdigit():
        try:
            ts = float(clean_val)
            if ts > 10000000000:  # ミリ秒判定
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts)
            weekdays = ["月", "火", "水", "木", "金", "土", "日"]
            return (
                f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）",
                f"{dt.hour:02d}:{dt.minute:02d}",
            )
        except Exception:
            pass

    # 2. ISO 8601 文字列の場合
    try:
        iso_str = clean_val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        return (
            f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）",
            f"{dt.hour:02d}:{dt.minute:02d}",
        )
    except Exception:
        pass

    return None, None


def extract_dynamic_fields(html_content):
    """HTML / Next.jsデータ全域から正規表現パターンで各種フィールドを検出"""
    extracted = {
        "event_date": None,
        "open_time": None,
        "start_time": None,
        "sales_periods": [],
    }

    # --- A. 開場時間（open_at, openAt, open_time, openTime など） ---
    open_patterns = [
        r'\\?["\'](?:open_at|openAt|open_time|openTime)\\?["\']\s*:\s*\\?["\']?([^"\'\\,{}]+)\\?["\']?',
        r'(?:開場|OPEN)[\s:：]*(\d{1,2}:\d{2})',
    ]
    for pat in open_patterns:
        m = re.search(pat, html_content, re.IGNORECASE)
        if m:
            raw_val = m.group(1).strip()
            d_str, t_str = parse_datetime_value(raw_val)
            if d_str and not extracted["event_date"]:
                extracted["event_date"] = d_str
            if t_str:
                extracted["open_time"] = t_str
            elif re.match(r"^\d{1,2}:\d{2}$", raw_val):
                extracted["open_time"] = raw_val
            if extracted["open_time"]:
                break

    # --- B. 開演時間（start_at, startAt, start_time, startTime など） ---
    start_patterns = [
        r'\\?["\'](?:start_at|startAt|start_time|startTime)\\?["\']\s*:\s*\\?["\']?([^"\'\\,{}]+)\\?["\']?',
        r'(?:開演|START)[\s:：]*(\d{1,2}:\d{2})',
    ]
    for pat in start_patterns:
        m = re.search(pat, html_content, re.IGNORECASE)
        if m:
            raw_val = m.group(1).strip()
            d_str, t_str = parse_datetime_value(raw_val)
            if d_str and not extracted["event_date"]:
                extracted["event_date"] = d_str
            if t_str:
                extracted["start_time"] = t_str
            elif re.match(r"^\d{1,2}:\d{2}$", raw_val):
                extracted["start_time"] = raw_val
            if extracted["start_time"]:
                break

    # --- C. 販売期間（sales_start_at / sales_end_at のペア抽出） ---
    sales_patterns = [
        # スネークケース / キャメルケースペア
        r'\\?["\'](?:sales_start_at|salesStartAt|sales_start|salesStart)\\?["\']\s*:\s*\\?["\']?([^"\'\\,{}]+)\\?["\']?.*?\\?["\'](?:sales_end_at|salesEndAt|sales_end|salesEnd)\\?["\']\s*:\s*\\?["\']?([^"\'\\,{}]+)\\?["\']?',
        # テキスト表現（例: 09/20(日)10:00 ～ 09/27(日)10:30）
        r'(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2}\s*～\s*\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})',
    ]

    for pat in sales_patterns:
        matches = re.findall(pat, html_content, re.DOTALL | re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple) and len(match) == 2:
                s_start, s_end = match
                d1, t1 = parse_datetime_value(s_start)
                d2, t2 = parse_datetime_value(s_end)
                if d1 and t1 and d2 and t2:
                    period = f"{d1}{t1} ～ {d2}{t2}"
                    if period not in extracted["sales_periods"]:
                        extracted["sales_periods"].append(period)
            elif isinstance(match, str) and match.strip():
                if match.strip() not in extracted["sales_periods"]:
                    extracted["sales_periods"].append(match.strip())

    return extracted


def fetch_event_details(event_url, headers):
    """個別イベントページの取得と解析"""
    try:
        res = requests.get(event_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None

        html = res.text
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(separator=" ", strip=True)

        extracted = {
            "title": "イベント名称未設定",
            "event_date": "情報なし",
            "open_time": "情報なし",
            "start_time": "情報なし",
            "sales_periods": [],
        }

        # 1. og:title メタタグからタイトルを取得
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            cand_og = clean_title(og_title["content"])
            if cand_og:
                extracted["title"] = cand_og

        # 2. HTMLおよびデータ構造から時刻・日付・販売期間を解析
        dynamic_res = extract_dynamic_fields(html)

        if dynamic_res["event_date"]:
            extracted["event_date"] = dynamic_res["event_date"]
        if dynamic_res["open_time"]:
            extracted["open_time"] = dynamic_res["open_time"]
        if dynamic_res["start_time"]:
            extracted["start_time"] = dynamic_res["start_time"]
        if dynamic_res["sales_periods"]:
            extracted["sales_periods"] = dynamic_res["sales_periods"]

        # 3. テキスト全域からの最終フォールバック
        if extracted["event_date"] == "情報なし":
            date_m = re.search(
                r"(\d{1,2}/\d{1,2}\s*[\(（][月火水木金土日祝][\)）])", page_text
            )
            if date_m:
                extracted["event_date"] = date_m.group(1).strip()

        if not extracted["sales_periods"]:
            extracted["sales_periods"] = ["公式ページをご確認ください"]

        extracted["url"] = event_url
        return extracted

    except Exception as e:
        print(f"詳細取得エラー ({event_url}): {e}")
        return None


def fetch_events():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    response = requests.get(TARGET_URL, headers=headers)
    if response.status_code != 200:
        print(f"Error: {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    events = []

    links = soup.find_all("a", href=True)
    for link in links:
        href = link["href"]
        if "/events/" in href or "/event/" in href:
            full_url = (
                href if href.startswith("http") else f"https://ticketdive.com{href}"
            )
            if full_url not in [e["url"] for e in events]:
                details = fetch_event_details(full_url, headers)
                if details:
                    events.append(details)

    return events


def load_seen_events():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_events(seen_set):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_set), f, ensure_ascii=False, indent=2)


def send_line_message(message):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_GROUP_ID,
        "messages": [{"type": "text", "text": message}],
    }
    res = requests.post(url, headers=headers, json=payload)
    if res.status_code != 200:
        print(f"【LINE送信エラー】ステータス: {res.status_code}, 内容: {res.text}")
        raise Exception("LINE送信失敗")


def main():
    events = fetch_events()
    seen = load_seen_events()

    new_events = []
    for ev in events:
        if ev["url"] not in seen:
            new_events.append(ev)
            seen.add(ev["url"])

    if new_events:
        print(
            f"{len(new_events)} 件の新着イベントを検知しました。LINEに通知します。"
        )

        msg_blocks = []
        for ev in new_events:
            for sales in ev["sales_periods"]:
                block_text = (
                    f"イベント名：{ev['title']}\n"
                    f"販売期間：{sales}\n"
                    f"公演日：{ev['event_date']}\n"
                    f"開場時刻：{ev['open_time']}\n"
                    f"開演時刻：{ev['start_time']}\n"
                    f"URL：{ev['url']}"
                )
                msg_blocks.append(block_text)

        chunk_size = 2
        for i in range(0, len(msg_blocks), chunk_size):
            chunk = msg_blocks[i : i + chunk_size]
            msg = "【Falench.ライブ情報（ダイブ）】\n\n"
            msg += "\n\n──────────────────\n\n".join(chunk)
            send_line_message(msg)

        save_seen_events(seen)
    else:
        print("新着イベントはありませんでした。")


if __name__ == "__main__":
    main()
