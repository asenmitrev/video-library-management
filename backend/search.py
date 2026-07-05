"""Text-to-video-segment search."""

from . import config, db, embedder


def search(
    query: str, limit: int = config.DEFAULT_SEARCH_LIMIT, folder: str | None = None
) -> list[dict]:
    query_emb = embedder.embed_text(query)
    conn = db.connect()
    try:
        return db.search(conn, query_emb, limit, folder=folder)
    finally:
        conn.close()
