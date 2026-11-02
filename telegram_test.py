
import os
import urllib.parse
import urllib.request

token = os.environ["TELEGRAM_BOT_TOKEN"]
chat_id = os.environ["TELEGRAM_CHAT_ID"]

message = "🎬 CGV 예매 알림봇 연결 테스트 성공!"

data = urllib.parse.urlencode({
    "chat_id": chat_id,
    "text": message
}).encode("utf-8")

url = f"https://api.telegram.org/bot{token}/sendMessage"

with urllib.request.urlopen(
    urllib.request.Request(url, data=data),
    timeout=15
) as response:
    print("텔레그램 테스트 전송 성공:", response.status)
    
