FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py watch_showtime.py .

# 상태 파일을 컨테이너 재시작 후에도 유지하려면 볼륨을 마운트하세요:
#   docker run -v $(pwd)/state.json:/app/state.json ...
# 어떤 스크립트를 돌릴지는 `docker run` 에서 CMD를 덮어써서 지정하세요:
#   docker run ... cgv-ticket-alert python main.py
#   docker run ... cgv-ticket-alert python watch_showtime.py
CMD ["python", "main.py"]
