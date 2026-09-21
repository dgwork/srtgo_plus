"""보유 좌석 집계 회귀 테스트.

실제로 돈이 샌 적이 있는 지점이다. tickets() 는 좌석 하나당 행 하나를 돌려주므로
2석짜리 구매는 같은 예약번호(pnr)로 행이 두 개 온다. 여기서 예약번호로 중복을
제거하면 2석이 1석으로 집계되고, '아직 목표 미달' 로 오판해 같은 구간을 계속
사들이게 된다.

    python -m pytest tests/ -q      (pytest 가 있으면)
    python tests/test_pair_holdings.py
"""

from datetime import datetime

from srtgo.pair import Holdings

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


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed}개 통과")
