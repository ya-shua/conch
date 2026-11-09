"""单实例：本体 / 托盘 / 守卫 都只能有一个真身。

（venv 的 pythonw.exe 是个壳，会再拉起真正的解释器，所以任务管理器里
 看到「同名两行」是正常的 —— 那是父子壳，不是两个实例。这里查的是锁。）
"""

import os
import sys
from pathlib import Path

import main as conch_main

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_host_lock_takes_once(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    assert conch_main._take_host_lock(str(cfg)) is True


def test_host_lock_refuses_when_other_alive(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    lock = tmp_path / "data" / "host.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getppid()), encoding="utf-8")  # 父进程肯定活着，且不是自己
    assert conch_main._take_host_lock(str(cfg)) is False


def test_host_lock_clears_stale(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    lock = tmp_path / "data" / "host.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("999999", encoding="utf-8")  # 早就没有这个进程了（privacy-ok：不是 QQ 号）
    assert conch_main._take_host_lock(str(cfg)) is True
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_tray_lock_refuses_second(tmp_path, monkeypatch):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import tray

    lock = tmp_path / "tray.lock"
    monkeypatch.setattr(tray, "LOCK", lock)
    assert tray.take_lock() is True
    lock.write_text(str(os.getppid()), encoding="utf-8")  # 换成一个活着的别人
    assert tray.take_lock() is False


def test_guard_lock_refuses_second(tmp_path, monkeypatch):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import window_guard

    lock = tmp_path / "guard.lock"
    monkeypatch.setattr(window_guard, "_lock_path", lambda: lock)
    assert window_guard._take_lock() is True
    lock.write_text(str(os.getppid()), encoding="utf-8")
    assert window_guard._take_lock() is False