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
    """タイトルから不要な接尾辞を除去"""
    if not raw_title:
        return "イベント名称未設定"

    title = re.sub(r"[\r\n]+", " ", str(raw_title)).strip()
    title = re.sub(
        r"[\s:：\-–|]*?(?:販売情報|チケット情報|TicketDive).*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    title = re.split(
        r"(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])", title
    )[0].strip()
    title = re.sub(r"[\s\-/／:：]+$", "", title).strip()

    return title if title else "イベント名称未設定"


def format_dt(dt_input):
    """様々な日付フォーマット（ISO8601、Timestamp等）を「MM/DD（週）HH:MM」形式に統一変換"""
    if not dt_input:
        return None

    dt_str = str(dt_input).strip()
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]

    # 1. ISO 8601 (例: 2026-09-25T18:00:00.000Z)
    try:
        clean_iso = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        pass

    # 2. YYYY-MM-DD HH:MM:SS または YYYY/MM/DD HH:MM
    match = re.search(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s+|T)(\d{1,2}):(\d{2})", dt_str
    )
    if match:
        y, m, d, hh, mm = map(int, match.groups())
        dt = datetime(y, m, d, hh, mm)
        return f"{m:02d}/{d:02d}（{weekdays[dt.weekday()]}）{hh:02d}:{mm:02d}"

    # 3. YYYY-MM-DD または YYYY/MM/DD（時刻なし）
    match_date = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", dt_str)
    if match_date:
        y, m, d = map(int, match_date.groups())
        dt = datetime(y, m, d)
        return f"{m:02d}/{d:02d}（{weekdays[dt.weekday()]}）"

    return dt_str


def deep_search_json(data):
    """Next.js / tRPCの動的状態オブジェクトからキー・値を再帰的に全探索"""
    extracted = {
        "title": None,
        "event_date": None,
        "open_time": None,
        "start_time": None,
        "sales_periods": [],
    }

    def walk(obj):
        if isinstance(obj, dict):
            # イベント名候補
            if not extracted["title"]:
                for t_key in ["title", "eventName", "name"]:
                    if t_key in obj and isinstance(obj[t_key], str):
                        t_val = obj[t_key].strip()
                        if (
                            t_val
                            and "ticketdive" not in t_val.lower()
                            and t_val != "販売情報"
                        ):
                            extracted["title"] = clean_title(t_val)
                            break

            # 開場・開演時刻候補
            if not extracted["open_time"] and "openAt" in obj:
                extracted["open_time"] = format_dt(obj["openAt"])
            if not extracted["start_time"] and "startAt" in obj:
                extracted["start_time"] = format_dt(obj["startAt"])

            # 公演日候補
            if not extracted["event_date"]:
                for d_key in ["eventDate", "date", "performanceDate"]:
                    if d_key in obj and obj[d_key]:
                        extracted["event_date"] = format_dt(obj[d_key])
                        break

            # 販売期間候補
            s_start = (
                obj.get("salesStartAt")
                or obj.get("sales_start_at")
                or obj.get("startAt")
            )
            s_end = (
                obj.get("salesEndAt")
                or obj.get("sales_end_at")
                or obj.get("endAt")
            )
            if (
                s_start
                and s_end
                and ("sales" in str(obj).lower() or "ticket" in str(obj).lower())
            ):
                fmt_s = format_dt(s_start)
                fmt_e = format_dt(s_end)
                if fmt_s and fmt_e:
                    ticket_name = (
                        obj.get("name") or obj.get("ticketName") or ""
                    )
                    prefix = f"【{ticket_name}】" if ticket_name else ""
                    period = f"{prefix}{fmt_s} ～ {fmt_e}"
                    if period not in extracted["sales_periods"]:
                        extracted["sales_periods"].append(period)

            for v in obj.values():
                walk(v)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return extracted


def fetch_event_details(event_url, headers):
    """個別ページのHTMLおよび内部JSON構造から情報を徹底抽出"""
    try:
        res = requests.get(event_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        page_text = soup.get_text(separator=" ", strip=True)

        extracted = {
            "title": "イベント名称未設定",
            "event_date": "情報なし",
            "open_time": "情報なし",
            "start_time": "情報なし",
            "sales_periods": [],
        }

        # 1. __NEXT_DATA__ スクリプトタグからのJSONディープサーチ
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if script_tag and script_tag.string:
            try:
                json_data = json.loads(script_tag.string)
                json_res = deep_search_json(json_data)

                if json_res["title"]:
                    extracted["title"] = json_res["title"]
                if json_res["event_date"]:
                    extracted["event_date"] = json_res["event_date"]
                if json_res["open_time"]:
                    extracted["open_time"] = json_res["open_time"]
                if json_res["start_time"]:
                    extracted["start_time"] = json_res["start_time"]
                if json_res["sales_periods"]:
                    extracted["sales_periods"] = json_res["sales_periods"]
            except Exception as e:
                print(f"JSON解析警告 ({event_url}): {e}")

        # 2. HTMLフォールバック（JSONで取得できなかった項目の補完）
        if extracted["title"] == "イベント名称未設定":
            h1_el = soup.find("h1")
            if h1_el and "販売情報" not in h1_el.get_text():
                extracted["title"] = clean_title(h1_el.get_text(strip=True))

        # 公演日の正規表現補完
        if extracted["event_date"] == "情報なし":
            date_match = re.search(
                r"(\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}\s*[\(（].+?[\)）])",
                page_text,
            )
            if date_match:
                extracted["event_date"] = date_match.group(1).strip()

        # 開場・開演時刻の正規表現補完
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

        # 販売期間の正規表現補完
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
