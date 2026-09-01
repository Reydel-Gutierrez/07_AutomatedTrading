from datetime import datetime

from agentic_portfolio.calendar import EASTERN, NyseEquityCalendar, is_new_session, is_regular_hours
from agentic_portfolio.session import observe_nav_for_session
from agentic_portfolio.state_store import load_hwm_state, save_hwm_state
from tests.conftest import ctx


def test_weekend_does_not_reset_sod_risk_session(tmp_path):
    cal = NyseEquityCalendar()
    path = tmp_path / "session_state.json"
    friday = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
    saturday = datetime(2026, 8, 29, 12, 0, tzinfo=EASTERN)
    first = observe_nav_for_session(current_nav=500.0, now=friday, persist_path=path, calendar=cal)
    assert first.session_id == "2026-08-28"
    assert first.sod_nav == 500.0
    weekend = observe_nav_for_session(
        current_nav=480.0, now=saturday, prior=first, persist_path=path, calendar=cal
    )
    assert weekend.session_id == "2026-08-28"
    assert weekend.sod_nav == 500.0
    assert weekend.fail_safe is False
    rolled, session, reason = is_new_session(prior_session_id="2026-08-28", now=saturday, calendar=cal)
    assert rolled is False
    assert reason == "non_trading_day"
    # Daily halt still measured vs Friday SOD, not a fake Saturday session.
    c = ctx(480.0, start_of_day_nav=weekend.sod_nav)
    assert c.daily_risk_halt is True


def test_holiday_does_not_create_session():
    cal = NyseEquityCalendar()
    # New Year's Day 2026 is Thursday.
    ny = datetime(2026, 1, 1, 12, 0, tzinfo=EASTERN)
    assert cal.session_for(ny) is None
    # Labor Day 2026-09-07
    ld = datetime(2026, 9, 7, 10, 0, tzinfo=EASTERN)
    assert cal.session_for(ld) is None


def test_next_valid_session_creates_new_sod_anchor(tmp_path):
    cal = NyseEquityCalendar()
    path = tmp_path / "session_state.json"
    friday = datetime(2026, 8, 28, 16, 0, tzinfo=EASTERN)
    monday_preopen = datetime(2026, 8, 31, 8, 0, tzinfo=EASTERN)
    first = observe_nav_for_session(current_nav=500.0, now=friday, persist_path=path, calendar=cal)
    nxt = observe_nav_for_session(
        current_nav=510.0, now=monday_preopen, prior=first, persist_path=path, calendar=cal
    )
    assert nxt.session_id == "2026-08-31"
    assert nxt.sod_nav == 510.0
    assert nxt.sod_nav != first.sod_nav


def test_early_close_same_trading_day():
    cal = NyseEquityCalendar()
    # Black Friday 2026-11-27
    d = datetime(2026, 11, 27, 10, 0, tzinfo=EASTERN)
    s = cal.session_for(d)
    assert s is not None
    assert s.is_early_close is True
    assert s.session_id == "2026-11-27"


def test_process_restart_preserves_session_hwm_thesis_sleeve(tmp_path):
    from agentic_portfolio.schemas import Sleeve, SleeveAssignmentStatus, ThesisStatus
    from agentic_portfolio.sleeve_registry import SleeveRegistry
    from agentic_portfolio.thesis_registry import ThesisRegistry

    cal = NyseEquityCalendar()
    spath = tmp_path / "session_state.json"
    friday = datetime(2026, 8, 28, 10, 0, tzinfo=EASTERN)
    st = observe_nav_for_session(current_nav=500.0, now=friday, persist_path=spath, calendar=cal)
    c = ctx(500.0, start_of_day_nav=st.sod_nav, prior_hwm=500.0)
    # Simulate a small drawdown so HWM is meaningful.
    c2 = ctx(450.0, start_of_day_nav=st.sod_nav, prior_hwm=500.0, prior_nav=500.0)
    save_hwm_state(c2, tmp_path / "hwm_state.json")
    sleeves = SleeveRegistry(tmp_path / "sleeves.json")
    sleeves.assign(symbol="AAPL", sleeve=Sleeve.TACTICAL, status=SleeveAssignmentStatus.ACTIVE)
    theses = ThesisRegistry(tmp_path / "theses.json")
    rec = theses.create(symbol="AAPL", sleeve=Sleeve.TACTICAL, status=ThesisStatus.ACTIVE, thesis_summary="t")

    sleeves2 = SleeveRegistry(tmp_path / "sleeves.json")
    theses2 = ThesisRegistry(tmp_path / "theses.json")
    hwm = load_hwm_state(tmp_path / "hwm_state.json")
    from agentic_portfolio.session import load_session_state

    st2 = load_session_state(spath)
    assert sleeves2.get("AAPL").sleeve.value == "TACTICAL"
    assert theses2.get(rec.thesis_id).status.value == "ACTIVE"
    assert hwm["cash_flow_adjusted_hwm"] == c2.cash_flow_adjusted_hwm
    assert st2.sod_nav == 500.0
    assert st2.session_id == "2026-08-28"


def test_naive_datetime_fails_safe(tmp_path):
    naive = datetime(2026, 8, 31, 9, 30)
    st = observe_nav_for_session(current_nav=500.0, now=naive, persist_path=tmp_path / "s.json")
    assert st.fail_safe is True
    assert st.sod_nav is None


def test_latest_completed_session_weekend_is_friday():
    cal = NyseEquityCalendar()
    sunday = datetime(2026, 8, 30, 12, 0, tzinfo=EASTERN)
    completed = cal.latest_completed_session(sunday)
    assert completed is not None
    assert completed.session_id == "2026-08-28"
    friday_open = datetime(2026, 8, 28, 11, 0, tzinfo=EASTERN)
    during = cal.latest_completed_session(friday_open)
    assert completed is not None
    assert during.session_id == "2026-08-27"
    friday_close = datetime(2026, 8, 28, 16, 30, tzinfo=EASTERN)
    after = cal.latest_completed_session(friday_close)
    assert after.session_id == "2026-08-28"


def test_regular_hours_excludes_weekend_and_after_hours():
    cal = NyseEquityCalendar()
    friday_rth = datetime(2026, 8, 28, 15, 59, tzinfo=EASTERN)
    friday_close = datetime(2026, 8, 28, 16, 0, tzinfo=EASTERN)
    sunday = datetime(2026, 8, 30, 16, 0, tzinfo=EASTERN)
    premarket = datetime(2026, 8, 28, 8, 0, tzinfo=EASTERN)
    assert is_regular_hours(friday_rth, cal) is True
    assert is_regular_hours(friday_close, cal) is False
    assert is_regular_hours(sunday, cal) is False
    assert is_regular_hours(premarket, cal) is False


def test_next_regular_open_is_today_when_still_premarket():
    from datetime import date, time

    from agentic_portfolio.calendar import next_regular_open_at

    cal = NyseEquityCalendar()
    pre = datetime(2026, 9, 1, 8, 49, tzinfo=EASTERN)
    nxt = cal.next_regular_open(pre)
    assert nxt is not None
    local = nxt.astimezone(EASTERN)
    assert local.date() == date(2026, 9, 1)
    assert local.time() == time(9, 30)
    # next_session still means the following trading day, even before today's open.
    skipped = cal.next_session(pre)
    assert skipped is not None
    assert skipped.session_id == "2026-09-02"
    helper = next_regular_open_at(pre)
    assert helper.astimezone(EASTERN).date() == date(2026, 9, 1)


def test_next_regular_open_after_close_skips_weekend_and_holiday():
    from datetime import date, time

    cal = NyseEquityCalendar()
    friday_after = datetime(2026, 9, 4, 17, 0, tzinfo=EASTERN)
    nxt = cal.next_regular_open(friday_after)
    local = nxt.astimezone(EASTERN)
    # Monday 2026-09-07 is Labor Day.
    assert local.date() == date(2026, 9, 8)
    assert local.time() == time(9, 30)
