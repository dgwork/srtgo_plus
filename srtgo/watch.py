"""좌석 감시(모니터링) 전용 실행기.

지정한 구간/시간대의 KTX(코레일)·SRT 열차를 주기적으로 조회해서
'예매 가능' 상태로 바뀌는 순간을 알려준다. 예매는 하지 않는다.

    srtgo-watch --rail KTX --dep 서울 --arr 포항 \
        --start "2026-09-23 12:00" --end "2026-09-24 12:00"
"""

try:
    from curl_cffi.requests.exceptions import ConnectionError
except ImportError:
    from requests.exceptions import ConnectionError

from datetime import datetime, timedelta
from json.decoder import JSONDecodeError
from random import gammavariate, uniform
from termcolor import colored
from typing import Dict, List, Optional, Tuple

import asyncio
import click
import keyring
import os
import re
import subprocess
import sys
import time

from .ktx import (
    Korail,
    KorailError,
    NeedToLoginError,
    NetFunnelError,
    NoResultsError,
    ReserveOption,
    SoldOutError,
    TrainType,
    AdultPassenger,
    ChildPassenger,
    SeniorPassenger,
)
from .srt import SRT, SRTError, SRTNetFunnelError, SeatType, Adult, Child, Senior
from .srtgo import STATIONS, get_telegram, pay_card

try:
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # tzdata 미설치 환경 - 로컬 시간을 그대로 쓴다
    KST = None

# 한 번의 스윕에서 페이지를 넘기며 조회할 최대 횟수 (무한루프 방지)
MAX_PAGES_PER_DAY = 12
# 페이지 사이 간격 (초). 연속 요청으로 anti-bot 에 걸리지 않게 둔다.
PAGE_INTERVAL = (0.7, 1.6)
# --interval 로 줄 수 있는 최소값. 서버에 부담을 주지 않기 위한 하한선.
MIN_INTERVAL = 10
# SRT 가 '조회 결과 없음' 을 에러로 돌려줄 때 쓰는 문구들 (정상 상황으로 처리한다)
SRT_EMPTY_MARKERS = ("직통열차", "조회 결과가 없", "열차가 없")

# 수도권 KTX 역 -> SRT 대체역 (SRT 는 수서에서 출발한다)
SEOUL_TO_SUSEO = {"서울", "용산", "영등포", "광명", "청량리", "행신"}

# --seat-type -> 각 철도사의 예약 옵션
SEAT_OPTION = {
    "SRT": {
        "any": SeatType.GENERAL_FIRST,
        "general": SeatType.GENERAL_ONLY,
        "special": SeatType.SPECIAL_ONLY,
    },
    "KTX": {
        "any": ReserveOption.GENERAL_FIRST,
        "general": ReserveOption.GENERAL_ONLY,
        "special": ReserveOption.SPECIAL_ONLY,
    },
}

# 예매 성공 후 결제까지 끝나면 이 코드로 종료한다
EXIT_RESERVED = 0


def _keyring_get(service: str, key: str) -> Optional[str]:
    """키체인이 없는 서버 환경에서도 죽지 않게 감싼다."""
    try:
        return keyring.get_password(service, key)
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(KST).replace(tzinfo=None) if KST else datetime.now()


def _log(msg: str, *, newline: bool = True) -> None:
    stamp = _now().strftime("%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True, end="\n" if newline else "")


def parse_when(text: str, *, end: bool = False) -> datetime:
    """'2026-09-23 12:00', '09-23 12:00', '23 12:00', '09-23' 등을 받는다."""
    text = text.strip()
    now = _now()
    m = re.fullmatch(
        r"(?:(?:(\d{4})[-/.])?(\d{1,2})[-/.])?(\d{1,2})"
        r"(?:[ T]+(\d{1,2})(?::?(\d{2}))?)?",
        text,
    )
    if not m:
        raise click.BadParameter(
            f"날짜/시각을 해석할 수 없습니다: {text!r} "
            '(예: "2026-09-23 12:00", "09-23 12:00", "23 14")'
        )
    year, month, day, hour, minute = m.groups()
    year = int(year) if year else now.year
    month = int(month) if month else now.month
    day = int(day)
    if hour is None:
        hour, minute = (23, 59) if end else (0, 0)
    else:
        hour, minute = int(hour), int(minute or 0)

    try:
        when = datetime(year, month, day, hour, minute)
    except ValueError as err:
        raise click.BadParameter(f"존재하지 않는 날짜입니다: {text!r} ({err})")

    # 연/월을 생략했는데 이미 지난 날짜라면 다음 달(또는 다음 해)로 해석한다
    cutoff = now - timedelta(days=1)
    if m.group(1) is None and when < cutoff:
        if m.group(2) is None:  # 월도 생략됨 -> 다음 달
            month, year = (month % 12) + 1, year + (1 if month == 12 else 0)
        else:  # 월은 줬는데 지난 날짜 -> 내년
            year += 1
        try:
            when = datetime(year, month, day, hour, minute)
        except ValueError as err:
            raise click.BadParameter(f"존재하지 않는 날짜입니다: {text!r} ({err})")
    return when


def _dep_datetime(train, is_srt: bool) -> datetime:
    date = train.dep_date
    dep_time = train.dep_time
    return datetime(
        int(date[0:4]),
        int(date[4:6]),
        int(date[6:8]),
        int(dep_time[0:2]),
        int(dep_time[2:4]),
    )


def _train_key(rail_type: str, train, is_srt: bool) -> Tuple[str, str, str, str]:
    number = train.train_number if is_srt else train.train_no
    return (rail_type, str(number), train.dep_date, train.dep_time)


def _available(train, seat_type: str, include_standby: bool, is_srt: bool) -> bool:
    if is_srt:
        general, special = train.general_seat_available(), train.special_seat_available()
        standby = train.reserve_standby_available()
    else:
        general, special = train.has_general_seat(), train.has_special_seat()
        standby = train.has_waiting_list()

    if seat_type == "general":
        ok = general
    elif seat_type == "special":
        ok = special
    else:
        ok = general or special
    return ok or (include_standby and standby)


class RailWatcher:
    """하나의 철도사(KTX 또는 SRT) 를 감시한다."""

    def __init__(
        self,
        rail_type: str,
        dep: str,
        arr: str,
        start: datetime,
        end: datetime,
        passengers: Dict[str, int],
        seat_type: str,
        include_standby: bool,
        ktx_only: bool,
        debug: bool,
        sessions: Dict[str, object],
    ):
        self.rail_type = rail_type
        self.is_srt = rail_type == "SRT"
        self.dep = dep
        self.arr = arr
        self.start = start
        self.end = end
        self.counts = passengers
        self.total_passengers = sum(passengers.values())
        self.seat_type = seat_type
        self.seat_option = SEAT_OPTION[rail_type][seat_type]
        self.include_standby = include_standby
        self.ktx_only = ktx_only
        self.debug = debug
        # 같은 철도사를 보는 감시자끼리 세션을 공유한다 (계정당 로그인 1회)
        self._sessions = sessions
        self.state: Dict[Tuple[str, str, str, str], bool] = {}
        # 이번 스윕에서 예매 가능한 열차 (예매 모드에서 사용)
        self.open_trains: List[object] = []

    def passenger_objects(self) -> List:
        """예약 요청에 쓸 실제 승객 객체 목록."""
        classes = (
            {"adult": Adult, "child": Child, "senior": Senior}
            if self.is_srt
            else {
                "adult": AdultPassenger,
                "child": ChildPassenger,
                "senior": SeniorPassenger,
            }
        )
        return [classes[k](n) for k, n in self.counts.items() if n > 0]

    # --- 로그인 -------------------------------------------------------
    def _credentials(self) -> Tuple[str, str]:
        env_prefix = self.rail_type
        user_id = os.environ.get(f"{env_prefix}_ID") or _keyring_get(
            self.rail_type, "id"
        )
        password = os.environ.get(f"{env_prefix}_PASS") or _keyring_get(
            self.rail_type, "pass"
        )
        if not user_id or not password:
            raise click.ClickException(
                f"{self.rail_type} 로그인 정보가 없습니다. "
                f"`srtgo` 를 실행해 '로그인 설정' 을 먼저 마치거나, "
                f"환경변수 {env_prefix}_ID / {env_prefix}_PASS 를 설정하세요."
            )
        return user_id, password

    @property
    def rail(self):
        return self._sessions.get(self.rail_type)

    @staticmethod
    def _logged_in(rail) -> bool:
        if rail is None:
            return False
        state = getattr(rail, "is_login", None)
        if state is None:
            state = getattr(rail, "logined", False)
        return bool(state)

    def login(self, force: bool = False) -> None:
        if not force and self._logged_in(self.rail):
            return
        user_id, password = self._credentials()
        cls = SRT if self.is_srt else Korail
        rail = cls(user_id, password, verbose=self.debug)
        if not self._logged_in(rail):
            raise click.ClickException(f"{self.rail_type} 로그인에 실패했습니다.")
        self._sessions[self.rail_type] = rail

    # --- 조회 ---------------------------------------------------------
    def _search_page(self, date: str, dep_time: str) -> List:
        if self.is_srt:
            try:
                return self.rail.search_train(
                    dep=self.dep,
                    arr=self.arr,
                    date=date,
                    time=dep_time,
                    passengers=[Adult(self.total_passengers)],
                    available_only=False,
                )
            except SRTError as ex:
                msg = getattr(ex, "msg", "") or ""
                if any(marker in msg for marker in SRT_EMPTY_MARKERS):
                    return []
                raise
        params = {
            "dep": self.dep,
            "arr": self.arr,
            "date": date,
            "time": dep_time,
            "passengers": [AdultPassenger(self.total_passengers)],
            "include_no_seats": True,
            "include_waiting_list": True,
        }
        if self.ktx_only:
            params["train_type"] = TrainType.KTX
        try:
            return self.rail.search_train(**params) or []
        except NoResultsError:
            return []

    def sweep(self) -> List:
        """감시 구간에 걸치는 모든 열차를 (매진 포함) 모아서 돌려준다."""
        found: Dict[Tuple[str, str, str, str], object] = {}
        day = self.start.date()
        last_day = self.end.date()
        today = _now().date()

        while day <= last_day:
            if day < today:
                day += timedelta(days=1)
                continue
            date = day.strftime("%Y%m%d")
            dep_time = (
                self.start.strftime("%H%M%S") if day == self.start.date() else "000000"
            )
            for page in range(MAX_PAGES_PER_DAY):
                trains = self._search_page(date, dep_time)
                if not trains:
                    break
                latest = dep_time
                added = False
                for train in trains:
                    latest = max(latest, train.dep_time)
                    when = _dep_datetime(train, self.is_srt)
                    if self.start <= when <= self.end:
                        key = _train_key(self.rail_type, train, self.is_srt)
                        if key not in found:
                            found[key] = train
                            added = True
                # 마지막 열차가 감시 구간을 넘어섰거나 더 나올 게 없으면 종료
                if latest <= dep_time:
                    break
                last_when = datetime(
                    int(date[0:4]),
                    int(date[4:6]),
                    int(date[6:8]),
                    int(latest[0:2]),
                    int(latest[2:4]),
                )
                if last_when >= self.end:
                    break
                if not added and page > 0:
                    break
                dep_time = (last_when + timedelta(minutes=1)).strftime("%H%M%S")
                time.sleep(uniform(*PAGE_INTERVAL))
            day += timedelta(days=1)

        return [found[k] for k in sorted(found, key=lambda k: (k[2], k[3]))]

    def check(self) -> Tuple[List[str], List[str], List[str]]:
        """(새로 열린 열차, 지금 예매 가능한 열차, 전체 현황) 메시지를 돌려준다."""
        trains = self.sweep()
        newly, open_now, snapshot = [], [], []
        self.open_trains = []
        for train in trains:
            key = _train_key(self.rail_type, train, self.is_srt)
            ok = _available(train, self.seat_type, self.include_standby, self.is_srt)
            line = f"[{self.rail_type}] {train}"
            snapshot.append(line)
            if ok:
                open_now.append(line)
                self.open_trains.append(train)
                if not self.state.get(key, False):
                    newly.append(line)
            self.state[key] = ok
        return newly, open_now, snapshot

    def recover(self, ex: Exception) -> None:
        """예외 종류에 맞춰 세션을 정리하거나 다시 로그인한다."""
        msg = getattr(ex, "msg", str(ex)) or ""
        code = getattr(ex, "code", "") or ""
        if self.debug:
            _log(f"{self.rail_type} 예외: {type(ex).__name__}: {msg or ex}")

        if isinstance(ex, (NetFunnelError, SRTNetFunnelError)) or "MACRO" in f"{code}{msg}":
            self.rail.clear()
            return
        if isinstance(ex, NeedToLoginError) or "Need to Login" in msg or "로그인 후 사용" in msg:
            self.login(force=True)
            return
        if isinstance(ex, (ConnectionError, JSONDecodeError)):
            self.login(force=True)
            return
        if isinstance(ex, (KorailError, SRTError)):
            # 잔여석 없음류는 정상 상태이므로 조용히 넘어간다
            if any(
                k in msg
                for k in ("Sold out", "잔여석없음", "사용자가 많아", "예약대기자한도수초과")
            ):
                return
            self.rail.clear()
            return
        self.login(force=True)


def _try_reserve(candidates, pay: bool, dry_run: bool) -> Optional[str]:
    """예매 가능한 열차를 순서대로 시도한다. 성공하면 결과 메시지를 돌려준다.

    결제는 srtgo 가 이미 제공하는 pay_card() 를 그대로 쓴다 (--pay 를 준 경우에만).
    """
    for watcher, train in candidates:
        label = f"[{watcher.rail_type}] {train}"
        if dry_run:
            _log(colored(f"[DRY-RUN] 예매를 시도했을 열차: {label}", "cyan"))
            return None
        _log(f"예매 시도: {label}")
        try:
            reservation = watcher.rail.reserve(
                train,
                passengers=watcher.passenger_objects(),
                option=watcher.seat_option,
            )
        except (SoldOutError, KorailError, SRTError) as ex:
            msg = getattr(ex, "msg", str(ex)) or type(ex).__name__
            # 한발 늦었거나 조건이 안 맞는 경우 - 다음 후보로 넘어간다
            _log(colored(f"  실패 ({msg}), 다음 후보 시도", "yellow"))
            continue

        text = f"{reservation}"
        if getattr(reservation, "tickets", None):
            text += "\n" + "\n".join(map(str, reservation.tickets))

        if reservation.is_waiting:
            return "🎫 예약대기 신청 완료\n" + text

        text = "🎫 예매 성공!\n" + text
        if pay:
            try:
                paid = pay_card(watcher.rail, reservation)
            except Exception as err:
                paid, text = False, text + f"\n⚠️ 결제 중 오류: {err}"
            text += (
                "\n💳 결제 완료"
                if paid
                else "\n⚠️ 자동 결제가 되지 않았습니다. 구입기한 내에 직접 결제하세요."
            )
        else:
            text += "\n⚠️ 아직 미결제 상태입니다. 구입기한 내에 직접 결제하세요."
        return text
    return None


def _confirm_reserve(watchers, start_dt, end_dt, pay: bool, assume_yes: bool) -> None:
    """실제 예약이 일어나므로 시작 전에 조건을 보여주고 확인받는다."""
    passengers = ", ".join(f"{k} {v}명" for k, v in watchers[0].counts.items() if v)
    print(
        colored(
            "\n⚠️  자동 예매 모드: 조건에 맞는 자리를 발견하면 즉시 예약합니다.", "yellow"
        )
    )
    print(
        f"  구간   : {' + '.join(f'{w.rail_type} {w.dep}→{w.arr}' for w in watchers)}\n"
        f"  시간대 : {start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M} 출발\n"
        f"  승객   : {passengers}\n"
        f"  좌석   : {watchers[0].seat_type}\n"
        f"  결제   : {'등록된 카드로 자동 결제' if pay else '예약만 (결제는 직접)'}\n"
    )
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise click.ClickException(
            "자동 예매는 확인이 필요합니다. 터미널이 아닌 환경에서는 --yes 를 명시하세요."
        )
    if not click.confirm("위 조건으로 자동 예매를 시작할까요?", default=False):
        raise click.Abort()


def _telegram_sender():
    """환경변수 우선, 없으면 srtgo 키체인 설정을 쓴다. 둘 다 없으면 None."""
    token = os.environ.get("TELEGRAM_TOKEN") or _keyring_get("telegram", "token")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or _keyring_get("telegram", "chat_id")
    if not token or not chat_id:
        return None
    if os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_CHAT_ID"):
        import telegram

        async def send(text):
            bot = telegram.Bot(token=token)
            async with bot:
                await bot.send_message(chat_id=chat_id, text=text)

        return send
    return get_telegram()


def _notify(text: str, sender, exec_cmd: Optional[str]) -> None:
    if sender is not None:
        try:
            asyncio.run(sender(text))
        except Exception as err:  # 알림 실패가 감시를 멈추면 안 된다
            _log(colored(f"텔레그램 전송 실패: {err}", "yellow"))
    if exec_cmd:
        try:
            subprocess.Popen(exec_cmd, shell=True, env={**os.environ, "SRTGO_MESSAGE": text})
        except Exception as err:
            _log(colored(f"--exec 실행 실패: {err}", "yellow"))


def _resolve_stations(rail_type: str, dep: str, arr: str) -> Tuple[str, str]:
    valid = STATIONS[rail_type]
    if rail_type == "SRT" and dep not in valid and dep in SEOUL_TO_SUSEO:
        _log(colored(f"SRT 는 {dep}역에서 출발하지 않습니다. 수서역으로 대체합니다.", "yellow"))
        dep = "수서"
    for name, label in ((dep, "출발역"), (arr, "도착역")):
        if name not in valid:
            raise click.ClickException(
                f"{rail_type} 에 없는 {label}입니다: {name}\n"
                f"가능한 역: {', '.join(valid)}"
            )
    return dep, arr


@click.command()
@click.option(
    "--rail",
    "rails",
    multiple=True,
    type=click.Choice(["KTX", "SRT"], case_sensitive=False),
    default=("KTX",),
    help="감시할 철도사. 여러 번 줄 수 있습니다 (예: --rail KTX --rail SRT).",
)
@click.option(
    "--dep",
    "deps",
    multiple=True,
    required=True,
    help="출발역. 여러 번 줄 수 있습니다 (예: --dep 서울 --dep 수서).",
)
@click.option("--arr", required=True, help="도착역 (예: 포항)")
@click.option("--srt-dep", default=None, help="SRT 출발역을 따로 지정 (기본: --dep, 수도권이면 수서)")
@click.option("--srt-arr", default=None, help="SRT 도착역을 따로 지정 (기본: --arr)")
@click.option("--start", required=True, help='감시 시작 시각 (예: "2026-09-23 12:00")')
@click.option("--end", required=True, help='감시 종료 시각 (예: "2026-09-24 12:00")')
@click.option("--passengers", default=1, show_default=True, help="성인 승객 수")
@click.option("--child", default=0, show_default=True, help="어린이 승객 수")
@click.option("--senior", default=0, show_default=True, help="경로 승객 수")
@click.option(
    "--seat-type",
    type=click.Choice(["any", "general", "special"]),
    default="any",
    show_default=True,
    help="어떤 좌석이 열릴 때 알릴지",
)
@click.option("--include-standby", is_flag=True, help="예약대기 가능도 '가능' 으로 본다")
@click.option("--ktx-only", is_flag=True, help="KTX 등급만 조회 (무궁화/ITX 제외)")
@click.option("--interval", default=60, show_default=True, help=f"조회 간격 (초, 최소 {MIN_INTERVAL})")
@click.option("--once", is_flag=True, help="한 번만 조회하고 종료 (cron 용)")
@click.option("--telegram/--no-telegram", default=False, help="텔레그램으로 알림 전송")
@click.option("--exec", "exec_cmd", default=None, help="자리가 났을 때 실행할 셸 명령")
@click.option(
    "--repeat-notify",
    default=0,
    show_default=True,
    help="자리가 계속 있을 때 N분마다 다시 알림 (0=상태가 바뀔 때만)",
)
@click.option(
    "--reserve",
    "do_reserve",
    is_flag=True,
    help="감시만 하지 않고, 자리가 나면 즉시 예약까지 진행합니다.",
)
@click.option(
    "--pay",
    is_flag=True,
    help="예약 성공 시 srtgo 에 등록된 카드로 결제까지 진행 (--reserve 필요)",
)
@click.option(
    "--prefer",
    type=click.Choice(["earliest", "latest"]),
    default="earliest",
    show_default=True,
    help="여러 편이 동시에 열렸을 때 어느 쪽을 먼저 잡을지",
)
@click.option("--yes", "assume_yes", is_flag=True, help="자동 예매 확인 프롬프트 생략")
@click.option(
    "--dry-run",
    is_flag=True,
    help="예약 직전까지만 수행하고 실제 예약은 하지 않습니다 (--reserve 검증용)",
)
@click.option("--debug", is_flag=True, help="디버그 출력")
def watch(
    rails,
    deps,
    arr,
    srt_dep,
    srt_arr,
    start,
    end,
    passengers,
    child,
    senior,
    seat_type,
    include_standby,
    ktx_only,
    interval,
    once,
    telegram,
    exec_cmd,
    repeat_notify,
    do_reserve,
    pay,
    prefer,
    assume_yes,
    dry_run,
    debug,
):
    """지정한 구간·시간대의 좌석을 감시하고, --reserve 를 주면 예약까지 진행합니다."""
    rails = tuple(dict.fromkeys(r.upper() for r in rails))
    interval = max(interval, MIN_INTERVAL)
    start_dt, end_dt = parse_when(start), parse_when(end, end=True)
    if end_dt <= start_dt:
        raise click.ClickException("--end 는 --start 보다 뒤여야 합니다.")

    counts = {"adult": passengers, "child": child, "senior": senior}
    total = sum(counts.values())
    if total < 1:
        raise click.ClickException("승객수는 1명 이상이어야 합니다.")
    if total > 9:
        raise click.ClickException("승객수는 9명을 초과할 수 없습니다.")
    if pay and not do_reserve:
        raise click.ClickException("--pay 는 --reserve 와 함께 써야 합니다.")
    if dry_run and not do_reserve:
        raise click.ClickException("--dry-run 은 --reserve 와 함께 써야 합니다.")

    watchers = []
    sessions: Dict[str, object] = {}
    seen = set()
    for rail_type in rails:
        rail_deps = (srt_dep,) if (rail_type == "SRT" and srt_dep) else deps
        r_arr = (srt_arr or arr) if rail_type == "SRT" else arr
        for raw_dep in rail_deps:
            r_dep, resolved_arr = _resolve_stations(rail_type, raw_dep, r_arr)
            # 수도권 역을 여러 개 준 경우 SRT 쪽은 전부 수서로 모이므로 중복을 걸러낸다
            if (rail_type, r_dep, resolved_arr) in seen:
                continue
            seen.add((rail_type, r_dep, resolved_arr))
            watchers.append(
                RailWatcher(
                    rail_type,
                    r_dep,
                    resolved_arr,
                    start_dt,
                    end_dt,
                    counts,
                    seat_type,
                    include_standby,
                    ktx_only,
                    debug,
                    sessions,
                )
            )

    if do_reserve:
        _confirm_reserve(watchers, start_dt, end_dt, pay, assume_yes)

    mode = "자동 예매" + (" (DRY-RUN)" if dry_run else "") if do_reserve else "감시"
    _log(
        colored(
            f"{mode} 시작: "
            f"{' + '.join(f'{w.rail_type} {w.dep}→{w.arr}' for w in watchers)} | "
            f"{start_dt:%m/%d %H:%M} ~ {end_dt:%m/%d %H:%M} | "
            f"{total}명 | {interval}초 간격",
            "cyan",
        )
    )

    tg_sender = _telegram_sender() if telegram else None
    if telegram and tg_sender is None:
        raise click.ClickException(
            "텔레그램 설정이 없습니다. `srtgo` 의 '텔레그램 설정' 을 마치거나 "
            "환경변수 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 를 설정하세요."
        )

    for watcher in watchers:
        watcher.login()

    last_notified = 0.0
    sweep_no = 0
    try:
        while True:
            sweep_no += 1
            newly, open_now, snapshot = [], [], []
            for watcher in watchers:
                try:
                    w_new, w_open, w_snap = watcher.check()
                except click.ClickException:
                    raise
                except Exception as ex:
                    _log(colored(f"{watcher.rail_type} 조회 실패 ({type(ex).__name__}), 복구 시도", "yellow"))
                    try:
                        watcher.recover(ex)
                    except Exception as err:
                        _log(colored(f"{watcher.rail_type} 복구 실패: {err}", "red"))
                    continue
                newly += w_new
                open_now += w_open
                snapshot += w_snap

            n_open, n_total = len(open_now), len(snapshot)

            # 예매 모드: 열려 있는 자리를 선호 순서대로 잡는다
            if do_reserve and n_open:
                candidates = [
                    (w, t) for w in watchers for t in w.open_trains
                ]
                candidates.sort(
                    key=lambda wt: (wt[1].dep_date, wt[1].dep_time),
                    reverse=(prefer == "latest"),
                )
                try:
                    result = _try_reserve(candidates, pay, dry_run)
                except Exception as ex:
                    _log(colored(f"예매 중 오류 ({type(ex).__name__}: {ex})", "red"))
                    result = None
                if result:
                    _log(colored(result, "white", "on_green"))
                    _notify(result, tg_sender, exec_cmd)
                    return EXIT_RESERVED

            if newly:
                text = "🚄 좌석이 열렸습니다!\n" + "\n".join(newly)
                _log(colored(text, "white", "on_green"))
                _notify(text, tg_sender, exec_cmd)
                last_notified = time.time()
            elif (
                repeat_notify
                and n_open
                and time.time() - last_notified >= repeat_notify * 60
            ):
                text = "🚄 아직 좌석이 남아 있습니다\n" + "\n".join(open_now)
                _log(colored(text, "green"))
                _notify(text, tg_sender, exec_cmd)
                last_notified = time.time()
            else:
                _log(
                    f"#{sweep_no:<4d} 감시 중… 대상 {n_total}편 / 예매가능 "
                    + (colored(f"{n_open}편", "green") if n_open else "0편"),
                )

            if debug and snapshot:
                for line in snapshot:
                    print("    " + line, flush=True)

            if once:
                break
            time.sleep(gammavariate(4, interval / 8) + interval * 0.5)
    except KeyboardInterrupt:
        _log("종료합니다.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(watch())
