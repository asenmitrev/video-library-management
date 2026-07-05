"""Tests for db.remove_folder: nested watched folders must survive removal
of a parent (and vice versa) — only files not covered by any remaining
watched folder get deleted."""

import pytest

from backend import db


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def add_file(conn, path):
    file_id = db.add_file(conn, path, mtime=1.0, size=100, duration_sec=None)
    conn.commit()
    return file_id


def file_paths(conn):
    return [r["path"] for r in db.all_files(conn)]


def test_removing_parent_keeps_nested_watched_folder(conn):
    db.add_folder(conn, "/a")
    db.add_folder(conn, "/a/b")
    add_file(conn, "/a/x.mp4")
    add_file(conn, "/a/b/y.mp4")

    db.remove_folder(conn, "/a")

    assert file_paths(conn) == ["/a/b/y.mp4"]
    assert db.list_folders(conn) == ["/a/b"]


def test_removing_child_keeps_files_covered_by_parent(conn):
    db.add_folder(conn, "/a")
    db.add_folder(conn, "/a/b")
    add_file(conn, "/a/x.mp4")
    add_file(conn, "/a/b/y.mp4")

    db.remove_folder(conn, "/a/b")

    assert file_paths(conn) == ["/a/b/y.mp4", "/a/x.mp4"]
    assert db.list_folders(conn) == ["/a"]


def test_removing_folder_without_nesting_deletes_its_files(conn):
    db.add_folder(conn, "/a")
    db.add_folder(conn, "/c")
    add_file(conn, "/a/x.mp4")
    add_file(conn, "/c/z.mp4")

    db.remove_folder(conn, "/a")

    assert file_paths(conn) == ["/c/z.mp4"]
    assert db.list_folders(conn) == ["/c"]


def test_sibling_folder_with_common_prefix_is_untouched(conn):
    db.add_folder(conn, "/a")
    db.add_folder(conn, "/ab")
    add_file(conn, "/a/x.mp4")
    add_file(conn, "/ab/y.mp4")

    db.remove_folder(conn, "/a")

    assert file_paths(conn) == ["/ab/y.mp4"]
    assert db.list_folders(conn) == ["/ab"]
