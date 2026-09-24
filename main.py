from datetime import datetime, timezone, timedelta
import json
import os
import re
import urllib.parse
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://ticketdive.com/artist/falench"
DATA_FILE = "seen_events.json"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_GROUP_ID = os.getenv("LINE_GROUP_ID")

JST = timezone(timedelta(hours=9))


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


def parse_datetime_obj(dt_input):
    """日時文字列や数値から datetime(JST) オブジェクトを取得"""
    if not dt_input:
        return None

    dt_str = str(dt_input).strip().replace("\\", "").replace('"', "")

    # 13桁のミリ秒タイムスタンプ
    if dt_str.isdigit() and len(dt_str) == 13:
        try:
            return datetime.fromtimestamp(int(dt_str) / 1000, tz=JST)
        except Exception:
            pass

    # ISO 8601
    try:
        clean_iso = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean_iso).astimezone(JST)
    except Exception:
        pass

    return None


def format_dt_obj(dt_obj):
    """datetime オブジェクトを 「MM/DD（曜日）HH:MM」 に整形"""
    if not dt_obj:
        return ""
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    return f"{dt_obj.month:02d}/{dt_obj.day:02d}（{weekdays[dt_obj.weekday()]}）{dt_obj.hour:02d}:{dt_obj.minute:02d}"


def fetch_api_details(event_slug, headers):
    """tRPC APIからチケット情報を取得し、最も適切な（最新/有効な）販売期間のみを抽出"""
    input_param = urllib.parse.quote(f'{{"json":{{"slug":"{event_slug}"}}}}')
    api_url = f"https://ticketdive.com/api/trpc/event.getBySlug?input={input_param}"

    api_headers = headers.copy()
    api_headers.update(
        {
            "x-trpc-source": "nextjs-react",
            "Referer": f"https://ticketdive.com/event/{event_slug}",
        }
    )

    try:
        res = requests.get(api_url, headers=api_headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            result_data = (
                data.get("result", {})
                .get("data", {})
                .get("json", {})
            )
            tickets = result_data.get("ticketTypes", []) or result_data.get(
                "tickets", []
            )

            now = datetime.now(JST)
            valid_periods = []
            upcoming_periods = []
            all_periods = []

            for t in tickets:
                t_name = t.get("name", "")
                s_start_raw = (
                    t.get("salesStartAt")
                    or t.get("sales_start_at")
                    or t.get("startAt")
                )
                s_end_raw = (
                    t.get("salesEndAt")
                    or t.get("sales_end_at")
                    or t.get("endAt")
                )

                dt_start = parse_datetime_obj(s_start_raw)
                dt_end = parse_datetime_obj(s_end_raw)

                if dt_start and dt_end:
                    fmt_s = format_dt_obj(dt_start)
                    fmt_e = format_dt_obj(dt_end)
                    prefix = f"【{t_name}】" if t_name else ""
                    period_str = f"{prefix}{fmt_s} ～ {fmt_e}"

                    item = {
                        "str": period_str,
                        "start": dt_start,
                        "end": dt_end,
                    }

                    if period_str not in [p["str"] for p in all_periods]:
                        all_periods.append(item)

                    # 現在販売中
                    if dt_start <= now <= dt_end:
                        if period_str not in [p["str"] for p in valid_periods]:
                            valid_periods.append(item)
                    # 将来の販売予定
                    elif now < dt_start:
                        if period_str not in [p["str"] for p in upcoming_periods]:
                            upcoming_periods.append(item)

            # 1. 現在販売中の枠があればそれを最優先（最大2件まで）
            if valid_periods:
                return [p["str"] for p in valid_periods[:2]]

            # 2. 次に販売予定の枠があればそれを優先（直近の1件）
            if upcoming_periods:
                upcoming_periods.sort(key=lambda x: x["start"])
                return [upcoming_periods[0]["str"]]

            # 3. すべて終了済みの場合は最新のものを1件のみ表示
            if all_periods:
                all_periods.sort(key=lambda x: x["end"], reverse=True)
                return [all_periods[0]["str"]]

    except Exception as e:
        print(f"API取得エラー ({event_slug}): {e}")

    return []


def fetch_event_details(event_url, headers):
    """個別イベントページの取得と解析"""
    try:
        res = requests.get(event_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None

        html = res.text
        soup = BeautifulSoup(html, "html.parser")

        extracted = {
            "title": "イベント名称未設定",
            "event_date": "情報なし",
            "sales_periods": [],
        }

        # 1. タイトル取得（og:title）
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            cand_og = clean_title(og_title["content"])
            if cand_og:
                extracted["title"] = cand_og

        # 2. メタタグ（og:description）から公演日を解析
        desc_tag = soup.find("meta", property="og:description") or soup.find(
            "meta", attrs={"name": "description"}
        )
        if desc_tag and desc_tag.get("content"):
            meta_desc = desc_tag["content"]
            m_date = re.search(
                r"【日付】\s*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})", meta_desc
            )
            if m_date:
                dt_d = parse_datetime_obj(m_date.group(1))
                if dt_d:
                    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
                    extracted["event_date"] = (
                        f"{dt_d.month:02d}/{dt_d.day:02d}（{weekdays[dt_d.weekday()]}）"
                    )

        # 3. APIから最適な販売期間のみを取得
        event_slug = event_url.split("/event/")[-1].split("/")[0].split("?")[0]
        api_periods = fetch_api_details(event_slug, headers)

        if api_periods:
            extracted["sales_periods"] = api_periods
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
