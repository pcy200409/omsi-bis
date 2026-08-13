# OMSI BIS — 공유 버스정보시스템

여러 유저가 각자 OMSI 2를 켜면, 각자의 버스가 하나의 웹 관제지도(BIS 노선 스트립)에
실시간으로 표시됩니다. 마커는 OMSI 자신의 운행 스케줄(다음 정류장 + 거리)을 읽어 배치합니다.

```
유저들 OMSI 클라(C#) ──POST──▶ 서버(FastAPI+WS) ──WS──▶ 관제 웹(브라우저)
```

## 구성
- `server/` — FastAPI 서버(중계 + 웹/노선 서빙). 배포 대상.
- `web/` — 관제 화면(index.html). 서버가 같이 서빙.
- `data/` — 노선/정류장 JSON(`route_124A/B.json`, `routes.json`, `kname_overrides.json`).
- `client/` — OMSI 위치를 읽어 서버로 보내는 C# 클라이언트(관리자 권한 필요).

## 로컬 실행
```bash
cd server
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m uvicorn app:app --host 127.0.0.1 --port 8000
# 브라우저: http://127.0.0.1:8000
```
클라이언트: `client/publish/OmsiBisClient.exe` **더블클릭**(관리자 UAC 승인) →
창에서 서버주소·닉네임·노선·맵 입력 후 **[시작]**. OMSI(맵+버스 탑승) 실행 중이어야 함.
개발 PC에선 창의 **[로컬 서버 켜기]** 로 `127.0.0.1:8000` 서버도 버튼으로 띄울 수 있음.

## 클라우드 배포 (Render 무료)
1. 이 저장소를 GitHub에 올린다.
2. Render(https://render.com) 가입 → **New + → Blueprint** → 이 저장소 선택.
   `render.yaml`을 읽어 자동 설정된다. (또는 New Web Service 수동:
   Root=`server`, Build=`pip install -r requirements.txt`,
   Start=`uvicorn app:app --host 0.0.0.0 --port $PORT`, 환경변수 `BIS_READONLY=1`.)
3. 배포되면 공개주소(`https://<이름>.onrender.com`)가 나온다 → 친구들에게 공유.
4. 친구들에게 `client/publish/` 폴더째 전달(.NET 설치 불필요). 각자 `OmsiBisClient.exe`
   더블클릭 → 서버주소에 `https://<이름>.onrender.com` 넣고 닉네임 입력 → [시작].

**클라우드는 보기 전용**(`BIS_READONLY=1`): 정류장명 편집은 OMSI 맵이 있는 로컬에서만 하고,
바뀐 `data/*.json`을 커밋/푸시하면 클라우드에 반영된다.

## 부하 특성
클라는 스케줄 기반이라 **~2Hz**만 보내도 화면이 부드럽다(초당 10회 불필요).
`server/loadtest.py`로 가짜 운전자 N명을 시뮬레이션해 서버 CPU/지연을 측정할 수 있다:
```bash
python loadtest.py <port> <n_drivers> <hz> <seconds>
```
측정 결과 2Hz에서 무료 Render로 친구 규모(~10명)는 안전.
