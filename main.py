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


def format_dt(dt_input):
    """日付・時刻表現を「MM/DD（曜日）HH:MM」等に整形"""
    if not dt_input:
        return None

    dt_str = str(dt_input).strip().replace("\\", "").replace('"', "")

    # 13桁のミリ秒タイムスタンプ
    if dt_str.isdigit() and len(dt_str) == 13:
        try:
            dt = datetime.fromtimestamp(int(dt_str) / 1000, tz=JST)
            weekdays = ["月", "火", "水", "木", "金", "土", "日"]
            return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
        except Exception:
            pass

    # ISO 8601
    try:
        clean_iso = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso).astimezone(JST)
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        pass

    # YYYY/MM/DD または YYYY-MM-DD
    m_date = re.search(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", dt_str)
    if m_date:
        try:
            year, month, day = (
                int(m_date.group(1)),
                int(m_date.group(2)),
                int(m_date.group(3)),
            )
            dt = datetime(year, month, day)
            weekdays = ["月", "火", "水", "木", "金", "土", "日"]
            return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）"
        except Exception:
            pass

    return dt_str


def parse_raw_html_for_dates(html):
    """HTML全文字列からISO日時ペア・タイムスタンプペアを強力抽出"""
    periods = []

    # パターン1: ISO日時のペア（"2026-08-28T13:00:00.000Z" 等）
    iso_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+\d{2}:\d{2})?"
    found_isos = re.findall(iso_pattern, html)

    if len(found_isos) >= 2:
        # ペアを作成して整形
        for i in range(0, len(found_isos) - 1, 2):
            s_fmt = format_dt(found_isos[i])
            e_fmt = format_dt(found_isos[i + 1])
            if s_fmt and e_fmt:
                p = f"{s_fmt} ～ {e_fmt}"
                if p not in periods:
                    periods.append(p)

    # パターン2: 日本語表記（例: 08/28(金)22:00 ～ 09/27(日)10:30）
    sales_m = re.findall(
        r"(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2}\s*～\s*\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})",
        html,
    )
    for m in sales_m:
        if m not in periods:
            periods.append(m)

    return periods


def fetch_api_details(event_slug, headers):
    """tRPC APIから正確なチケット情報を直接取得"""
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
            periods = parse_raw_html_for_dates(res.text)

            # JSONデータからチケット種別ごとの抽出
            try:
                data = res.json()
                result_data = (
                    data.get("result", {})
                    .get("data", {})
                    .get("json", {})
                )
                tickets = result_data.get("ticketTypes", []) or result_data.get(
                    "tickets", []
                )

                for t in tickets:
                    t_name = t.get("name", "")
                    s_start = (
                        t.get("salesStartAt")
                        or t.get("sales_start_at")
                        or t.get("startAt")
                    )
                    s_end = (
                        t.get("salesEndAt")
                        or t.get("sales_end_at")
                        or t.get("endAt")
                    )

                    if s_start and s_end:
                        fmt_s = format_dt(s_start)
                        fmt_e = format_dt(s_end)
                        prefix = f"【{t_name}】" if t_name else ""
                        p = f"{prefix}{fmt_s} ～ {fmt_e}"
                        if p not in periods:
                            periods.append(p)
            except Exception:
                pass

            return periods
    except Exception as e:
        print(f"API取得失敗 ({event_slug}): {e}")

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
                extracted["event_date"] = format_dt(m_date.group(1))

        # 3. イベントslugの抽出とAPI直接リクエスト
        event_slug = event_url.split("/event/")[-1].split("/")[0].split("?")[0]
        api_periods = fetch_api_details(event_slug, headers)
        if api_periods:
            extracted["sales_periods"].extend(api_periods)

        # 4. 生HTMLからの正規表現全探索（フォールバック）
        if not extracted["sales_periods"]:
            html_periods = parse_raw_html_for_dates(html)
            if html_periods:
                extracted["sales_periods"].extend(html_periods)

        # 重複削除
        extracted["sales_periods"] = list(dict.fromkeys(extracted["sales_periods"]))

        # どうしても取得できない場合
        if not extracted["sales_periods"]:
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
