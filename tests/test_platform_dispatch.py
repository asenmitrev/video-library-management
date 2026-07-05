"""Tests for the Linux platform-dispatch helpers in backend.server:
zenity folder picking (with the manual-entry fallback signal) and
timestamped playback via mpv/vlc. All external binaries are mocked."""

import subprocess
from types import SimpleNamespace

from backend import server


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------- _is_mac ----------


def test_is_mac_follows_sys_platform(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    assert server._is_mac()
    monkeypatch.setattr(server.sys, "platform", "linux")
    assert not server._is_mac()


# ---------- _choose_folder_zenity ----------


def test_zenity_missing_requests_manual_entry(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    assert server._choose_folder_zenity() == {"folder": None, "manual": True}


def test_zenity_returns_chosen_folder(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/zenity")
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: _completed(stdout="/home/me/videos\n"),
    )
    assert server._choose_folder_zenity() == {"folder": "/home/me/videos"}


def test_zenity_cancel_returns_null_folder_without_manual(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/zenity")
    monkeypatch.setattr(
        server.subprocess, "run", lambda *a, **k: _completed(returncode=1)
    )
    assert server._choose_folder_zenity() == {"folder": None}


def test_zenity_failure_requests_manual_entry(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/zenity")
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr="cannot open display"),
    )
    assert server._choose_folder_zenity() == {"folder": None, "manual": True}


def test_zenity_oserror_requests_manual_entry(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/zenity")

    def boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(server.subprocess, "run", boom)
    assert server._choose_folder_zenity() == {"folder": None, "manual": True}


# ---------- _open_at_timestamp_linux ----------


def test_seek_prefers_mpv(monkeypatch):
    monkeypatch.setattr(
        server.shutil, "which", lambda name: "/usr/bin/mpv" if name == "mpv" else None
    )
    calls = []
    monkeypatch.setattr(
        server.subprocess, "Popen", lambda cmd, **k: calls.append(cmd)
    )
    assert server._open_at_timestamp_linux("/v/clip.mp4", 12.5)
    assert calls == [["/usr/bin/mpv", "--start=12.50", "/v/clip.mp4"]]


def test_seek_falls_back_to_vlc(monkeypatch):
    monkeypatch.setattr(
        server.shutil, "which", lambda name: "/usr/bin/vlc" if name == "vlc" else None
    )
    calls = []
    monkeypatch.setattr(
        server.subprocess, "Popen", lambda cmd, **k: calls.append(cmd)
    )
    assert server._open_at_timestamp_linux("/v/clip.mp4", 12.5)
    assert calls == [["/usr/bin/vlc", "--start-time=12.50", "/v/clip.mp4"]]


def test_seek_without_player_reports_failure(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)

    def unexpected(*a, **k):
        raise AssertionError("should not launch anything")

    monkeypatch.setattr(server.subprocess, "Popen", unexpected)
    assert not server._open_at_timestamp_linux("/v/clip.mp4", 12.5)


def test_seek_launch_failure_reports_failure(monkeypatch):
    monkeypatch.setattr(
        server.shutil, "which", lambda name: "/usr/bin/mpv" if name == "mpv" else None
    )

    def boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(server.subprocess, "Popen", boom)
    assert not server._open_at_timestamp_linux("/v/clip.mp4", 12.5)
