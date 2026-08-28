# CGV 예매 오픈 알림봇

CGV 예매 상태를 주기적으로 확인해서 Discord로 알림을 보내는 봇입니다. 스크립트 두 개로 구성되어 있고, 목적이 다릅니다.

| 스크립트 | 감지 대상 | 언제 쓰나 |
|---|---|---|
| `main.py` | 영화 자체의 예매 오픈 여부 (N→Y) | 아직 예매 시작 전인 영화의 "예매 오픈 순간"을 기다릴 때 |
| `watch_showtime.py` | 특정 영화관 + 특정 상영관 타입(IMAX 등)의 신규 상영일자 오픈 **+** 임박한 회차의 잔여좌석 증가(취소표) | 이미 예매는 열려있지만, IMAX처럼 인기 상영관이 항상 매진이라 "다음 주 회차가 새로 열리는 순간" 또는 "취소표가 풀리는 순간"을 노려야 할 때 |

두 스크립트 모두 **알림만 보냅니다.** 좌석 선택, 로그인, 결제는 자동화하지 않습니다 — CGV 로그인 비밀번호나 결제
정보를 대신 입력하는 기능은 없고, 알림을 받은 사람이 직접 빠르게 예매해야 합니다.

CGV 사이트는 Cloudflare 봇 차단이 걸려 있어 단순 HTTP 요청(curl/requests)으로는 데이터를 못 가져옵니다. 그래서
Playwright로 실제 브라우저(헤드리스 크로미움)를 띄워 CGV가 자체적으로 호출하는 API 응답을 읽는 방식으로 동작합니다.

## 1. 공통 준비

### movNo 찾기 (영화 번호)
1. https://cgv.co.kr 에서 원하는 영화 상세 페이지로 이동
2. 주소창 URL 확인: `https://cgv.co.kr/cnm/cgvChart/movieChart/30001350`
3. 마지막 숫자(`30001350`)가 movNo

### Discord 웹훅 만들기
1. Discord에서 알림 받을 채널 → 채널 설정 → 연동(Integrations) → 웹후크(Webhooks) → 새 웹후크
2. "웹후크 URL 복사" 클릭

### 설치
```bash
cd cgv-ticket-alert
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium --with-deps

cp .env.example .env
```

## 2. main.py — 영화 예매 오픈 감지

`.env`에서 `DISCORD_WEBHOOK_URL`, `MOVIE_IDS` (형식: `movNo:표시이름`, 콤마 구분)를 채운 뒤:

```bash
python main.py --test   # 웹훅 연결 테스트
python main.py          # 모니터링 시작 (Ctrl+C 종료)
```

| 변수 | 설명 |
|---|---|
| `MOVIE_IDS` | `movNo:표시이름` 콤마 구분. 예: `30001350:경주기행,30001234` |
| `POLL_INTERVAL_SECONDS` | 확인 주기(초). 기본 120초 |
| `NOTIFY_IF_ALREADY_OPEN` | 최초 실행 시 이미 열려있으면 알림 보낼지 (기본 false) |
| `STATE_FILE` | 상태 저장 파일 경로 |

## 3. watch_showtime.py — 특정 극장·상영관의 신규 회차 오픈 감지

**siteNo(극장 코드) 찾기** — movNo와 극장 이름으로 검색:
```bash
python watch_showtime.py --find-site 30001323 용산아이파크몰
# -> siteNo=0013  siteNm=용산아이파크몰
```

`.env`에서 `SHOWTIME_WATCHES`를 채웁니다. 형식은 `라벨|movNo|siteNo|attrCd` (세미콜론으로 여러 개 구분,
attrCd 생략 시 전체 상영관 타입 대상):

```
SHOWTIME_WATCHES=용산아이파크몰 IMAX 오디세이|30001323|0013|04
```

상영관 타입 코드(attrCd): `IMAX=04`, `SCREENX=08`, `4DX=03`, `ULTRA 4DX=09`, `DOLBY ATMOS=06`

```bash
python watch_showtime.py --test   # 웹훅 연결 테스트
python watch_showtime.py          # 모니터링 시작 (Ctrl+C 종료)
```

이 스크립트는 두 가지 신호를 동시에 감지합니다.

1. **신규 날짜/회차 오픈**: 봇을 켠 시점에 열려있는 상영 날짜 목록을 기준선으로 저장해두고, 이후 폴링마다
   새로 추가된 날짜가 있는지 비교합니다. 새 날짜가 나타나면 즉시 알림 (감지 범위: 열려있는 모든 날짜).
2. **잔여좌석 증가(취소표 발생 등)**: 오늘부터 `SHOWTIME_SEAT_CHECK_DAYS_AHEAD`일 이내의 회차들은 잔여좌석
   수까지 추적해서, 직전 확인 대비 `SHOWTIME_MIN_SEAT_INCREASE`석 이상 늘어나면 즉시 알림. (전체 날짜가
   아니라 가까운 날짜만 보는 이유: 열려있는 날짜마다 별도 API 호출이 필요해서, 전체를 다 보면 폴링마다
   요청이 너무 많아짐 — 취소표는 어차피 임박한 회차에서 잡아야 의미가 있어서 범위를 좁혔습니다)

알림엔 새로 열린 날짜 목록 또는 회차·시간·잔여좌석 변화 + 예매 페이지 링크가 같이 옵니다. 최초 실행 시엔
기준선 저장만 하고 알림은 보내지 않습니다.

| 변수 | 설명 |
|---|---|
| `SHOWTIME_WATCHES` | `라벨\|movNo\|siteNo\|attrCd` 세미콜론 구분 |
| `SHOWTIME_POLL_INTERVAL_SECONDS` | 확인 주기(초). 기본 15초 |
| `SHOWTIME_MIN_SEAT_INCREASE` | 잔여좌석이 몇 석 이상 늘어나면 알림 보낼지. 기본 2석 |
| `SHOWTIME_SEAT_CHECK_DAYS_AHEAD` | 좌석 상세까지 체크할 범위(오늘부터 며칠 이내). 기본 3일 |
| `SHOWTIME_STATE_FILE` | 상태 저장 파일 경로 |
| `COOKIE_REWARM_EVERY_CYCLES` | 몇 사이클마다 세션(쿠키)을 새로 고칠지. 기본 100 |

> ⚠️ 15초 주기는 CGV/Cloudflare에 상대적으로 부하를 더 주는 설정이에요. "1초 만에 사라지는 걸 100% 잡는다"는
> 애초에 사람이 클릭해서 예매하는 속도(수 초~수십 초) 때문에 불가능한 목표이니, 지나치게 짧게(예: 5초 미만)
> 잡지는 마세요 — 차단 위험만 커지고 실효는 크지 않습니다.

## 4. 클라우드 서버에 배포하기 (Docker)

`Dockerfile`의 `CMD`를 원하는 스크립트로 바꾸거나, 두 봇을 각각 별도 컨테이너로 띄우세요.

```bash
docker build -t cgv-ticket-alert .

# main.py 용 컨테이너
docker run -d --name cgv-movie-alert \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/state.json:/app/state.json \
  cgv-ticket-alert python main.py

# watch_showtime.py 용 컨테이너
docker run -d --name cgv-showtime-alert \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/showtime_state.json:/app/showtime_state.json \
  cgv-ticket-alert python watch_showtime.py
```

- `--restart unless-stopped`: 서버 재부팅 시 자동 재시작
- `-v .../*.json`: 컨테이너를 재생성해도 상태를 기억해서 중복 알림 방지
- 로그 확인: `docker logs -f cgv-showtime-alert`

AWS EC2, GCP Compute Engine, Oracle Cloud Free Tier 등 Docker가 돌아가는 아무 VM에 올리면 됩니다.
메모리는 헤드리스 크로미움 기준 컨테이너당 최소 512MB~1GB 권장.

## 주의사항

- 개인적으로 예매/회차 오픈 여부를 확인하기 위한 용도입니다. 폴링 주기를 너무 짧게 설정해 CGV 서버에
  과도한 부하를 주지 않도록 해주세요 (`main.py` 60초 이상, `watch_showtime.py` 60초 이상 권장).
- CGV의 페이지 구조나 API 응답 형식이 바뀌면 동작하지 않을 수 있습니다. 이 경우 각 스크립트의 API 경로/필드
  파싱 로직을 다시 확인해야 합니다.
- 매크로/자동 예매(자동 결제, 자동 좌석 선점, 자동 로그인 등)는 포함되어 있지 않습니다. 이 봇들은 "오픈 감지
  알림"까지만 수행하며, 실제 예매는 알림을 받은 사람이 직접 진행합니다.
