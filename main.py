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

    # 画面上のボタ言や汎用ラベルを除外
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


def parse_iso_dt(iso_str):
    """ISO8601文字列（2026-09-27T10:30:00Z など）から (日付文字列, 時刻文字列) を返す"""
    if not iso_str:
        return None, None

    try:
        clean_iso = str(iso_str).replace("\\", "").replace('"', "").strip()
        clean_iso = clean_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)

        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        date_part = f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）"
        time_part = f"{dt.hour:02d}:{dt.minute:02d}"

        return date_part, time_part
    except Exception:
        return None, None


def parse_app_router_data(html_content):
    """Next.js App RouterのエスケープされたJSONストリーミングデータを解析"""
    extracted = {
        "title": None,
        "event_date": None,
        "open_time": None,
        "start_time": None,
        "sales_periods": [],
    }

    # 1. タイトル抽出
    name_matches = re.findall(r'\\"name\\"\s*:\s*\\"([^"\\]+)\\"', html_content)
    for n in name_matches:
        cand = clean_title(n)
        if cand and cand not in [
            "名前",
            "チケット",
            "一般",
            "優先",
            "前方",
            "VIP",
        ]:
            extracted["title"] = cand
            break

    # 2. 開場（openAt）の解析 -> 公演日 & 開場時刻
    open_match = re.search(r'\\"openAt\\"\s*:\s*\\"([^"\\]+)\\"', html_content)
    if open_match:
        d_str, t_str = parse_iso_dt(open_match.group(1))
        if d_str:
            extracted["event_date"] = d_str
        if t_str:
            extracted["open_time"] = t_str

    # 3. 開演（startAt）の解析 -> 公演日（補完） & 開演時刻
    start_match = re.search(r'\\"startAt\\"\s*:\s*\\"([^"\\]+)\\"', html_content)
    if start_match:
        d_str, t_str = parse_iso_dt(start_match.group(1))
        if not extracted["event_date"] and d_str:
            extracted["event_date"] = d_str
        if t_str:
            extracted["start_time"] = t_str

    # 4. チケット販売期間の抽出 (salesStartAt ~ salesEndAt)
    sales_matches = re.findall(
        r'\\"salesStartAt\\"\s*:\s*\\"([^"\\]+)\\"[^\}]*?\\"salesEndAt\\"\s*:\s*\\"([^"\\]+)\\"',
        html_content,
    )
    for s_start, s_end in sales_matches:
        d1, t1 = parse_iso_dt(s_start)
        d2, t2 = parse_iso_dt(s_end)

        if d1 and t1 and d2 and t2:
            period = f"{d1}{t1} ～ {d2}{t2}"
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

        # 1. og:title メタタグからタイトルを優先取得
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            cand_og = clean_title(og_title["content"])
            if cand_og:
                extracted["title"] = cand_og

        # 2. Next.js App Routerのストリーミングデータを解読
        app_res = parse_app_router_data(html)

        if (
            extracted["title"] == "イベント名称未設定"
            and app_res["title"]
        ):
            extracted["title"] = app_res["title"]

        if app_res["event_date"]:
            extracted["event_date"] = app_res["event_date"]
        if app_res["open_time"]:
            extracted["open_time"] = app_res["open_time"]
        if app_res["start_time"]:
            extracted["start_time"] = app_res["start_time"]
        if app_res["sales_periods"]:
            extracted["sales_periods"] = app_res["sales_periods"]

        # 3. テキストからの補完処理
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

        # 公演日のテキスト補完
        if extracted["event_date"] == "情報なし":
            date_m = re.search(
                r"(\d{1,2}/\d{1,2}\s*[\(（][月火水木金土日祝][\)）])", page_text
            )
            if date_m:
                extracted["event_date"] = date_m.group(1).strip()

        # 販売期間のテキスト補完
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
