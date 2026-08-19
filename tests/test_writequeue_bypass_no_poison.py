"""Verify WriteQueue bypasses under pytest WITHOUT the sys.modules['pytest']=True hack.

Criterion 2: fix must not break WriteQueue auto-detect.
Under pytest, 'pytest' is naturally in sys.modules, so the auto-detect
`any(key in sys.modules for key in ('pytest','_pytest'))` returns True on its own.
This test proves the bypass engages and no worker thread starts.
"""
import sys
import threading


def test_no_poisoning_present():
    # The bool hack must NOT be present: sys.modules['pytest'] must be the real module.
    import pytest as real_pytest
    assert real_pytest is sys.modules['pytest']
    assert not isinstance(sys.modules['pytest'], bool), "sys.modules['pytest'] is poisoned with a bool!"
    assert hasattr(real_pytest, 'fixture'), "pytest must resolve to the real module with .fixture"


def test_writequeue_bypasses_under_pytest():
    from engine.write_queue import WriteQueue
    # Reset singleton to force fresh __init__ auto-detect path
    WriteQueue._instance = None
    wq = WriteQueue()
    # Auto-detect: 'pytest' is naturally in sys.modules under pytest -> bypass True
    assert wq._bypass is True, f"WriteQueue should auto-bypass under pytest, got _bypass={wq._bypass}"
    # No worker thread should be running in bypass mode
    assert wq._worker_thread is None or not wq._worker_thread.is_alive(), \
        "WriteQueue worker thread must not run under pytest bypass"


def test_conftest_class_level_bypass():
    # conftest.py also forces class-level bypass as belt-and-suspenders
    from engine.write_queue import WriteQueue
    assert WriteQueue._bypass is True, "conftest should set WriteQueue._bypass=True at class level"
