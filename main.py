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
    """タイトルから不要な文言や共通画面要素を除去"""
    if not raw_title:
        return "イベント名称未設定"

    title = re.sub(r"[\r\n]+", " ", str(raw_title)).strip()

    # 「チケットの分配」などのUI共通見出しやデフォルト文字列を徹底削除
    ng_words = [
        "チケットの分配",
        "チケット分配",
        "販売情報",
        "チケット情報",
        "TicketDive",
        "ログイン",
        "マイページ",
        "新規会員登録",
    ]
    for ng in ng_words:
        title = re.sub(re.escape(ng), "", title, flags=re.IGNORECASE).strip()

    # 出演者表記以降をカット
    title = re.split(
        r"(?:【出演】|出演[：:]|［出演］|ACT[：:]|【CAST】|CAST[：:])", title
    )[0].strip()
    title = re.sub(r"^[\s:：\-–|]+|[\s\-/／:：]+$", "", title).strip()

    return title if title else "イベント名称未設定"


def format_dt(dt_input):
    """ISO8601表記やUnixタイムスタンプを「MM/DD（週）HH:MM」形式に変換"""
    if not dt_input:
        return None

    try:
        # 数値（ミリ秒タイムスタンプ）の場合
        if isinstance(dt_input, (int, float)):
            dt = datetime.fromtimestamp(dt_input / 1000.0)
            weekdays = ["月", "火", "水", "木", "金", "土", "日"]
            return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"

        dt_str = str(dt_input).strip()
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]

        # ISO 8601
        clean_iso = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        pass

    return str(dt_input)


def extract_from_trpc(data):
    """TicketDive固有の tRPC データ構造から正確にイベント情報を抽出"""
    result = {
        "title": None,
        "event_date": None,
        "open_time": None,
        "start_time": None,
        "sales_periods": [],
    }

    try:
        queries = (
            data.get("props", {})
            .get("pageProps", {})
            .get("trpcState", {})
            .get("json", {})
            .get("queries", [])
        )

        for query in queries:
            state = query.get("state", {}).get("data", {})
            if not isinstance(state, dict):
                continue

            # イベント本体データの探索
            event_data = state.get("event") or state
            if isinstance(event_data, dict):
                if event_data.get("name") and not result["title"]:
                    result["title"] = clean_title(event_data.get("name"))

                if event_data.get("openAt") and not result["open_time"]:
                    result["open_time"] = format_dt(event_data.get("openAt"))

                if event_data.get("startAt") and not result["start_time"]:
                    result["start_time"] = format_dt(event_data.get("startAt"))

                if event_data.get("eventDate") and not result["event_date"]:
                    result["event_date"] = format_dt(
                        event_data.get("eventDate")
                    )
                elif event_data.get("startAt") and not result["event_date"]:
                    # startAtから日付部分のみを取得
                    st_fmt = format_dt(event_data.get("startAt"))
                    if st_fmt and "（" in st_fmt:
                        result["event_date"] = st_fmt.split("）")[0] + "）"

            # チケット情報・販売期間の探索
            ticket_types = state.get("ticketTypes") or state.get(
                "tickets", []
            )
            if isinstance(ticket_types, list):
                for t in ticket_types:
                    if isinstance(t, dict):
                        s_start = t.get("salesStartAt") or t.get(
                            "sales_start_at"
                        )
                        s_end = t.get("salesEndAt") or t.get("sales_end_at")
                        t_name = (
                            t.get("name")
                            or t.get("title")
                            or t.get("ticketName")
                            or ""
                        )

                        if s_start and s_end:
                            fmt_s = format_dt(s_start)
                            fmt_e = format_dt(s_end)
                            prefix = f"【{t_name}】" if t_name else ""
                            period = f"{prefix}{fmt_s} ～ {fmt_e}"
                            if period not in result["sales_periods"]:
                                result["sales_periods"].append(period)
    except Exception as e:
        print(f"tRPC解析中の軽微なエラー: {e}")

    return result


def fetch_event_details(event_url, headers):
    """個別イベントページの取得および多角解析"""
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

        # 1. __NEXT_DATA__ (tRPC解析)
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if script_tag and script_tag.string:
            try:
                json_data = json.loads(script_tag.string)
                trpc_res = extract_from_trpc(json_data)

                if trpc_res["title"]:
                    extracted["title"] = trpc_res["title"]
                if trpc_res["event_date"]:
                    extracted["event_date"] = trpc_res["event_date"]
                if trpc_res["open_time"]:
                    extracted["open_time"] = trpc_res["open_time"]
                if trpc_res["start_time"]:
                    extracted["start_time"] = trpc_res["start_time"]
                if trpc_res["sales_periods"]:
                    extracted["sales_periods"] = trpc_res["sales_periods"]
            except Exception as e:
                print(f"JSONパースエラー ({event_url}): {e}")

        # 2. og:title / meta タグからのタイトル補完（JSON解析で漏れた場合）
        if (
            extracted["title"] == "イベント名称未設定"
            or extracted["title"] == ""
        ):
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                extracted["title"] = clean_title(
                    og_title["content"].split("｜")[0].split(" - ")[0]
                )

        # 3. テキスト正規表現によるフォールバック補完
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
