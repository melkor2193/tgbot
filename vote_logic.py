# vote_logic.py
from typing import List, Dict, Optional
from db import get_connection


def get_videos_for_nomination_excluding_user(nomination_id: int, voter_id: int) -> List[Dict]:
    """Возвращает все видео в номинации, кроме видео самого голосующего."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT v.id, v.title, v.url, u.display_name AS author_name, u.id AS author_id
            FROM videos v
            JOIN users u ON v.participant_id = u.id
            WHERE v.nomination_id = ?
              AND v.participant_id != ?
            ORDER BY v.id
            """,
            (nomination_id, voter_id),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def save_vote(nomination_id: int, voter_id: int, video_id: int) -> None:
    """Сохраняет голос (1 на номинацию)."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO votes (nomination_id, voter_id, video_id)
            VALUES (?, ?, ?)
            """,
            (nomination_id, voter_id, video_id),
        )
        conn.commit()


def get_vote_stats(nomination_id: int) -> List[Dict]:
    """Считает количество голосов по каждому видео."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT v.id, v.title, u.display_name AS author_name, COUNT(vo.id) AS votes_count
            FROM videos v
            JOIN users u ON v.participant_id = u.id
            LEFT JOIN votes vo ON vo.video_id = v.id AND vo.nomination_id = v.nomination_id
            WHERE v.nomination_id = ?
            GROUP BY v.id
            ORDER BY votes_count DESC, v.id
            """,
            (nomination_id,),
        )
        return [dict(r) for r in cur.fetchall()]
