import os
import requests

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def get_bot_info():
    # Bot自体の情報を取得してアクセストークンが正しいか確認
    url = "https://api.line.me/v2/bot/info"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    res = requests.get(url, headers=headers)
    print("--- [1] アクセストークン確認 ---")
    print(f"ステータス: {res.status_code}")
    print(f"レスポンス: {res.text}\n")

if __name__ == "__main__":
    get_bot_info()
