"""
参加表スプレッドシート（ふぁれんち管理参加表）への自動反映。

新着ライブを検知したら、スプレッドシートに設置した Google Apps Script（GAS）の
Webアプリへイベント情報を送り、対応する日付の行の
  ライブ名 / 会場 / チケ発時刻 / チケページ
を更新してもらう。行探し・書き込みはGAS側（gas/Code.gs）が行う。

必要な環境変数（GitHub Secrets）:
  SHEET_WEBHOOK_URL … GASをWebアプリとしてデプロイしたURL（.../exec）
  SHEET_TOKEN       … GASのスクリプトプロパティ TOKEN と同じ合言葉
どちらかが未設定なら何もしない（通知はこれまで通り動く）。

このファイルは LivePocket版 / TicketDive版の両リポジトリで同じ内容を使う。
"""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

SHEET_WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL")
SHEET_TOKEN = os.environ.get("SHEET_TOKEN")

JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# 日付・時刻の解析
# ---------------------------------------------------------------------------
# 2026/11/29, 2026-11-29, 2026.11.29, 2026年11月29日, 11/29, 11月29日 など
_DATE_RE = re.compile(
    r'(?:(20\d{2})\s*(?:[/.\-]|年)\s*)?(\d{1,2})\s*(?:[/.\-]|月)\s*(\d{1,2})\s*日?'
    r'(?:\s*[\(（][^\)）]{1,4}[\)）])?'
    r'(?:\s*(\d{1,2})\s*[:：]\s*(\d{2}))?'
)
_ISO_RE = re.compile(r'(20\d{2})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?')
_RANGE_SEP_RE = re.compile(r'[～〜~–—]|\s-\s')


def _today():
    return datetime.now(JST).date()


def _infer_year(month, day, base=None, prefer="future"):
    """年が書かれていない日付の年を推定する。
    prefer="future": 公演日用。base（今日）の30日前以降で一番近い日にする
    prefer="past"  : 販売開始用。base（公演日）以前で一番近い日にする
    """
    base = base or _today()
    years = (base.year - 1, base.year, base.year + 1)
    if prefer == "past":
        years = tuple(reversed(years))
    for y in years:
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if prefer == "future" and d >= base - timedelta(days=30):
            return y
        if prefer == "past" and d <= base:
            return y
    return base.year


def _iter_datetimes(text):
    """テキスト中の日付(+時刻)を (位置, 年 or None, 月, 日, 時 or None, 分 or None) で順に返す"""
    text = text or ""
    for m in _ISO_RE.finditer(text):
        y, mo, d, hh, mm = m.groups()
        yield m.start(), int(y), int(mo), int(d), (int(hh) if hh else None), (int(mm) if mm else None)
    for m in _DATE_RE.finditer(text):
        y, mo, d, hh, mm = m.groups()
        # ISO形式（2026-11-29）は上で処理済みなので、ハイフン区切りは年付きのみ受け付ける
        if "-" in m.group(0) and not y:
            continue
        if _ISO_RE.match(text, m.start()):
            continue
        mo, d = int(mo), int(d)
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        yield m.start(), (int(y) if y else None), mo, d, (int(hh) if hh else None), (int(mm) if mm else None)


def parse_event_date(text):
    """公演日の文字列（例: '2026/11/29(日) OPEN 10:00'）から date を返す。読めなければ None"""
    best = None
    for pos, y, mo, d, _, _ in _iter_datetimes(text):
        if best is None or pos < best[0]:
            best = (pos, y, mo, d)
    if not best:
        return None
    _, y, mo, d = best
    y = y or _infer_year(mo, d)
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_release_at(periods, event_date=None):
    """販売期間（文字列 or 文字列リスト）から、一番早い販売開始日時を datetime(JST) で返す。

    例: '2026/11/13(木) 21:00 ～ 2026/11/28(金) 23:59 / 2026/11/20 12:00 ～ ...'
        → 2026-11-13 21:00
    「～ 11/28」のように開始が書かれていない区間は無視する。
    """
    if isinstance(periods, str):
        segments = re.split(r'\s+/\s+|\n', periods)
    else:
        segments = []
        for p in periods or []:
            segments += re.split(r'\s+/\s+|\n', str(p))

    starts = []
    for seg in segments:
        sep = _RANGE_SEP_RE.search(seg)
        head = seg[:sep.start()] if sep else seg
        found = list(_iter_datetimes(head))
        if not found:
            continue
        # 区間の前半にある最後の日時を開始とみなす（「先行 11/13 21:00 ～」など）
        _, y, mo, d, hh, mm = sorted(found)[-1]
        if hh is None:
            continue  # 時刻の無い日付は販売開始とみなさない（公演日の誤検出防止）
        if not y:
            y = _infer_year(mo, d, base=event_date, prefer="past") if event_date else _infer_year(mo, d)
        try:
            starts.append(datetime(y, mo, d, hh, mm, tzinfo=JST))
        except ValueError:
            continue
    return min(starts) if starts else None


# ---------------------------------------------------------------------------
# 会場・公演日の抽出（ページのテキスト / 埋め込みJSONから）
# ---------------------------------------------------------------------------
VENUE_LABELS = ["会場名", "会場", "開催場所", "開催会場", "場所", "ライブ会場", "Venue", "VENUE", "venue"]
DATE_LABELS = ["開催日時", "公演日時", "開催日", "公演日", "日時", "日程", "開催期間"]
_VENUE_NOISE_RE = re.compile(r'\s*(?:Google\s*マップ.*|地図(?:を|で).*|MAP\s*$|アクセス.*)$', re.IGNORECASE)
_ADDRESS_RE = re.compile(r'(?:〒|東京都|北海道|(?:大阪|京都)府|..?.?県|\d+-\d+)')
_SALES_WORDS_RE = re.compile(r'販売|発売|受付|申込|申し込み|締切|締め切り|期限|入金|支払|抽選|先着')


def clean_lines(text):
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r'[ \t　\xa0]+', ' ', raw).strip()
        if line:
            lines.append(line)
    return lines


def _label_value(lines, labels, max_len=80):
    """「ラベル 値」「ラベル：値」「ラベル\\n値」の値を返す（ラベルの完全一致のみ）"""
    for i, line in enumerate(lines):
        for label in labels:
            if not line.startswith(label):
                continue
            rest = line[len(label):]
            if rest and not re.match(r'^[\s:：|｜]', rest):
                continue  # 「会場では～」のような本文は除外
            rest = rest.lstrip(" :：|｜").strip()
            value = rest or (lines[i + 1] if i + 1 < len(lines) else "")
            if value and len(value) <= max_len:
                return value
    return ""


def clean_venue(v):
    v = re.sub(r'\s+', ' ', str(v or "")).strip()
    v = _VENUE_NOISE_RE.sub("", v).strip()
    # 「渋谷RING（東京都渋谷区...）」→「渋谷RING」
    m = re.match(r'^(.+?)\s*[\(（](.+)[\)）]\s*$', v)
    if m and _ADDRESS_RE.search(m.group(2)):
        v = m.group(1).strip()
    # 「渋谷RING 東京都渋谷区...」→「渋谷RING」
    m = re.match(r'^(.+?)\s+(?:〒|東京都|北海道|大阪府|京都府|\S{2,3}県)', v)
    if m:
        v = m.group(1).strip()
    if _DATE_RE.fullmatch(v) or len(v) > 50:
        return ""
    return v


def venue_from_lines(lines):
    v = clean_venue(_label_value(lines, VENUE_LABELS))
    if v and not parse_event_date(v):
        return v
    return ""


def event_date_from_lines(lines):
    """ラベル付きの公演日 → ラベル無しなら販売系の語を含まない最初の日付"""
    v = _label_value(lines, DATE_LABELS, max_len=150)
    d = parse_event_date(v) if v else None
    if d:
        return d
    for line in lines:
        if len(line) < 120 and not _SALES_WORDS_RE.search(line):
            d = parse_event_date(line)
            if d:
                return d
    return None


def _iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


_JSON_VENUE_KEYS = ("venueName", "venue_name", "placeName", "place_name", "hallName",
                    "hall_name", "locationName", "location_name", "venue", "place", "location")
_JSON_DATE_KEYS = ("eventDate", "event_date", "performanceDate", "performance_date", "heldAt",
                   "held_at", "openAt", "open_at", "startDate", "start_date", "eventStartAt",
                   "event_start_at", "startAt", "start_at", "date")


def _json_blobs(html):
    soup = BeautifulSoup(html or "", "html.parser")
    blobs = []
    for s in soup.find_all("script"):
        t = (s.get("type") or "").lower()
        if s.get("id") == "__NEXT_DATA__" or "ld+json" in t:
            try:
                blobs.append(json.loads(s.string or s.get_text()))
            except Exception:
                pass
    return blobs


def venue_from_html(html):
    """埋め込みJSON（JSON-LD / Next.jsの__NEXT_DATA__）から会場名を探す"""
    for blob in _json_blobs(html):
        for node in _iter_dicts(blob):
            for key in _JSON_VENUE_KEYS:
                val = node.get(key)
                if isinstance(val, dict):
                    val = val.get("name") or val.get("title")
                if isinstance(val, str):
                    v = clean_venue(val)
                    if v and not v.startswith("http"):
                        return v
    return ""


def event_date_from_html(html):
    for blob in _json_blobs(html):
        for node in _iter_dicts(blob):
            # チケット（販売期間を持つ要素）の日時は公演日ではないので除外
            if any("sales" in k.lower() for k in node.keys()):
                continue
            for key in _JSON_DATE_KEYS:
                val = node.get(key)
                if isinstance(val, str):
                    m = _ISO_RE.search(val)
                    if m:
                        try:
                            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                            if dt.tzinfo:
                                dt = dt.astimezone(JST)
                            return dt.date()
                        except Exception:
                            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


# ---------------------------------------------------------------------------
# スプレッドシートへの送信
# ---------------------------------------------------------------------------
def sheet_enabled():
    return bool(SHEET_WEBHOOK_URL and SHEET_TOKEN)


def sync_events_to_sheet(events, source):
    """
    events: [{"title", "url", "event_date": date|None, "venue", "release_at": datetime|None}, ...]
    各イベントに e["sheet_status"]（通知に載せる一言）を書き込んで返す。
    失敗しても例外は投げない（通知を止めないため）。
    """
    if not events:
        return events
    if not sheet_enabled():
        print("[INFO] SHEET_WEBHOOK_URL / SHEET_TOKEN が未設定のため、スプシ反映をスキップします。")
        return events

    payload_events = []
    for e in events:
        d = e.get("event_date")
        r = e.get("release_at")
        payload_events.append({
            "title": e.get("title") or "",
            "url": e.get("url") or "",
            "date": d.isoformat() if d else "",
            "venue": e.get("venue") or "",
            "release_at": r.strftime("%Y-%m-%dT%H:%M") if r else "",
            "source": source,
        })

    for e, p in zip(events, payload_events):
        print(f"[SHEET] 送信: {p}")

    try:
        # GASのWebアプリは302で結果URLへリダイレクトする。requestsはPOST→GETに切り替えて追従する
        res = requests.post(
            SHEET_WEBHOOK_URL,
            data=json.dumps({"token": SHEET_TOKEN, "events": payload_events}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        print(f"[SHEET] レスポンス {res.status_code}: {res.text[:500]}")
        data = res.json()
    except Exception as ex:
        print(f"[ERROR] スプシ反映に失敗: {ex}")
        for e in events:
            e["sheet_status"] = "⚠ スプシ未反映（送信エラー）"
        return events

    if not data.get("ok"):
        msg = data.get("error", "不明なエラー")
        print(f"[ERROR] スプシ反映に失敗: {msg}")
        for e in events:
            e["sheet_status"] = f"⚠ スプシ未反映（{msg}）"
        return events

    results = data.get("results") or []
    for i, e in enumerate(events):
        r = results[i] if i < len(results) else {}
        e["sheet_status"] = ("📝 スプシ: " if r.get("ok") else "⚠ スプシ未反映: ") + (r.get("message") or "結果なし")
    return events
