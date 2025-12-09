import sqlite3
from typing import Optional, Dict, Any, List

from config import DB_PATH

ROLE_PARTICIPANT = "participant"
ROLE_HOST = "host"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK (role IN ('participant', 'host')),
                display_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS nominations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nomination_id INTEGER NOT NULL,
                participant_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (nomination_id) REFERENCES nominations(id) ON DELETE CASCADE,
                FOREIGN KEY (participant_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nomination_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                video_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (nomination_id) REFERENCES nominations(id) ON DELETE CASCADE,
                FOREIGN KEY (voter_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE,
                UNIQUE (nomination_id, voter_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )

        nomination_names = {
            1: "Еда",
            2: "Животные",
            3: "Блогерский контент",
            4: "DIY",
            5: "Образовательный",
            6: "Милые видео",
            7: "Реклама",
            8: "Смешные видео",
            9: "Комментарии",
            10: "Релакс",
            11: "Грустные видео",
            12: "Нарезки",
            13: "Кринж",
            14: "Странные",
            15: "Сюжетное",
            16: "Вау видео",
            17: "Музыкальное",
        }

        for nom_id, name in nomination_names.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO nominations (id, name)
                VALUES (?, ?)
                """,
                (nom_id, name),
            )
            conn.execute(
                """
                UPDATE nominations
                SET name = ?
                WHERE id = ?
                """,
                (name, nom_id),
            )

        conn.commit()

def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def create_user(telegram_id: int, role: str, display_name: str) -> Dict[str, Any]:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, role, display_name)
            VALUES (?, ?, ?)
            """,
            (telegram_id, role, display_name),
        )
        conn.commit()

        cur = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        return dict(row)


def update_user_role(telegram_id: int, role: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET role = ? WHERE telegram_id = ?",
            (role, telegram_id),
        )
        conn.commit()


def get_participant_count() -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = ?",
            (ROLE_PARTICIPANT,),
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0


def get_host() -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM users WHERE role = ?",
            (ROLE_HOST,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_nominations() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM nominations ORDER BY id"
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def get_nomination_by_id(nomination_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM nominations WHERE id = ?",
            (nomination_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

def get_participant_videos_count(participant_id: int, nomination_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM videos
            WHERE participant_id = ? AND nomination_id = ?
            """,
            (participant_id, nomination_id),
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0


def create_video(
    nomination_id: int,
    participant_id: int,
    title: str,
    url: str,
) -> Dict[str, Any]:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO videos (nomination_id, participant_id, title, url)
            VALUES (?, ?, ?, ?)
            """,
            (nomination_id, participant_id, title, url),
        )
        conn.commit()

        cur = conn.execute(
            """
            SELECT * FROM videos
            WHERE nomination_id = ? AND participant_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (nomination_id, participant_id),
        )
        row = cur.fetchone()
        return dict(row)


def get_participant_videos_for_nomination(
    participant_id: int,
    nomination_id: int,
) -> list[Dict[str, Any]]:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT v.*
            FROM videos v
            WHERE v.participant_id = ? AND v.nomination_id = ?
            ORDER BY v.id
            """,
            (participant_id, nomination_id),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def get_all_videos_with_meta() -> list[Dict[str, Any]]:
    """
    Все прикреплённые видео с номинацией и участником.
    Используется ведущим для просмотра текущих работ.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT
                n.id AS nomination_id,
                n.name AS nomination_name,
                u.id AS participant_id,
                u.display_name AS participant_name,
                v.id AS video_id,
                v.title AS title,
                v.url AS url
            FROM videos v
            JOIN nominations n ON v.nomination_id = n.id
            JOIN users u ON v.participant_id = u.id
            ORDER BY n.id, u.display_name, v.id
            """
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
