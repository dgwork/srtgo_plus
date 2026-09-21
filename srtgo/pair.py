"""일행 인원수만큼 '같은 열차' 좌석을 모으는 실행기.

명절처럼 2석이 한 번에 나오지 않는 상황을 위한 전략이다.
조건 구간의 열차를 계속 훑으면서 **1석이라도 보이면 일단 예약**해 두고,
그러다 특정 열차에 예약이 인원수만큼 겹치는 순간을 승리로 본다.

    # 서울/수서 아무거나 -> 2장이 한 열차에 겹치면 성공, 단 수서를 최종 목표로
    srtgo-pair --dep 서울 --dep 수서 --prefer-dep 수서 --arr 포항 \
        --start "2026-09-23 17:00" --end "2026-09-24 10:00"

--prefer-dep 을 주면 2단계로 동작한다.
  1단계: 모든 출발역 대상. 어느 열차든 인원수를 채우면 '바닥'을 확보한 것.
  2단계: 확보한 열차가 --prefer-dep 이 아니면, 이제 --prefer-dep 출발
         열차만 대상으로 같은 일을 반복한다. 이미 잡은 표는 놓지 않는다.

통합(2026-09-01 운행분~) 이후 수서 출발 편성도 코레일이 판매하므로
이 명령은 코레일 계정 하나만 쓴다.
"""

from datetime import datetime, timedelta
from random import gammavariate
from termcolor import colored
from typing import Dict, List, Optional, Tuple

import click
import sys
import time

from .ktx import AdultPassenger, KorailError, ReserveOption
from .srtgo import pay_card
from .watch import (
    MIN_INTERVAL,
    RailWatcher,
    _log,
    _notify,
    _now,
    _resolve_stations,
    _telegram_sender,
    parse_when,
)

# 좌석 유형 -> 예약 옵션
SEAT_OPTION = {
    "any": ReserveOption.GENERAL_FIRST,
    "general": ReserveOption.GENERAL_ONLY,
    "special": ReserveOption.SPECIAL_ONLY,
}

TrainKey = Tuple[str, str]


def _train_key(train) -> TrainKey:
    return (str(train.train_no), str(train.dep_date))


def _rsv_key(rsv) -> TrainKey:
    return (str(rsv.train_no), str(rsv.dep_date))


def _dep_dt(date: str, dep_time: str) -> datetime:
    return datetime(
        int(date[0:4]), int(date[4:6]), int(date[6:8]),
        int(dep_time[0:2]), int(dep_time[2:4]),
    )


class Holdings:
    """지금 내가 들고 있는 예약 현황. 서버 응답을 그대로 신뢰한다.

    스크립트가 재시작되거나 예약이 구입기한을 넘겨 사라져도, 매 스윕마다
    서버에서 다시 읽으므로 상태가 어긋나지 않는다.
    """

    def __init__(self, rail, arr: str, deps: Tuple[str, ...], start: datetime, end: datetime):
        self.rail = rail
        self.arr = arr
        self.deps = set(deps)
        self.start = start
        self.end = end
        self.by_train: Dict[TrainKey, int] = {}
        self.detail: Dict[TrainKey, List] = {}
        # 서버 목록 반영이 늦을 때 쓰는 출발역 보조 정보
        self.dep_hint: Dict[TrainKey, str] = {}

    def _all_holdings(self) -> List:
        """미결제 예약 + 결제 완료된 승차권을 함께 읽는다.

        결제하는 순간 그 건은 reservations() 에서 빠지고 tickets() 로 넘어간다.
        예약만 보면 방금 결제한 표를 '없는 것' 으로 판단해 같은 자리를 또 사게 된다.
        """
        reservations, tickets, ok = [], [], 0
        for fetch, bucket in (
            (self.rail.reservations, reservations),
            (self.rail.tickets, tickets),
        ):
            try:
                bucket += list(fetch() or [])
                ok += 1
            except Exception:
                continue  # 한쪽이 실패해도 나머지는 살린다
        if not ok:
            # 둘 다 실패했는데 '보유 0' 으로 넘어가면 같은 자리를 또 산다.
            # 이전 상태를 유지하도록 호출부로 올린다.
            raise RuntimeError("보유 내역 조회에 실패했습니다")

        # tickets() 는 좌석 하나당 행 하나를 준다. 즉 2석짜리 구매는 같은 예약번호로
        # 행이 두 개다. 여기서 예약번호로 중복을 거르면 2석을 1석으로 세어
        # '아직 목표 미달' 로 오판하고 같은 구간을 계속 사게 된다.
        # 따라서 행은 합치되, 결제 전후로 같은 건이 양쪽에 걸쳐 보이는 경우에만
        # 결제본(tickets) 쪽을 채택한다.
        paid_pnrs = {getattr(t, "pnr_no", None) for t in tickets}
        rows = list(tickets)
        rows += [r for r in reservations if getattr(r, "rsv_id", None) not in paid_pnrs]
        return rows

    def refresh(self) -> None:
        by_train, detail = {}, {}
        for rsv in self._all_holdings():
            if rsv.arr_name != self.arr or rsv.dep_name not in self.deps:
                continue
            if getattr(rsv, "is_waiting", False):
                continue  # 예약대기는 확정 좌석이 아니다
            try:
                when = _dep_dt(rsv.dep_date, rsv.dep_time)
            except (TypeError, ValueError):
                continue
            if not (self.start <= when <= self.end):
                continue
            key = _rsv_key(rsv)
            by_train[key] = by_train.get(key, 0) + int(rsv.seat_no_count)
            detail.setdefault(key, []).append(rsv)
        # 조회가 통째로 실패한 경우가 아니라면 갱신한다
        self.by_train, self.detail = by_train, detail

    def seats_on(self, key: TrainKey) -> int:
        return self.by_train.get(key, 0)

    def total(self) -> int:
        return sum(self.by_train.values())

    def complete_trains(self, seats: int) -> List[TrainKey]:
        return [k for k, n in self.by_train.items() if n >= seats]

    def describe(self) -> str:
        if not self.detail:
            return "  (보유 없음)"
        lines = []
        for key in sorted(self.detail, key=lambda k: (k[1], k[0])):
            for rsv in self.detail[key]:
                lines.append(f"  {rsv}")
        return "\n".join(lines)


def _settle(rail, rsv) -> str:
    """예약 직후 등록된 카드로 결제한다. 실패해도 예약은 남으므로 멈추지 않는다."""
    if getattr(rsv, "is_waiting", False):
        return "⚠️ 예약대기라 결제하지 않았습니다."
    try:
        if pay_card(rail, rsv):
            return "💳 결제 완료"
        return "⚠️ 결제 실패 - 카드 설정을 확인하고 구입기한 내 직접 결제하세요."
    except Exception as err:
        return f"⚠️ 결제 중 오류: {err} - 구입기한 내 직접 결제하세요."


def _dep_of(key: TrainKey, holdings: Holdings) -> Optional[str]:
    rsvs = holdings.detail.get(key)
    if rsvs:
        return rsvs[0].dep_name
    return holdings.dep_hint.get(key)


@click.command()
@click.option("--dep", "deps", multiple=True, required=True,
              help="출발역. 여러 번 줄 수 있습니다 (예: --dep 서울 --dep 수서).")
@click.option("--arr", required=True, help="도착역 (예: 포항)")
@click.option("--prefer-dep", default=None,
              help="최종적으로 노릴 출발역. 여기서 인원수를 채우면 종료합니다.")
@click.option("--start", required=True, help='시작 시각 (예: "2026-09-23 17:00")')
@click.option("--end", required=True, help='종료 시각 (예: "2026-09-24 10:00")')
@click.option("--seats", default=2, show_default=True, help="일행 인원수 (한 열차에 모아야 할 좌석 수)")
@click.option("--max-holds", default=4, show_default=True,
              help="동시에 들고 있을 1인 예약 총 수의 상한 (0=무제한)")
@click.option("--seat-type", type=click.Choice(["any", "general", "special"]),
              default="any", show_default=True)
@click.option("--interval", default=30, show_default=True, help=f"조회 간격 (초, 최소 {MIN_INTERVAL})")
@click.option("--summary-interval", default=60, show_default=True,
              help="진행 상황 요약을 보낼 주기 (분, 0=보내지 않음)")
@click.option("--pay/--no-pay", default=True, show_default=True,
              help="예약 즉시 등록된 카드로 결제 (미결제 예약은 구입기한이 지나면 사라집니다)")
@click.option("--telegram/--no-telegram", default=True, show_default=True,
              help="예약·결제 내역을 텔레그램으로 전송")
@click.option("--dry-run", is_flag=True, help="예약은 하지 않고 무엇을 시도할지만 출력")
@click.option("--yes", "assume_yes", is_flag=True, help="시작 확인 프롬프트 생략")
@click.option("--debug", is_flag=True, help="디버그 출력")
def pair(deps, arr, prefer_dep, start, end, seats, max_holds, seat_type,
         interval, summary_interval, pay, telegram, dry_run, assume_yes, debug):
    """1석씩 모아서 한 열차에 --seats 장을 맞추는 예매기 (코레일 계정 전용)."""
    interval = max(interval, MIN_INTERVAL)
    start_dt, end_dt = parse_when(start), parse_when(end, end=True)
    if end_dt <= start_dt:
        raise click.ClickException("--end 는 --start 보다 뒤여야 합니다.")
    if seats < 2:
        raise click.ClickException("--seats 는 2 이상이어야 합니다. 1명이면 srtgo-watch 를 쓰세요.")

    deps = tuple(dict.fromkeys(deps))
    for d in deps:
        _resolve_stations("KTX", d, arr)
    if prefer_dep and prefer_dep not in deps:
        raise click.ClickException(f"--prefer-dep {prefer_dep} 은 --dep 목록에 없습니다.")

    print(colored("\n⚠️  이 명령은 자리가 보이는 대로 1인 예약을 반복 생성합니다.", "yellow"))
    print(
        f"  구간   : {'/'.join(deps)}→{arr}\n"
        f"  시간대 : {start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M} 출발\n"
        f"  목표   : 한 열차에 {seats}석"
        + (f" (최종 목표 출발역: {prefer_dep})" if prefer_dep else "")
        + f"\n  보유상한: {max_holds or '무제한'}\n"
        + (
            "  결제   : 예약 즉시 등록된 카드로 자동 결제\n"
            "           (짝이 맞지 않은 표도 결제되며, 취소 시 환불 수수료가 붙습니다)\n"
            if pay
            else "  결제   : 하지 않음 (구입기한 내 직접 결제하세요)\n"
        )
        + ("  알림   : 텔레그램 전송\n" if telegram else "")
    )
    if not (dry_run or assume_yes):
        if not sys.stdin.isatty():
            raise click.ClickException("확인이 필요합니다. 비대화형 환경에서는 --yes 를 쓰세요.")
        if not click.confirm("위 조건으로 시작할까요?", default=False):
            raise click.Abort()

    tg = _telegram_sender() if telegram else None
    if telegram and tg is None:
        _log(colored("텔레그램 설정이 없어 알림 없이 진행합니다.", "yellow"))

    # 출발역마다 감시자를 하나씩 두고 세션은 공유한다 (로그인 1회)
    sessions: Dict[str, object] = {}
    watchers = {
        d: RailWatcher("KTX", d, arr, start_dt, end_dt, {"adult": 1, "child": 0, "senior": 0},
                       seat_type, False, False, debug, sessions)
        for d in deps
    }
    for w in watchers.values():
        w.login()
    rail = next(iter(watchers.values())).rail

    holdings = Holdings(rail, arr, deps, start_dt, end_dt)
    option = SEAT_OPTION[seat_type]
    active = list(deps)          # 지금 노리는 출발역들
    floor: Optional[TrainKey] = None  # 이미 인원수를 채운 열차 (놓지 않는다)
    sweep_no = 0

    # 새벽에 돌려두는 용도라, 아무 일이 없어도 살아 있다는 신호를 주기적으로 보낸다
    began = _now()
    last_summary = began
    stats = {
        "sweeps": 0, "scanned": 0, "open": 0,
        "attempts": 0, "bought": 0, "spent": 0, "rejects": {}, "errors": 0,
    }

    def send_summary(reason: str = "정기") -> None:
        nonlocal last_summary
        now = _now()
        elapsed = now - began
        hours, rem = divmod(int(elapsed.total_seconds()), 3600)
        minutes = rem // 60
        rejects = ", ".join(
            f"{msg} {n}회" for msg, n in sorted(
                stats["rejects"].items(), key=lambda kv: -kv[1]
            )[:4]
        ) or "없음"
        held = ", ".join(
            f"{k[0]} {n}석" for k, n in sorted(holdings.by_train.items())
        ) or "없음"
        text = (
            f"📊 srtgo-pair {reason} 요약 ({now:%m/%d %H:%M})\n"
            f"경과 {hours}시간 {minutes}분 · {stats['sweeps']}회차\n"
            f"대상 {'/'.join(active)}→{arr} · 마지막 회차 {stats['scanned']}편 중 "
            f"예매가능 {stats['open']}편\n"
            f"예약 시도 {stats['attempts']}회 → 확보 {stats['bought']}석"
            + (f" ({stats['spent']:,}원)" if stats["spent"] else "")
            + f"\n거부 사유: {rejects}"
            + (f"\n조회/예약 오류 {stats['errors']}회" if stats["errors"] else "")
            + f"\n현재 보유: {held}"
            + (f"\n바닥 확보됨: {floor[0]} (이제 {prefer_dep} 만 탐색)" if floor else "")
        )
        _log(colored(text, "cyan"))
        notify(text)
        last_summary = now

    def notify(text: str) -> None:
        _notify(text, tg, None)

    def evaluate() -> bool:
        """목표 달성 여부를 판정한다. 최종 달성이면 True.

        예약을 한 건 할 때마다 즉시 불러야 한다. 회차 시작 시점에만 검사하면
        이미 목표를 채운 뒤에도 같은 회차의 남은 후보를 계속 사들인다.
        """
        nonlocal floor, active
        for key in holdings.complete_trains(seats):
            dep_name = _dep_of(key, holdings)
            if prefer_dep is None or dep_name == prefer_dep:
                text = (
                    f"🎉 {dep_name} 출발 열차 {key[0]} 에 {seats}석 확보 완료! 탐색을 종료합니다.\n"
                    + holdings.describe()
                    + (
                        "\n\n결제까지 끝난 표입니다. 짝이 맞지 않고 남은 표는 "
                        "직접 확인해서 정리하세요."
                        if pay
                        else "\n\n⚠️ 미결제 상태입니다. 구입기한 내 결제하세요."
                    )
                )
                _log(colored(text, "white", "on_green"))
                notify(text)
                return True
            if floor != key:
                floor = key
                active = [prefer_dep]
                text = (
                    f"✅ 바닥 확보: {dep_name} 출발 {key[0]} 에 {seats}석.\n"
                    f"이제 {prefer_dep} 출발 열차만 노립니다. 기존 표는 그대로 둡니다.\n"
                    + holdings.describe()
                )
                _log(colored(text, "white", "on_blue"))
                notify(text)
        return False

    try:
        while True:
            sweep_no += 1

            # 1) 서버 기준으로 보유 현황을 다시 읽는다
            try:
                holdings.refresh()
            except Exception as ex:
                _log(colored(f"보유 예약 조회 실패 ({type(ex).__name__}), 다음 회차에 다시 시도", "yellow"))

            # 2) 승리 조건 확인 - 이미 채웠으면 아무것도 사지 않고 끝낸다
            if evaluate():
                return 0

            # 3) 후보 수집 - 지금 예약 가능한 열차
            candidates = []
            stats["sweeps"] = sweep_no
            scanned = opened = 0
            for dep_name in active:
                watcher = watchers[dep_name]
                try:
                    watcher.check()  # sweep + 가용 판정, open_trains 를 채운다
                except Exception as ex:
                    stats["errors"] += 1
                    _log(colored(f"{dep_name} 조회 실패 ({type(ex).__name__}), 복구 시도", "yellow"))
                    try:
                        watcher.recover(ex)
                    except Exception as err:
                        _log(colored(f"{dep_name} 복구 실패: {err}", "red"))
                    continue
                scanned += len(watcher.state)
                opened += len(watcher.open_trains)
                for train in watcher.open_trains:
                    key = _train_key(train)
                    need = seats - holdings.seats_on(key)
                    if need <= 0:
                        continue
                    candidates.append((need, dep_name, train, key))

            stats["scanned"], stats["open"] = scanned, opened

            # 이미 표를 들고 있는 열차를 최우선으로 (짝이 맞는 순간이 목표다),
            # 그 다음 --prefer-dep, 그 다음 이른 출발 순
            candidates.sort(
                key=lambda c: (
                    0 if holdings.seats_on(c[3]) > 0 else 1,
                    0 if c[1] == prefer_dep else 1,
                    c[2].dep_date,
                    c[2].dep_time,
                )
            )

            # 4) 예약 시도
            # 이미 확보한 '바닥' 좌석은 상한에서 뺀다. 그러지 않으면 서울에서
            # 인원수를 채운 순간 상한이 차버려서 2단계에서 수서 표를 못 산다.
            floor_seats = holdings.seats_on(floor) if floor else 0
            for need, dep_name, train, key in candidates:
                in_play = holdings.total() - floor_seats
                if max_holds and in_play >= max_holds and holdings.seats_on(key) == 0:
                    continue  # 상한에 걸렸으면 새 열차로 벌리지 않는다 (짝 맞추기는 계속)
                if dry_run:
                    _log(colored(f"[DRY-RUN] 예약 시도했을 열차: [{dep_name}] {train} (필요 {need}석)", "cyan"))
                    continue

                prev = holdings.seats_on(key)
                bought = 0
                # 한 번에 need 석을 잡을 수 있으면 그게 최선, 안 되면 1석이라도
                for count in ([need, 1] if need > 1 else [1]):
                    stats["attempts"] += 1
                    try:
                        rsv = watchers[dep_name].rail.reserve(
                            train, passengers=[AdultPassenger(count)], option=option
                        )
                    except KorailError as ex:
                        msg = getattr(ex, "msg", str(ex))
                        stats["rejects"][msg] = stats["rejects"].get(msg, 0) + 1
                        if count == 1 or "잔여" in msg or "Sold out" in msg:
                            if not any(k in msg for k in ("Sold out", "잔여석없음")):
                                _log(colored(f"  예약 거부 ({msg})", "yellow"))
                            break
                        continue
                    except Exception as ex:
                        stats["errors"] += 1
                        _log(colored(f"  예약 오류 ({type(ex).__name__}: {ex})", "red"))
                        break

                    stats["bought"] += count
                    stats["spent"] += int(getattr(rsv, "price", 0) or 0)
                    text = f"🎫 {count}석 확보: [{dep_name}] {train}\n{rsv}"
                    text += (
                        "\n" + _settle(watchers[dep_name].rail, rsv)
                        if pay
                        else "\n⚠️ 미결제 상태입니다. 구입기한 내 결제하세요."
                    )
                    _log(colored(text, "white", "on_green"))
                    notify(text)
                    bought = count
                    break

                if not bought:
                    continue

                # 방금 산 좌석은 무슨 일이 있어도 집계에 반영한다.
                # 서버 목록 반영이 늦거나 조회가 실패했을 때 '아직 미달' 로 오판하면
                # 같은 구간을 계속 사들이게 된다.
                try:
                    holdings.refresh()
                except Exception:
                    pass
                holdings.dep_hint[key] = dep_name
                if holdings.seats_on(key) < prev + bought:
                    holdings.by_train[key] = prev + bought

                # 한 건 살 때마다 즉시 판정한다
                if evaluate():
                    return 0
                if dep_name not in active:
                    break  # 단계가 바뀌었으므로 이번 회차 후보 목록은 버린다

            # 5) 현황 한 줄
            held_desc = ", ".join(
                f"{k[0]}:{n}석" for k, n in sorted(holdings.by_train.items())
            ) or "없음"
            _log(
                f"#{sweep_no:<4d} {'/'.join(active)}→{arr} | 보유 {holdings.total()}석 ({held_desc})"
            )

            # 6) 주기 요약 - 아무 일이 없어도 살아 있다는 신호를 보낸다
            if summary_interval and (
                (_now() - last_summary).total_seconds() >= summary_interval * 60
            ):
                send_summary()

            time.sleep(gammavariate(4, interval / 8) + interval * 0.5)

    except KeyboardInterrupt:
        _log("종료합니다. 현재 보유:")
        print(holdings.describe())
        if summary_interval:
            send_summary("중단")
        return 0


if __name__ == "__main__":
    sys.exit(pair())
