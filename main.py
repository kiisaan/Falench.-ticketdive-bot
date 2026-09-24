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

    # 13桁ミリ秒または10桁秒のタイムスタンプ
    if dt_str.isdigit():
        try:
            val = int(dt_str)
            if len(dt_str) == 13:
                return datetime.fromtimestamp(val / 1000, tz=JST)
            elif len(dt_str) == 10:
                return datetime.fromtimestamp(val, tz=JST)
        except Exception:
            pass

    # ISO 8601等
    try:
        clean_iso = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean_iso).astimezone(JST)
    except Exception:
        pass

    # YYYY/MM/DD や YYYY-MM-DD
    m_date = re.search(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", dt_str)
    if m_date:
        try:
            year, month, day = (
                int(m_date.group(1)),
                int(m_date.group(2)),
                int(m_date.group(3)),
            )
            return datetime(year, month, day, tzinfo=JST)
        except Exception:
            pass

    return None


def format_dt_obj(dt_obj, include_time=True):
    """datetime オブジェクトを整形"""
    if not dt_obj:
        return ""
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    if include_time:
        return f"{dt_obj.month:02d}/{dt_obj.day:02d}（{weekdays[dt_obj.weekday()]}）{dt_obj.hour:02d}:{dt_obj.minute:02d}"
    return f"{dt_obj.month:02d}/{dt_obj.day:02d}（{weekdays[dt_obj.weekday()]}）"


def extract_sales_periods_from_html(html_text):
    """HTML全テキストから正規表現で直接販売期間（開始/終了日時）のペアを抽出"""
    tickets = []

    # パターン1: salesStartAt/salesEndAt または startAt/endAt パターン
    patterns = [
        r'(?:salesStartAt|sales_start_at|startAt)["\']?\s*:\s*["\']?([^"\',\}]+)["\']?[\s\S]*?(?:salesEndAt|sales_end_at|endAt)["\']?\s*:\s*["\']?([^"\',\}]+)["\']?',
        r'["\']?salesStartAt["\']?\s*:\s*["\']?([^"\',\}]+)["\']?[\s\S]*?["\']?salesEndAt["\']?\s*:\s*["\']?([^"\',\}]+)["\']?',
    ]

    for pat in patterns:
        matches = re.findall(pat, html_text)
        for s_start, s_end in matches:
            dt_s = parse_datetime_obj(s_start)
            dt_e = parse_datetime_obj(s_end)
            if dt_s and dt_e:
                item = {"name": "", "start": dt_s, "end": dt_e}
                if item not in tickets:
                    tickets.append(item)

    return tickets


def search_json_recursive(data):
    """JSON構造内からチケット販売情報と公演日時を全探索"""
    tickets = []
    event_dates = []

    def walk(obj):
        if isinstance(obj, dict):
            # 公演日時の取得
            for k in ["eventDate", "event_date", "openAt", "eventStartAt"]:
                if k in obj and obj[k]:
                    dt = parse_datetime_obj(obj[k])
                    if dt and dt not in event_dates:
                        event_dates.append(dt)

            # チケット販売枠の取得
            s_start = (
                obj.get("salesStartAt")
                or obj.get("sales_start_at")
                or obj.get("salesStart")
                or obj.get("startAt")
            )
            s_end = (
                obj.get("salesEndAt")
                or obj.get("sales_end_at")
                or obj.get("salesEnd")
                or obj.get("endAt")
            )
            t_name = obj.get("name") or obj.get("title") or obj.get("ticketTypeName") or ""

            if (obj.get("salesStartAt") or obj.get("sales_start_at") or obj.get("ticketSalesStartAt")) and s_end:
                dt_s = parse_datetime_obj(s_start)
                dt_e = parse_datetime_obj(s_end)
                if dt_s and dt_e:
                    item = {"name": str(t_name), "start": dt_s, "end": dt_e}
                    if item not in tickets:
                        tickets.append(item)

            for v in obj.values():
                walk(v)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return tickets, event_dates


def process_extracted_periods(tickets, event_dates):
    """取得したチケット配列から現在/未来の最適な販売期間を抽出"""
    now = datetime.now(JST)
    valid_periods = []
    upcoming_periods = []
    all_periods = []

    for t in tickets:
        dt_start = t["start"]
        dt_end = t["end"]

        fmt_s = format_dt_obj(dt_start, include_time=True)
        fmt_e = format_dt_obj(dt_end, include_time=True)
        prefix = f"【{t['name']}】" if t["name"] and len(t["name"]) < 25 else ""
        period_str = f"{prefix}{fmt_s} ～ {fmt_e}"

        item = {"str": period_str, "start": dt_start, "end": dt_end}

        if period_str not in [p["str"] for p in all_periods]:
            all_periods.append(item)

        if dt_start <= now <= dt_end:
            if period_str not in [p["str"] for p in valid_periods]:
                valid_periods.append(item)
        elif now < dt_start:
            if period_str not in [p["str"] for p in upcoming_periods]:
                upcoming_periods.append(item)

    selected_periods = []
    if valid_periods:
        selected_periods = [p["str"] for p in valid_periods[:2]]
    elif upcoming_periods:
        upcoming_periods.sort(key=lambda x: x["start"])
        selected_periods = [upcoming_periods[0]["str"]]
    elif all_periods:
        all_periods.sort(key=lambda x: x["end"], reverse=True)
        selected_periods = [all_periods[0]["str"]]

    fmt_event_date = None
    if event_dates:
        fmt_event_date = format_dt_obj(event_dates[0], include_time=False)

    return selected_periods, fmt_event_date


def fetch_api_details(event_slug, headers):
    """tRPC APIから直接イベント情報を取得（構文エラー回避修正版）"""
    api_headers = headers.copy()
    api_headers.update(
        {
            "x-trpc-source": "nextjs-react",
            "Accept": "application/json",
            "Referer": f"https://ticketdive.com/event/{event_slug}",
        }
    )

    # f-string 内でのエスケープ文字エラーを避けるため事前に文字を作成
    param_single = json.dumps({"json": {"slug": event_slug}})
    param_batch = json.dumps({"0": {"json": {"slug": event_slug}}})
    param_id = json.dumps({"json": {"id": event_slug}})

    endpoints = [
        f"https://ticketdive.com/api/trpc/event.getBySlug?input={urllib.parse.quote(param_single)}",
        f"https://ticketdive.com/api/trpc/event.getBySlug?batch=1&input={urllib.parse.quote(param_batch)}",
        f"https://ticketdive.com/api/trpc/event.getById?input={urllib.parse.quote(param_id)}",
    ]

    for api_url in endpoints:
        try:
            res = requests.get(api_url, headers=api_headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                tickets, event_dates = search_json_recursive(data)
                periods, e_date = process_extracted_periods(tickets, event_dates)
                if periods:
                    return {"sales_periods": periods, "event_date": e_date}
        except Exception:
            pass

    return None


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

        # 2. メタタグ（og:description）から公演日を補完
        desc_tag = soup.find("meta", property="og:description") or soup.find(
            "meta", attrs={"name": "description"}
        )
        if desc_tag and desc_tag.get("content"):
            meta_desc = desc_tag["content"]
            m_date = re.search(
                r"【?日付】?\s*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2}|\d{1,2}[/\-]\d{1,2})",
                meta_desc,
            )
            if m_date:
                dt_d = parse_datetime_obj(m_date.group(1))
                if dt_d:
                    extracted["event_date"] = format_dt_obj(dt_d, include_time=False)

        # 3. HTMLソース全体からの直接テキスト抽出
        html_tickets = extract_sales_periods_from_html(html)
        if html_tickets:
            periods, e_date = process_extracted_periods(html_tickets, [])
            if periods:
                extracted["sales_periods"] = periods

        # 4. JSONタグからの解析
        if not extracted["sales_periods"]:
            for script in soup.find_all("script"):
                if script.string and ("salesStart" in script.string or "salesStartAt" in script.string or "ticket" in script.string):
                    try:
                        s_json = json.loads(script.string)
                        tickets, event_dates = search_json_recursive(s_json)
                        periods, e_date = process_extracted_periods(tickets, event_dates)
                        if periods:
                            extracted["sales_periods"] = periods
                            if e_date and extracted["event_date"] == "情報なし":
                                extracted["event_date"] = e_date
                            break
                    except Exception:
                        pass

        # 5. tRPC API からのバックアップ取得
        if not extracted["sales_periods"]:
            event_slug = event_url.split("/event/")[-1].split("/")[0].split("?")[0]
            api_data = fetch_api_details(event_slug, headers)
            if api_data:
                if api_data["sales_periods"]:
                    extracted["sales_periods"] = api_data["sales_periods"]
                if api_data["event_date"] and extracted["event_date"] == "情報なし":
                    extracted["event_date"] = api_data["event_date"]

        # フォールバック表記
        if not extracted["sales_periods"]:
            extracted["sales_periods"] = ["公式ページをご確認ください"]

        extracted["url"] = event_url
        return extracted

    except Exception as e:
        print(f"詳細取得例外 ({event_url}): {e}")
        return None


def fetch_events():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    response = requests.get(TARGET_URL, headers=headers)
    if response.status_code != 200:
        print(f"一覧ページ取得エラー: {response.status_code}")
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
