"""보유 좌석 집계 회귀 테스트.

실제로 돈이 샌 적이 있는 지점이다. tickets() 는 좌석 하나당 행 하나를 돌려주므로
2석짜리 구매는 같은 예약번호(pnr)로 행이 두 개 온다. 여기서 예약번호로 중복을
제거하면 2석이 1석으로 집계되고, '아직 목표 미달' 로 오판해 같은 구간을 계속
사들이게 된다.

    python -m pytest tests/ -q      (pytest 가 있으면)
    python tests/test_pair_holdings.py
"""

from datetime import datetime

from srtgo.pair import Holdings, _standby_want

START = datetime(2026, 10, 20, 17, 0)
END = datetime(2026, 10, 21, 10, 0)
DEPS = ("서울", "수서")


class _Row:
    """tickets() 응답 한 행 (= 좌석 하나)."""

    def __init__(self, train_no, pnr, dep="수서", seat_cnt=1):
        self.train_no, self.pnr_no = train_no, pnr
        self.dep_date, self.dep_time = "20261020", "183700"
        self.dep_name, self.arr_name = dep, "포항"
        self.seat_no_count = seat_cnt

    def __repr__(self):
        return f"[KTX {self.train_no}] {self.dep_name}~{self.arr_name} (pnr {self.pnr_no})"


class _FakeRail:
    def __init__(self, tickets=(), reservations=(), fail=()):
        self._tickets, self._reservations, self._fail = tickets, reservations, fail

    def tickets(self):
        if "tickets" in self._fail:
            raise RuntimeError("tickets 조회 실패")
        return list(self._tickets)

    def reservations(self):
        if "reservations" in self._fail:
            raise RuntimeError("reservations 조회 실패")
        return list(self._reservations)


def _holdings(**kwargs):
    h = Holdings(_FakeRail(**kwargs), "포항", DEPS, START, END)
    h.refresh()
    return h


def test_two_seat_purchase_counts_as_two():
    """2석 구매 1건 = 같은 pnr 행 2개. 반드시 2석으로 세야 한다."""
    h = _holdings(tickets=[_Row("397", "PNR1"), _Row("397", "PNR1")])
    assert h.seats_on(("397", "20261020")) == 2
    assert h.complete_trains(2) == [("397", "20261020")]


def test_single_seat_is_not_complete():
    h = _holdings(tickets=[_Row("397", "PNR1")])
    assert h.seats_on(("397", "20261020")) == 1
    assert h.complete_trains(2) == []


def test_two_separate_purchases_pair_up():
    """1석씩 따로 산 2건이 같은 열차에 겹치면 짝으로 인정한다 (이 도구의 목적)."""
    h = _holdings(tickets=[_Row("397", "PNR1"), _Row("397", "PNR2")])
    assert h.complete_trains(2) == [("397", "20261020")]


def test_paid_row_wins_over_stale_reservation():
    """결제 전후로 같은 건이 양쪽 목록에 걸쳐 보이면 결제본만 센다."""
    stale = _Row("397", "PNR1")
    stale.rsv_id, stale.seat_no_count = "PNR1", 2
    h = _holdings(tickets=[_Row("397", "PNR1"), _Row("397", "PNR1")], reservations=[stale])
    assert h.seats_on(("397", "20261020")) == 2


def test_other_trips_are_ignored():
    """구간·시간대가 다른 표는 집계에 들어오면 안 된다."""
    other = _Row("282", "PNR9", dep="포항")
    other.arr_name = "서울"
    h = _holdings(tickets=[other])
    assert h.by_train == {}


def test_total_failure_raises_instead_of_reporting_zero():
    """양쪽 조회가 모두 실패했을 때 '보유 0' 으로 넘어가면 같은 자리를 또 산다."""
    h = Holdings(_FakeRail(fail=("tickets", "reservations")), "포항", DEPS, START, END)
    try:
        h.refresh()
    except Exception:
        return
    raise AssertionError("조회 전면 실패는 예외로 올려야 한다")


class _Waiting(_Row):
    """reservations() 가 돌려주는 예약대기 한 건."""

    def __init__(self, train_no, pnr, dep="수서", seat_cnt=1):
        super().__init__(train_no, pnr, dep=dep, seat_cnt=seat_cnt)
        self.rsv_id = pnr
        self.is_waiting = True

    def __repr__(self):
        return f"[KTX {self.train_no}] {self.dep_name}~{self.arr_name} 예약대기"


def test_standby_is_not_counted_as_seat():
    """예약대기를 좌석으로 세면 자리도 없이 '인원수 확보' 로 오판하고 종료한다."""
    h = _holdings(reservations=[_Waiting("397", "W1"), _Waiting("397", "W2")])
    assert h.seats_on(("397", "20261020")) == 0
    assert h.total() == 0
    assert h.complete_trains(2) == []


def test_standby_is_tracked_for_dedup():
    """버리지 않고 따로 세야 같은 열차에 매 스윕 중복 신청하는 것을 막는다."""
    h = _holdings(reservations=[_Waiting("397", "W1", seat_cnt=2)])
    assert h.standby_on(("397", "20261020")) == 2
    assert h.standby_total() == 2


def test_seat_and_standby_coexist_on_same_train():
    """같은 열차에 확정 1석 + 대기 1석이면, 짝은 아직 안 맞은 것이다."""
    h = _holdings(tickets=[_Row("397", "PNR1")], reservations=[_Waiting("397", "W1")])
    key = ("397", "20261020")
    assert h.seats_on(key) == 1
    assert h.standby_on(key) == 1
    assert h.complete_trains(2) == []


def test_standby_outside_window_is_ignored():
    """시간대 밖의 대기는 집계에 들어오면 안 된다 (좌석과 같은 기준)."""
    w = _Waiting("999", "W9")
    w.dep_date = "20261101"
    h = _holdings(reservations=[w])
    assert h.standby == {}


def test_standby_want_keeps_filling_until_party_size():
    """1석짜리 대기 하나로 끝내면 짝이 안 맞아 쓸모가 없다. 인원수까지 채워야 한다."""
    # 좌석 0, 대기 0 -> 2석 모두 필요
    assert _standby_want(need=2, held_standby=0, standby_total=0, max_standby=0) == 2
    # 대기 1석을 이미 걸어둠 -> 1석 더
    assert _standby_want(need=2, held_standby=1, standby_total=1, max_standby=0) == 1
    # 대기로 인원수를 다 덮음 -> 그만
    assert _standby_want(need=2, held_standby=2, standby_total=2, max_standby=0) == 0


def test_standby_want_counts_confirmed_seats():
    """확정 1석을 들고 있으면(need=1) 대기는 1석만 더 걸면 된다."""
    assert _standby_want(need=1, held_standby=0, standby_total=1, max_standby=0) == 1
    assert _standby_want(need=1, held_standby=1, standby_total=2, max_standby=0) == 0


def test_standby_want_respects_max_standby():
    """상한이 남은 만큼만 신청한다. 상한이 차면 아예 걸지 않는다."""
    assert _standby_want(need=2, held_standby=0, standby_total=1, max_standby=2) == 1
    assert _standby_want(need=2, held_standby=0, standby_total=2, max_standby=2) == 0
    assert _standby_want(need=2, held_standby=0, standby_total=0, max_standby=2) == 2
    # 0 = 무제한
    assert _standby_want(need=2, held_standby=0, standby_total=99, max_standby=0) == 2


def test_describe_marks_standby():
    h = _holdings(tickets=[_Row("397", "PNR1")], reservations=[_Waiting("395", "W1")])
    out = h.describe()
    assert out.count("[대기]") == 1, out


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed}개 통과")
