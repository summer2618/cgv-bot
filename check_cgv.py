
import os
import json
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

movie_no = os.environ["MOVIE_IDS"].split(",")[0].split(":")[0].strip()
today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")

params = urllib.parse.urlencode({
    "coCd": "A420",
    "movNo": movie_no,
    "scnYmd": today,
})

url = (
    "https://cgv.co.kr/api/v1/content/cgvct/"
    "searchMovDtlMain?" + params
)

def find_booking_status(obj):
    if isinstance(obj, dict):
        if "atktPsblYn" in obj:
            return obj["atktPsblYn"]
        for value in obj.values():
            result = find_booking_status(value)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_booking_status(item)
            if result is not None:
                return result
    return None

request = urllib.request.Request(
    url,
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
)

with urllib.request.urlopen(request, timeout=20) as response:
    print("HTTP 상태:", response.status)
    data = json.load(response)

status = find_booking_status(data)

print("영화번호:", movie_no)
print("예매 가능 여부:", status)

if status not in ("Y", "N"):
    raise RuntimeError("예매 상태를 확인하지 못했습니다.")

print("CGV 조회 테스트 성공")
