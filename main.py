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
    """イベント名から過度な文字削りを防ぎ、明確な出演者ラベル以降のみカットする"""
    if not raw_title:
        return "イベント名称未設定"

    # 改行や連続スペースの整理
    title = re.sub(r"[\r\n]+", " ", raw_title).strip()

    # 「/」での分割を廃止し、明確な出演者指定ラベルのみで分割
    title = re.split(
        r"(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])", title
    )[0].strip()

    # 末尾に残った不要な記号を削除
    title = re.sub(r"[\s\-/／]+$", "", title)

    return title if title else "イベント名称未設定"


def parse_datetime_str(dt_str):
    """09/25（金）20:00 形式の文字列を datetime に変換（年は現在年）"""
    try:
        clean_str = re.sub(r"[\(（].+?[\)）]", "", dt_str).strip()
        now = datetime.now()
        dt = datetime.strptime(clean_str, "%m/%d %H:%M")
        return dt.replace(year=now.year)
    except Exception:
        return None


def is_currently_open(sales_str):
    """販売期間内に『現在日時』が含まれているか判定"""
    now = datetime.now()

    # 開始日時 〜 終了日時
    match = re.search(
        r"(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})\s*～\s*(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})",
        sales_str,
    )
    if match:
        start_dt = parse_datetime_str(match.group(1))
        end_dt = parse_datetime_str(match.group(2))
        if start_dt and end_dt:
            return start_dt <= now <= end_dt

    # 開始日時 〜
    match_start = re.search(
        r"(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})\s*～", sales_str
    )
    if match_start:
        start_dt = parse_datetime_str(match_start.group(1))
        if start_dt:
            return now >= start_dt

    return False


def parse_sales_info(soup, page_text):
    """TicketDiveの構造に合わせた販売期間取得処理"""
    sales_list = []

    # 1. ページ内の全テキストから「日時 〜 日時」パターンを広く抽出
    matches = re.findall(
        r"(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2}\s*～\s*(?:\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})?)",
        page_text,
    )

    for m in matches:
        info = m.strip()
        # 公演日などの開場・開演時刻と誤認しないよう「～」を含むものに限定
        if "～" in info:
            if is_currently_open(info) and "（申込受付中）" not in info:
                info += "（申込受付中）"
            sales_list.append(info)

    # 重複の削除
    unique_sales = []
    for s in sales_list:
        if s not in unique_sales:
            unique_sales.append(s)

    return unique_sales if unique_sales else ["詳細ページをご確認ください"]


def fetch_event_details(event_url, headers):
    """個別イベントページから正確に各項目をスクレイピング"""
    try:
        res = requests.get(event_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        page_text = soup.get_text(separator=" ", strip=True)

        # 1. イベント名の取得（h1 や meta から安全に抽出）
        raw_title = ""
        h1_el = soup.find("h1")
        if h1_el:
            raw_title = h1_el.get_text(strip=True)

        if not raw_title:
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                raw_title = og_title["content"].split("｜")[0].split(" - ")[0].strip()

        title = clean_title(raw_title)

        # 2. 公演日（例: 10/10（土） または 2026/10/10）
        date_str = "詳細ページをご確認ください"
        date_match = re.search(
            r"(?:日程|開催日|公演日|DATE)[\s:：]*(\d{1,2}/\d{1,2}\s*[\(（].+?[\)）]|\d{4}/\d{1,2}/\d{1,2})",
            page_text,
            re.IGNORECASE,
        )
        if date_match:
            date_str = date_match.group(1).strip()
        else:
            gen_date = re.search(
                r"(\d{1,2}/\d{1,2}\s*[\(（][月火水木金土日祝][\)）])", page_text
            )
            if gen_date:
                date_str = gen_date.group(1).strip()

        # 3. 開場・開演時刻
        open_time = "情報なし"
        start_time = "情報なし"

        open_match = re.search(
            r"(?:開場|OPEN)[\s:：]*(\d{1,2}:\d{2})", page_text, re.IGNORECASE
        )
        if open_match:
            open_time = open_match.group(1).strip()

        start_match = re.search(
            r"(?:開演|START)[\s:：]*(\d{1,2}:\d{2})", page_text, re.IGNORECASE
        )
        if start_match:
            start_time = start_match.group(1).strip()

        # 4. 販売期間
        sales_periods = parse_sales_info(soup, page_text)

        return {
            "title": title,
            "event_date": date_str,
            "open_time": open_time,
            "start_time": start_time,
            "sales_periods": sales_periods,
            "url": event_url,
        }

    except Exception as e:
        print(f"詳細取得エラー ({event_url}): {e}")
        return None


def fetch_events():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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
        print(f"【LINE送信エラー詳細】ステータスコード: {res.status_code}")
        print(f"レスポンス内容: {res.text}")
        raise Exception("LINEメッセージの送信に失敗しました")


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
            f"{len(new_events)} 件の新着イベントを検知しました。LINEに送信します。"
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
