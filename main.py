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
    """日付・時刻表現を「MM/DD（曜日）HH:MM」または「MM/DD（曜日）」等に整形"""
    if not dt_input:
        return None

    dt_str = str(dt_input).strip().replace("\\", "").replace('"', "")

    # ISO 8601 または タイムスタンプ処理
    try:
        clean_iso = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）{dt.hour:02d}:{dt.minute:02d}"
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
            dt = datetime(year, month, day)
            weekdays = ["月", "火", "水", "木", "金", "土", "日"]
            return f"{dt.month:02d}/{dt.day:02d}（{weekdays[dt.weekday()]}）"
        except Exception:
            pass

    return dt_str


def extract_periods_from_dict(data):
    """JSON辞書オブジェクトを深く走査してチケットの販売期間を取得"""
    periods = []

    def walk(obj):
        if isinstance(obj, dict):
            # TicketDiveのJSON構造におけるキー名を広範囲にカバー
            s_start = (
                obj.get("salesStartAt")
                or obj.get("sales_start_at")
                or obj.get("salesStart")
                or obj.get("validFrom")
            )
            s_end = (
                obj.get("salesEndAt")
                or obj.get("sales_end_at")
                or obj.get("salesEnd")
                or obj.get("validThrough")
            )
            t_name = obj.get("name") or obj.get("title") or ""

            if s_start and s_end:
                fmt_s = format_dt(s_start)
                fmt_e = format_dt(s_end)
                if fmt_s and fmt_e:
                    prefix = f"【{t_name}】" if t_name and len(str(t_name)) < 25 else ""
                    period = f"{prefix}{fmt_s} ～ {fmt_e}"
                    if period not in periods:
                        periods.append(period)

            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return periods


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

        # 3. Next.jsの埋め込みデータ（__NEXT_DATA__）から最優先で抽出
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if next_data_script and next_data_script.string:
            try:
                next_json = json.loads(next_data_script.string)
                periods = extract_periods_from_dict(next_json)
                if periods:
                    extracted["sales_periods"] = periods

                # 公演日の抽出補完
                if extracted["event_date"] == "情報なし":
                    event_date_m = re.search(
                        r'"(?:eventDate|date)"\s*:\s*"([^"]+)"',
                        next_data_script.string,
                    )
                    if event_date_m:
                        extracted["event_date"] = format_dt(
                            event_date_m.group(1)
                        )
            except Exception as e:
                print(f"__NEXT_DATA__ 解析エラー ({event_url}): {e}")

        # 4. JSON-LD スクリプトタグからの補完抽出
        if not extracted["sales_periods"]:
            json_ld_scripts = soup.find_all("script", type="application/ld+json")
            for script in json_ld_scripts:
                if script.string:
                    try:
                        ld_json = json.loads(script.string)
                        periods = extract_periods_from_dict(ld_json)
                        if periods:
                            extracted["sales_periods"].extend(periods)
                    except Exception:
                        pass

        # 5. 正規表現による文字列検索フォールバック
        if not extracted["sales_periods"]:
            sales_m = re.findall(
                r"(\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2}\s*～\s*\d{1,2}/\d{1,2}\s*(?:[\(（].+?[\)）])?\s*\d{1,2}:\d{2})",
                html,
            )
            if sales_m:
                extracted["sales_periods"] = list(set(sales_m))

        # 最終フォールバック
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
