# Falench.-ticketdive-bot
## スプレッドシート自動更新（ふぁれんち管理参加表）

新着通知のときに参加表の該当日の行へ イベント名 / 会場 / チケ発時刻 / チケページ を書き込みます。
スプレッドシート側（Apps Script）の設定と手順は LivePocket版リポジトリ
（kiisaan/Falench.-LivePocket-watcher）の README「スプレッドシート自動更新」と `gas/Code.gs` を参照してください。
このリポジトリには Secrets `SHEET_WEBHOOK_URL` と `SHEET_TOKEN`（LivePocket版と同じ値）を追加すれば動きます。
`sheet_sync.py` は両リポジトリで同じ内容です。
