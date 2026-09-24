from datetime import datetime, timezone, timedelta
import json
import os
import re
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET_URL = "https://ticketdive.com/artist/falench"
DATA_FILE = "seen_events.json"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_GROUP_ID = os.getenv("LINE_GROUP_ID")

JST = timezone(timedelta(hours=9))


def clean_title(raw_title):
    """タイトルから不要な表記や接尾辞を除去"""
    if not raw_title:
        return ""
    title = re.sub(r"[\r\n]+", " ", str(raw_title)).strip()
    ng_words = [
        "お気に入り", "シェア", "チケットの分配", "チケット分配",
        "販売情報", "チケット情報", "TicketDive", "ログイン",
        "マイページ", "新規会員登録", "チケット購入", "名前", "氏名"
    ]
    for ng in ng_words:
        if title == ng:
            return ""
        title = re.sub(re.escape(ng), "", title, flags=re.IGNORECASE).strip()

    title = title.split("｜")[0].split(" - ")[0].strip()
    title = re.split(r"(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])", title)[0].strip()
    return title.strip(" :：-–|/／")


def fetch_event_details_with_browser(event_url):
    """Playwrightを使って実際にブラウザでページを開き、動的に描画された販売期間を取得"""
    try:
        with sync_playwright() as p:
            # ヘッドレスブラウザの起動
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # ページへアクセスし、ネットワーク通信が落ち着くまで待機
            page.goto(event_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)  # Reactの描画完了まで3秒追加待機

            # タイトルの取得
            title = "イベント名称未設定"
            try:
                h1_element = page.query_selector("h1")
                if h1_element:
                    title = clean_title(h1_element.inner_text())
            except Exception:
                pass

            if not title or title == "イベント名称未設定":
                content = page.content()
                soup = BeautifulSoup(content, "html.parser")
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    title = clean_title(og_title["content"])

            # 画面上のテキスト全取得
            body_text = page.inner_text("body")
            lines = [line.strip() for line in body_text.splitlines() if line.strip()]

            sales_periods = []
            
            # 販売期間・チケット情報の解析
            for i, line in enumerate(lines):
                # 販売期間・受付期間に関連するキーワードを探す
                if any(kw in line for kw in ["販売期間", "受付期間", "販売:", "受付:", "先着", "抽選"]):
                    # 該当行とその前後数行をまとめてテキスト化
                    start_idx = max(0, i - 1)
                    end_idx = min(len(lines), i + 4)
                    block = " ".join(lines[start_idx:end_idx])

                    # 日時フォーマットパターン（例: 2026/09/01(火) 12:00 ～ 2026/09/10(木) 23:59）
                    matches = re.findall(
                        r"(\d{4}[/\-]\d{1,2}[/\-]\d{1,2}[\s\S]*?\d{1,2}:\d{2}\s*[\~～\-–—]\s*(?:\d{4}[/\-])?\d{1,2}[/\-]\d{1,2}[\s\S]*?\d{1,2}:\d{2})",
                        block
                    )
                    for m in matches:
                        clean_m = re.sub(r"\s+", " ", m).strip()
                        if clean_m not in sales_periods:
                            sales_periods.append(clean_m)

            # 公演日時の抽出
            event_date = "情報なし"
            m_date = re.search(r"(\d{4}[/\-]\d{1,2}[/\-]\d{1,2}|\d{1,2}月\d{1,2}日)", body_text)
            if m_date:
                event_date = m_date.group(1)

            browser.close()

            if not sales_periods:
                sales_periods = ["公式ページをご確認ください"]

            return {
                "title": title if title else "イベント名称未設定",
                "event_date": event_date,
                "sales_periods": sales_periods[:3],  # 最大3件まで表示
                "url": event_url
            }

    except Exception as e:
        print(f"ブラウザ実行エラー ({event_url}): {e}")
        return {
            "title": "イベント名称未設定",
            "event_date": "情報なし",
            "sales_periods": ["公式ページをご確認ください"],
            "url": event_url
        }


def fetch_events():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    response = requests.get(TARGET_URL, headers=headers)
    if response.status_code != 200:
        print(f"一覧取得エラー: {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    events = []

    links = soup.find_all("a", href=True)
    for link in links:
        href = link["href"]
        if "/events/" in href or "/event/" in href:
            full_url = href if href.startswith("http") else f"https://ticketdive.com{href}"
            if full_url not in [e["url"] for e in events]:
                print(f"解析中: {full_url}")
                details = fetch_event_details_with_browser(full_url)
                if details:
                    events.append(details)

    return events


def load_seen_events():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:  # ファイルが空の場合
                    return set()
                return set(json.loads(content))
        except json.JSONDecodeError:
            print("seen_events.json の読み込みに失敗したため、空のリストで初期化します。")
            return set()
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
        print(f"LINE送信エラー: {res.status_code}, {res.text}")
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
            sales_text = "\n".join(ev["sales_periods"])
            block_text = (
                f"イベント名：{ev['title']}\n"
                f"販売期間：{sales_text}\n"
                f"公演日：{ev['event_date']}\n"
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
