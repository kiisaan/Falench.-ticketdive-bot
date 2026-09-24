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
    """タイトルから不要な接尾辞や画面上の操作ボタン名を除去"""
    if not raw_title:
        return ""

    title = re.sub(r"[\r\n]+", " ", str(raw_title)).strip()

    # UIの操作ボタンや共通文字列を徹底除去
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
    ]
    for ng in ng_words:
        title = re.sub(re.escape(ng), "", title, flags=re.IGNORECASE).strip()

    title = title.split("｜")[0].split(" - ")[0].strip()
    title = re.split(
        r"(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])", title
    )[0].strip()
    title = re.sub(r"^[\s:：\-–|]+|[\s\-/／:：]+$", "", title).strip()

    return title


def format_dt(dt_input):
    """ISO表記やタイムスタンプを「MM/DD（週）HH:MM」形式に統一変換"""
    if not dt_input:
        return None

    try:
        if isinstance(dt_input, (int, float)):
            dt = datetime.fromtimestamp(dt_input / 1000.0)
            weekdays = ["月", "火", "水", "木", "金", "土", "日"]
            return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"

        dt_str = str(dt_input).strip()
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]

        # ISO 8601 (例: 2026-10-11T10:00:00.000Z)
        clean_iso = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        pass

    return str(dt_input)


def parse_app_router_data(html_content):
    """Next.js App Router (self.__next_f.push) から直接イベント属性を正規表現抽出"""
    extracted = {
        "title": None,
        "event_date": None,
        "open_time": None,
        "start_time": None,
        "sales_periods": [],
    }

    # 1. イベント名の抽出
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', html_content)
    if name_match:
        cand_name = clean_title(name_match.group(1))
        if cand_name:
            extracted["title"] = cand_name

    # 2. 開場・開演時刻の抽出
    open_match = re.search(r'"openAt"\s*:\s*"([^"]+)"', html_content)
    if open_match:
        extracted["open_time"] = format_dt(open_match.group(1))

    start_match = re.search(r'"startAt"\s*:\s*"([^"]+)"', html_content)
    if start_match:
        extracted["start_time"] = format_dt(start_match.group(1))

    # 3. 公演日の抽出
    date_match = re.search(
        r'"(?:eventDate|performanceDate|date)"\s*:\s*"([^"]+)"', html_content
    )
    if date_match:
        extracted["event_date"] = format_dt(date_match.group(1))
    elif extracted["start_time"] and "（" in extracted["start_time"]:
        extracted["event_date"] = (
            extracted["start_time"].split("）")[0] + "）"
        )

    # 4. チケット販売期間の抽出 (salesStartAt ~ salesEndAt)
    sales_matches = re.findall(
        r'{(?:[^{}]*?"name"\s*:\s*"([^"]+)")?[^{}]*?"salesStartAt"\s*:\s*"([^"]+)"[^{}]*?"salesEndAt"\s*:\s*"([^"]+)"[^{}]*?}',
        html_content,
    )
    for t_name, s_start, s_end in sales_matches:
        fmt_s = format_dt(s_start)
        fmt_e = format_dt(s_end)
        prefix = f"【{t_name}】" if t_name else ""
        period = f"{prefix}{fmt_s} ～ {fmt_e}"
        if period not in extracted["sales_periods"]:
            extracted["sales_periods"].append(period)

    # 万が一チケット名が後ろに来る順序パターンのレスポンス対策
    if not extracted["sales_periods"]:
        sales_simple = re.findall(
            r'"salesStartAt"\s*:\s*"([^"]+)".*?"salesEndAt"\s*:\s*"([^"]+)"',
            html_content,
        )
        for s_start, s_end in sales_simple:
            fmt_s = format_dt(s_start)
            fmt_e = format_dt(s_end)
            period = f"{fmt_s} ～ {fmt_e}"
            if period not in extracted["sales_periods"]:
                extracted["sales_periods"].append(period)

    return extracted


def fetch_event_details(event_url, headers):
    """個別イベントページの取得と多角解析"""
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

        # 1. Next.js App Routerのストリーミングデータを直接解析
        app_res = parse_app_router_data(html)

        if app_res["title"]:
            extracted["title"] = app_res["title"]
        if app_res["event_date"]:
            extracted["event_date"] = app_res["event_date"]
        if app_res["open_time"]:
            extracted["open_time"] = app_res["open_time"]
        if app_res["start_time"]:
            extracted["start_time"] = app_res["start_time"]
        if app_res["sales_periods"]:
            extracted["sales_periods"] = app_res["sales_periods"]

        # 2. メタタグ（og:title）からのタイトルフォールバック
        if (
            extracted["title"] == "イベント名称未設定"
            or extracted["title"] == ""
        ):
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                extracted["title"] = clean_title(og_title["content"])

        # 3. テキストからの日時・時刻正規表現フォールバック
        if extracted["open_time"] == "情報なし":
            open_m = re.search(
                r"(?:開場|OPEN)[\s:：]*(\d{1,2}:\d{2})", page_text, re.IGNORECASE
            )
            if open_m:
                extracted["open_time"] = open_m.group(1).strip()

        if extracted["start_time"] == "情報なし":
            start_m = re.search(
                r"(?:開演|START)[\s:：]*(\d{1,2}:\d{2})", page_text, re.IGNORECASE
            )
            if start_m:
                extracted["start_time"] = start_m.group(1).strip()

        if not extracted["sales_periods"]:
            sales_m = re.findall(
                r"(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2}\s*～\s*\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})",
                page_text,
            )
            if sales_m:
                extracted["sales_periods"] = list(set(sales_m))
            else:
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
