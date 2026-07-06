"""Tests for db.search pagination: offset slicing, has_more, page ordering,
and folder-scoped paging."""

import numpy as np
import pytest

from backend import config, db

N_SEGMENTS = 30
PAGE = 12


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def query(conn):
    """A query vector plus indexed segments ranked by decreasing similarity
    to it: segment i is the i-th best match."""
    rng = np.random.default_rng(42)
    q = rng.normal(size=config.EMBEDDING_DIM).astype(np.float32)
    q /= np.linalg.norm(q)
    for i in range(N_SEGMENTS):
        path = f"/{'a' if i < 10 else 'b'}/f{i}.mp4"
        fid = db.add_file(conn, path, mtime=1.0, size=100, duration_sec=60.0)
        emb = (1.0 - i * 0.03) * q + (i * 0.03) * rng.normal(
            size=config.EMBEDDING_DIM
        ).astype(np.float32)
        emb /= np.linalg.norm(emb)
        db.add_segment(conn, fid, i * 2.0, i * 2.0 + 2, None, emb)
    conn.commit()
    return q


def test_pages_are_disjoint_ordered_and_complete(conn, query):
    p1, more1 = db.search(conn, query, limit=PAGE, offset=0)
    p2, more2 = db.search(conn, query, limit=PAGE, offset=PAGE)
    p3, more3 = db.search(conn, query, limit=PAGE, offset=2 * PAGE)

    assert [len(p1), len(p2), len(p3)] == [12, 12, 6]
    assert (more1, more2, more3) == (True, True, False)

    ids = [r["segment_id"] for r in p1 + p2 + p3]
    assert len(set(ids)) == N_SEGMENTS

    scores = [r["score"] for r in p1 + p2 + p3]
    assert scores == sorted(scores, reverse=True)


def test_offset_past_end_returns_empty(conn, query):
    results, has_more = db.search(conn, query, limit=PAGE, offset=3 * PAGE)
    assert results == [] and not has_more


def test_exactly_one_page_has_no_more(conn, query):
    results, has_more = db.search(conn, query, limit=N_SEGMENTS, offset=0)
    assert len(results) == N_SEGMENTS and not has_more


def test_folder_scoped_pagination(conn, query):
    p1, more1 = db.search(conn, query, limit=6, offset=0, folder="/a")
    p2, more2 = db.search(conn, query, limit=6, offset=6, folder="/a")

    assert len(p1) == 6 and more1
    assert len(p2) == 4 and not more2
    assert all(r["path"].startswith("/a/") for r in p1 + p2)
