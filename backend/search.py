"""Text-to-video-segment search."""

from . import config, db, embedder


def search(query: str, limit: int = config.DEFAULT_SEARCH_LIMIT) -> list[dict]:
    query_emb = embedder.embed_text(query)
    conn = db.connect()
    try:
        return db.search(conn, query_emb, limit)
    finally:
        conn.close()
