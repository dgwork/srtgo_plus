# SRTgo Plus: K-Train (KTX, SRT) Reservation Assistant

> [!NOTE]
> 원본 [srtgo](https://github.com/lapis42/srtgo) 프로젝트가 2025년 9월 개발 중단됨에 따라, 본 저장소에서 이어서 유지보수 및 개발을 진행하고 있습니다.
> 코레일(KTX)의 anti-bot 정책 변경에 대응하여 정상 동작하도록 수정하였습니다.

> [!WARNING]
> 본 프로그램의 모든 상업적, 영리적 이용을 엄격히 금지합니다. 개인적인 승차권 예매 용도로만 사용해 주세요. 본 프로그램 사용에 따른 민형사상 책임을 포함한 모든 책임은 사용자에게 있으며, 개발자는 어떠한 책임도 부담하지 않습니다. 본 프로그램을 내려받음으로써 위 사항에 동의하는 것으로 간주됩니다.

---

## 설치

```bash
pip install git+https://github.com/DionNam/srtgo_plus.git
```

## 사용법

터미널에서 아래 명령어를 실행하면 됩니다.

```bash
srtgo
```

### 메뉴 구성

```
[?] 메뉴 선택:
  예매 시작
  예매 확인/결제/취소
  로그인 설정
  텔레그램 설정
  카드 설정
  역 설정
  역 직접 수정
  예매 옵션 설정
  나가기
```

### 1. 로그인 설정

처음 사용 시 **로그인 설정**에서 SRT 또는 KTX 계정을 등록합니다.

- 멤버십 번호, 이메일, 전화번호 중 하나로 로그인 가능
- 로그인 정보는 시스템 키체인에 안전하게 저장됩니다

### 2. 예매 시작

1. **SRT** 또는 **KTX** 선택
2. 출발역, 도착역, 날짜, 시간, 승객수 선택
3. 검색된 열차 목록에서 원하는 열차 선택 (복수 선택 가능)
4. 좌석 유형 선택 (일반실 우선 / 일반실만 / 특실 우선 / 특실만)
5. 매진 시 자동으로 빈 자리가 날 때까지 재시도

### 3. 텔레그램 알림 (선택)

텔레그램 봇을 연동하면 예매 성공 시 알림을 받을 수 있습니다.

1. [@BotFather](https://t.me/BotFather)에서 봇 생성 후 토큰 발급
2. **텔레그램 설정** 메뉴에서 토큰과 chat_id 입력

### 4. 카드 결제 (선택)

카드 정보를 미리 등록하면 예매와 동시에 자동 결제가 가능합니다.

## 좌석 감시 (srtgo-watch)

예매는 하지 않고, **지정한 구간·시간대에 자리가 나는지만 주기적으로 확인**하고 싶을 때 쓰는
비대화형 명령입니다. 터미널을 띄워두거나 서버·cron 에 걸어두면 됩니다.

```bash
# 9/23 12:00 ~ 9/24 12:00 사이 서울 → 포항 KTX 를 60초마다 확인
srtgo-watch --rail KTX --dep 서울 --arr 포항 \
    --start "2026-09-23 12:00" --end "2026-09-24 12:00" \
    --interval 60 --telegram
```

```bash
# 수서발 SRT 도 같이 보기 (SRT 는 서울역에 서지 않으므로 자동으로 수서로 바뀝니다)
srtgo-watch --rail KTX --rail SRT --dep 서울 --arr 포항 \
    --start "09-23 12:00" --end "09-24 12:00" --include-standby
```

```bash
# cron 용: 한 번만 확인하고 종료
*/5 * * * * srtgo-watch --dep 서울 --arr 포항 --start "09-23 12:00" --end "09-24 12:00" --once --telegram
```

### 주요 옵션

| 옵션 | 설명 |
| --- | --- |
| `--rail KTX\|SRT` | 감시할 철도사. 여러 번 지정 가능 |
| `--dep` / `--arr` | 출발역 / 도착역 |
| `--srt-dep` / `--srt-arr` | SRT 쪽 역만 따로 지정 (기본은 `--dep`/`--arr`, 수도권이면 수서로 대체) |
| `--start` / `--end` | 감시할 **출발 시각** 범위. `"2026-09-23 12:00"`, `"09-23 12:00"`, `"23 14"` 모두 가능 |
| `--passengers` | 성인 승객 수 (기본 1) |
| `--seat-type any\|general\|special` | 어떤 좌석이 열릴 때 알릴지 |
| `--include-standby` | 예약대기 신청 가능도 '자리 있음' 으로 간주 |
| `--ktx-only` | KTX 등급만 조회 (무궁화·ITX 제외) |
| `--interval` | 조회 간격(초). 최소 10초, 실제로는 약간의 지터가 붙습니다 |
| `--once` | 1회만 확인하고 종료 (cron 용) |
| `--telegram` | 텔레그램으로 알림 전송 |
| `--exec "명령"` | 자리가 났을 때 실행할 셸 명령. 알림 내용이 `$SRTGO_MESSAGE` 로 전달됩니다 |
| `--repeat-notify N` | 자리가 계속 남아 있으면 N분마다 재알림 (기본 0 = 상태가 바뀔 때만) |

알림은 **매진 → 예매가능 으로 바뀌는 순간에만** 나가므로 같은 열차로 반복해서 울리지 않습니다.
다시 매진됐다가 또 풀리면 그때 한 번 더 알립니다.

### 로그인 정보

`srtgo` 의 **로그인 설정**으로 저장한 키체인 값을 그대로 씁니다. 키체인이 없는 서버라면
환경변수로 줄 수 있습니다.

```bash
export KTX_ID=... KTX_PASS=...
export SRT_ID=... SRT_PASS=...
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...   # --telegram 사용 시
```

> [!NOTE]
> 서울역에서 포항으로 가는 열차는 **KTX(코레일)** 만 있습니다. SRT 는 수서역에서 출발하며
> 동해선(포항) 편성이 따로 있으니, 두 역 모두 열어두고 싶다면 `--rail KTX --rail SRT` 로
> 함께 감시하세요.

## 요구사항

- Python 3.10 이상
- 의존 패키지: `click`, `curl_cffi`, `requests`, `inquirer`, `keyring`, `PyCryptodome`, `prompt_toolkit`, `python-telegram-bot`, `termcolor`

## 변경사항 (원본 대비)

- KTX(코레일) anti-bot 대응: `x-dynapath-m-token` 생성 엔진 구현
- KTX MACRO ERROR 에러 핸들링 개선
- 예매 간격 파라미터 안전성 조정

## Credits

- 원본 프로젝트: [srtgo](https://github.com/lapis42/srtgo) by lapis42
- SRT 모듈: [SRT](https://github.com/ryanking13/SRT) by ryanking13 (MIT License)
- KTX 모듈: [korail2](https://github.com/carpedm20/korail2) by carpedm20 (BSD License)
- Anti-bot bypass 참고: [korail2 PR #54](https://github.com/carpedm20/korail2/pull/54) by dhfhfk
