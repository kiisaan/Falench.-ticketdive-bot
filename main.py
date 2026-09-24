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
    """タイトルから余分な接尾辞を綺麗に除去"""
    if not raw_title:
        return "イベント名称未設定"
    
    title = re.sub(r'[\r\n]+', ' ', raw_title).strip()
    
    # 静的メタタグ等のデフォルト文言を除去
    title = re.sub(r'[\s:：\-–|]*?(?:販売情報|チケット情報|TicketDive).*$', '', title, flags=re.IGNORECASE).strip()
    
    # 出演者タグなどでカット
    title = re.split(r'(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])', title)[0].strip()
    title = re.sub(r'[\s\-/／:：]+$', '', title).strip()
    
    return title if title else "イベント名称未設定"

def extract_json_data(soup):
    """Next.js / Nuxt などの埋め込みJSON (__NEXT_DATA__) からイベント詳細データを全抽出"""
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if script_tag and script_tag.string:
        try:
            return json.loads(script_tag.string)
        except Exception:
            pass
    return None

def format_datetime_str(iso_str):
    """ISO8601形式（例: 2026-09-20T10:00:00Z）を『MM/DD（週）HH:MM』形式に整形"""
    if not iso_str:
        return None
    try:
        # ISO文字列解析
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        return iso_str

def parse_sales_from_json(json_data):
    """JSON内部構造を再帰的に走査して販売開始・終了日時（sales_start_at / sales_end_at）等を抽出"""
    sales_list = []
    
    def search_dict(d):
        if isinstance(d, dict):
            # ticket_types や sales パラメータの特定
            start = d.get("sales_start_at") or d.get("salesStartAt") or d.get("start_at")
            end = d.get("sales_end_at") or d.get("salesEndAt") or d.get("end_at")
            name = d.get("name") or d.get("ticket_name") or ""
            
            if start and end:
                fmt_start = format_datetime_str(start)
                fmt_end = format_datetime_str(end)
                period_str = f"{fmt_start} ～ {fmt_end}"
                if name:
                    period_str = f"【{name}】{period_str}"
                sales_list.append(period_str)
            
            for v in d.values():
                search_dict(v)
        elif isinstance(d, list):
            for item in d:
                search_dict(item)

    search_dict(json_data)
    
    # 重複削除
    unique_sales = []
    for s in sales_list:
        if s not in unique_sales:
            unique_sales.append(s)
            
    return unique_sales

def fetch_event_details(event_url, headers):
    """個別イベントページからJSONおよびフォールバック解析"""
    try:
        res = requests.get(event_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
        
        soup = BeautifulSoup(res.text, "html.parser")
        page_text = soup.get_text(separator=" ", strip=True)
        
        # 1. 埋め込みJSONの解析
        json_data = extract_json_data(soup)
        
        title = ""
        sales_periods = []
        event_date = "詳細ページをご確認ください"
        open_time = "情報なし"
        start_time = "情報なし"
        
        if json_data:
            # JSONからタイトルと販売期間を取得
            sales_periods = parse_sales_from_json(json_data)
            
            # JSON内のイベントタイトル抽出
            try:
                page_props = json_data.get("props", {}).get("pageProps", {})
                event_info = page_props.get("event", {}) or page_props.get("eventDetail", {})
                if event_info.get("name"):
                    title = clean_title(event_info.get("name"))
            except Exception:
                pass

        # 2. JSONで取得できなかった場合のフォールバック（HTMLタグ解析）
        if not title:
            h1_el = soup.find("h1")
            raw_title = h1_el.get_text(strip=True) if h1_el else ""
            if not raw_title or "販売情報" in raw_title:
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    raw_title = og_title["content"]
            title = clean_title(raw_title)

        # 日時・時刻の抽出
        date_match = re.search(r'(?:日程|開催日|公演日|DATE)[\s:：]*(\d{1,2}/\d{1,2}\s*[\(（].+?[\)）]|\d{4}/\d{1,2}/\d{1,2})', page_text, re.IGNORECASE)
        if date_match:
            event_date = date_match.group(1).strip()
            
        open_match = re.search(r'(?:開場|OPEN)[\s:：]*(\d{1,2}:\d{2})', page_text, re.IGNORECASE)
        if open_match:
            open_time = open_match.group(1).strip()
            
        start_match = re.search(r'(?:開演|START)[\s:：]*(\d{1,2}:\d{2})', page_text, re.IGNORECASE)
        if start_match:
            start_time = start_match.group(1).strip()

        # 販売期間がまだ抽出できていない場合のテキスト正規表現パターン
        if not sales_periods:
            pattern = r'(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2}\s*～\s*\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})'
            matches = re.findall(pattern, page_text)
            sales_periods = list(set(matches)) if matches else ["公式ページにてご確認ください"]

        return {
            "title": title,
            "event_date": event_date,
            "open_time": open_time,
            "start_time": start_time,
            "sales_periods": sales_periods,
            "url": event_url
        }

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
    
    # ページ内のイベントリンク抽出
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
        print(f"{len(new_events)} 件の新着イベントを検知しました。")
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
