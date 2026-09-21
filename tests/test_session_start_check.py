"""
tests/test_session_start_check.py — regression test for scripts/session_start_check.py.

Covers the dated-reminder session-start safety net:
  - parses reminders from PROJECT_STATUS.md
  - overdue (today >= date, not DONE) -> surfaces alert, exit 1
  - marked DONE -> no alert, exit 0
  - future date -> no alert, exit 0

The script calls dt.datetime.now(); we patch datetime.datetime.now at the
module level (m.dt.datetime) so main() sees the simulated date. No temp files
at session root; uses a temp copy of PROJECT_STATUS.md for the DONE-path test.
"""
import os
import sys
import datetime
import tempfile
import importlib.util
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "session_start_check.py")
STATUS = os.path.join(REPO, "PROJECT_STATUS.md")


def _load_module():
    spec = importlib.util.spec_from_file_location("ssc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_datetime(year, month, day):
    class FakeDateTime(datetime.datetime):
        @classmethod
        def now(cls, *a, **k):
            return cls(year, month, day, 9, 0, 0)
    return FakeDateTime


def test_parses_three_reminders():
    m = _load_module()
    reminders = m.parse_dated_reminders(open(STATUS, encoding="utf-8").read())
    assert len(reminders) == 3


def test_overdue_surfaces_alert_exit1():
    m = _load_module()
    m.dt.datetime = _fake_datetime(2026, 7, 22)  # past hy3 07-21
    with pytest.raises(SystemExit) as e:
        m.main()
    assert e.value.code == 1


def test_marked_done_no_alert_exit0():
    m = _load_module()
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".md", dir=tempfile.gettempdir(), delete=False, encoding="utf-8")
    # Synthetic markdown with all past dates marked DONE
    text = """## Dated reminders (authoritative)
- 2026-07-21: tencent-hy3-free model retired [DONE:2026-07-21]
- 2026-07-28: laguna-m.1 model retired [DONE:2026-07-28]
- 2026-10-15: future unexpired reminder
"""
    tmp.write(text)
    tmp.close()
    m.STATUS_PATH = tmp.name
    m.dt.datetime = _fake_datetime(2026, 7, 22)
    try:
        with pytest.raises(SystemExit) as e:
            m.main()
        assert e.value.code == 0
    finally:
        os.remove(tmp.name)


def test_future_date_no_alert_exit0():
    m = _load_module()
    m.STATUS_PATH = STATUS
    m.dt.datetime = _fake_datetime(2026, 7, 18)  # before any reminder
    with pytest.raises(SystemExit) as e:
        m.main()
    assert e.value.code == 0


if __name__ == "__main__":
    # allow direct run: python tests/test_session_start_check.py
    PY = sys.executable
    sys.exit(subprocess.call([PY, "-m", "pytest", __file__, "-q"]))
