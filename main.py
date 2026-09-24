import json
import os
import re
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://ticketdive.com/artist/falench"
DATA_FILE = "seen_events.json"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_GROUP_ID = os.getenv("LINE_GROUP_ID")


def fetch_event_details(event_url, headers):
    """個別イベントページから「イベント名」「チケ発時間」「会場」を取得する関数"""
    try:
        res = requests.get(event_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return {"title": "（取得失敗）", "ticket_time": "不明", "venue": "不明"}

        soup = BeautifulSoup(res.text, "html.parser")

        # 1. イベント名取得
        title_el = soup.find("h1") or soup.find("h2")
        title = title_el.get_text(strip=True) if title_el else "タイトル不明"

        # 2. 会場名の取得
        venue = "会場情報なし"
        # ページ内のテキストや特定のタグから会場らしき場所を探す
        page_text = soup.get_text()
        venue_match = re.search(r"(?:会場|場所|LIVE HOUSE|@)[\s:：]*([^\n\r]+)", page_text)
        if venue_match:
            venue = venue_match.group(1).strip()
        else:
            # アイコンや要素のキーワード検索
            for el in soup.find_all(["p", "div", "span"]):
                txt = el.get_text(strip=True)
                if any(k in txt for k in ["ホール", "ライブハウス", "ビル", "Club", "CLUB", "劇場", "ReNY", "WWW", "キネマ"]):
                    if len(txt) < 30:
                        venue = txt
                        break

        # 3. チケ発時間の取得（受付中か、日付指定か）
        ticket_time = "詳細ページをご確認ください"
        if "受付中" in page_text or "販売中" in page_text:
            ticket_time = "申込受付中"
        else:
            # ○/○(○) ○:○ 形式の日時パターンを探す
            date_match = re.search(r"(\d{1,2}/\d{1,2}\s*[\(（].+?[\)）]\s*\d{1,2}:\d{2}\s*～?)", page_text)
            if date_match:
                ticket_time = date_match.group(1)

        return {
            "title": title,
            "ticket_time": ticket_time,
            "venue": venue
        }
    except Exception as e:
        print(f"詳細取得エラー ({event_url}): {e}")
        return {"title": "（取得エラー）", "ticket_time": "不明", "venue": "不明"}


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
            full_url = href if href.startswith("http") else f"https://ticketdive.com{href}"
            if full_url not in [e["url"] for e in events]:
                # 各イベントの個別ページを開いて詳細情報をスクレイピング
                details = fetch_event_details(full_url, headers)
                events.append({
                    "url": full_url,
                    "title": details["title"],
                    "ticket_time": details["ticket_time"],
                    "venue": details["venue"]
                })

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
        print(f"{len(new_events)} 件の新着イベントを検知しました。LINEに送信します。")

        # LINEの文字数制限対策で3件ずつ送信
        chunk_size = 3
        for i in range(0, len(new_events), chunk_size):
            chunk = new_events[i : i + chunk_size]
            msg = "【Falench.ライブ情報（ダイブ）】\n\n"
            
            items = []
            for ev in chunk:
                item_text = (
                    f"・{ev['title']}\n"
                    f"・{ev['ticket_time']}\n"
                    f"・{ev['venue']}\n"
                    f"・{ev['url']}"
                )
                items.append(item_text)
            
            msg += "\n\n──────────────────\n\n".join(items)
            send_line_message(msg)

        save_seen_events(seen)
    else:
        print("新着イベントはありませんでした。")


if __name__ == "__main__":
    main()
    
