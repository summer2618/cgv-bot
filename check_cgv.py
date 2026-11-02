
import os
from playwright.sync_api import sync_playwright

movie_ids = os.environ["MOVIE_IDS"]
movie_no = movie_ids.split(",")[0].split(":")[0].strip()

url = f"https://cgv.co.kr/cnm/cgvChart/movieChart/{movie_no}"
api_path = "/api/v1/content/cgvct/searchMovDtlMain"

print("CGV 영화번호:", movie_no)
print("조회 주소:", url)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(locale="ko-KR")

    try:
        with page.expect_response(
            lambda response: api_path in response.url,
            timeout=30000
        ) as response_info:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000
            )

        response = response_info.value
        print("HTTP 상태:", response.status)

        body = response.json()
        print("API 상태:", body.get("statusCode"))

        data = body.get("data") or {}
        print("영화명:", data.get("movNm"))
        print("예매 가능 여부:", data.get("atktPsblYn"))

        if data.get("atktPsblYn") not in ("Y", "N"):
            raise RuntimeError("예매 상태를 정상적으로 읽지 못했습니다.")

        print("CGV 조회 테스트 성공")

    finally:
        browser.close()
