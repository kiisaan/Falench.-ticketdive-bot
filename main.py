import os
import json
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime

TARGET_URL = "https://ticketdive.com/artist/falench"
DATA_FILE = "seen_events.json"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_GROUP_ID = os.getenv("LINE_GROUP_ID")

def clean_title(raw_title):
    """タイトルから不要な接尾辞や共通画面文言を削る"""
    if not raw_title:
        return "イベント名称未設定"
    
    title = re.sub(r'[\r\n]+', ' ', str(raw_title)).strip()
    
    # 共通画面UIキーワードを除外
    ng_words = [
        "チケットの分配", "チケット分配", "販売情報", "チケット情報", 
        "TicketDive", "ログイン", "マイページ", "新規会員登録", "チケット購入"
    ]
    for ng in ng_words:
        title = re.sub(re.escape(ng), '', title, flags=re.IGNORECASE).strip()
        
    # 「｜」や「 - 」で分解（サイト名等を除去）
    title = title.split('｜')[0].split(' - ')[0].strip()
    
    # 出演者タグ以降を除去
    title = re.split(r'(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])', title)[0].strip()
    title = re.sub(r'^[\s:：\-–|]+|[\s\-/／:：]+$', '', title).strip()
    
    return title if title else "イベント名称未設定"

def format_dt(dt_input):
    """ISO8601表記（例: 2026-09-25T18:00:00.000Z）やタイムスタンプを「MM/DD（週）HH:MM」形式に統一"""
    if not dt_input:
        return None
    try:
        if isinstance(dt_input, (int, float)):
            dt = datetime.fromtimestamp(dt_input / 1000.0)
            weekdays = ["月", "火", "水", "木", "金", "土", "日"]
            return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"

        dt_str = str(dt_input).strip()
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        clean_iso = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        pass
    return str(dt_input)

def deep_search_json(data):
    """Next.js (dehydratedState / trpcState) のJSONツリーから必要なフィールドをピンポイント検索"""
    extracted = {
        "title": None,
        "event_date": None,
        "open_time": None,
        "start_time": None,
        "sales_periods": []
    }

    def walk(obj):
        if isinstance(obj, dict):
            # 1. タイトル
            if not extracted["title"]:
                for k in ["name", "title", "eventName"]:
                    val = obj.get(k)
                    if isinstance(val, str) and val.strip():
                        cleaned = clean_title(val)
                        if cleaned and cleaned != "イベント名称未設定":
                            extracted["title"] = cleaned
                            break

            # 2. 開場・開演時刻・公演日 (キャメルケース/スネークケース両対応)
            open_at = obj.get("open_at") or obj.get("openAt")
            if open_at and not extracted["open_time"]:
                extracted["open_time"] = format_dt(open_at)

            start_at = obj.get("start_at") or obj.get("startAt")
            if start_at and not extracted["start_time"]:
                extracted["start_time"] = format_dt(start_at)

            event_date = obj.get("event_date") or obj.get("eventDate") or obj.get("date")
            if event_date and not extracted["event_date"]:
                extracted["event_date"] = format_dt(event_date)

            # 3. 販売期間
            s_start = obj.get("sales_start_at") or obj.get("salesStartAt")
            s_end = obj.get("sales_end_at") or obj.get("salesEndAt")
            if s_start and s_end:
                fmt_s = format_dt(s_start)
                fmt_e = format_dt(s_end)
                t_name = obj.get("name") or obj.get("ticket_name") or obj.get("ticketName") or ""
                prefix = f"【{t_name}】" if t_name and t_name != extracted["title"] else ""
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
    """個別イベントページの取得と解析"""
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
            "sales_periods": []
        }

        # 1. __NEXT_DATA__ 解析
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
                print(f"JSON解析エラー ({event_url}): {e}")

        # 2. og:title からタイトルの確実な取得（優先順位引き上げ）
        if extracted["title"] == "イベント名称未設定":
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                extracted["title"] = clean_title(og_title["content"])

        # 公演日のフォールバック
        if extracted["event_date"] == "情報なし" and extracted["start_time"] != "情報なし":
            if "（" in extracted["start_time"]:
                extracted["event_date"] = extracted["start_time"].split("）")[0] + "）"

        # 販売期間のフォールバック
        if not extracted["sales_periods"]:
            sales_m = re.findall(r'(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2}\s*～\s*\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})', page_text)
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
            full_url = href if href.startswith("http") else f"https://ticketdive.com{href}"
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
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_GROUP_ID,
        "messages": [{"type": "text", "text": message}]
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
        print(f"{len(new_events)} 件の新着イベントを検知しました。LINEに通知します。")

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
            chunk = msg_blocks[i:i + chunk_size]
            msg = "【Falench.ライブ情報（ダイブ）】\n\n"
            msg += "\n\n──────────────────\n\n".join(chunk)
            send_line_message(msg)

        save_seen_events(seen)
    else:
        print("新着イベントはありませんでした。")

if __name__ == "__main__":
    main()
