import os
import sqlite3
import random
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

try:
    from linebot.v3.webhooks import MemberJoinedEvent, MemberLeftEvent
except Exception:
    MemberJoinedEvent = None
    MemberLeftEvent = None

load_dotenv()

# =========================
# ENV
# =========================
TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

COUNT_SOURCE_ID = os.getenv("COUNT_SOURCE_ID", "").strip()
ADMIN_SOURCE_ID = os.getenv("ADMIN_SOURCE_ID", "").strip()

# 운영진방 여러 개 지원
# Railway Variables 예:
# ADMIN_SOURCE_ID=C방ID1,C방ID2
ADMIN_SOURCE_IDS = {
    x.strip() for x in ADMIN_SOURCE_ID.split(",") if x.strip()
}

ADMIN_USER_IDS = {
    x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
}

OPERATOR_USER_IDS = {
    x.strip() for x in os.getenv("OPERATOR_USER_IDS", "").split(",") if x.strip()
}

DB_PATH = os.getenv("DB_PATH", "madi_counter.db").strip()
PORT = int(os.getenv("PORT", "5000"))

MALE_LIMIT = int(os.getenv("MALE_LIMIT", "70"))
FEMALE_LIMIT = int(os.getenv("FEMALE_LIMIT", "50"))
WARNING_LIMIT = int(os.getenv("WARNING_LIMIT", "10"))
CURRENCY_NAME = os.getenv("CURRENCY_NAME", "코인").strip()
BOT_VERSION = "active-id-v18-nickdelete-confirm"

# 1코인 = 10포인트, 0.2코인 = 2포인트
COIN_SCALE = 10


def coin_to_points(value):
    try:
        return int(round(float(str(value).replace("코인", "").strip()) * COIN_SCALE))
    except Exception:
        raise ValueError("코인 금액은 숫자로 입력해주세요.")


def points_to_coin(points):
    points = int(points)
    if points % COIN_SCALE == 0:
        return str(points // COIN_SCALE)
    value = points / COIN_SCALE
    return f"{value:.1f}".rstrip("0").rstrip(".")


def coin_text(points):
    return f"{points_to_coin(points)}{CURRENCY_NAME}"

if not TOKEN or not SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET 값을 설정해야 합니다.")

KST = timezone(timedelta(hours=9))

app = Flask(__name__)
handler = WebhookHandler(SECRET)
config = Configuration(access_token=TOKEN)

# 닉삭제 다중 검색/확인 임시 저장소
# key: 운영진 user_id / value: {mode, candidates|target}
DELETE_PENDING = {}


# =========================
# 권한
# =========================
def is_admin(user_id):
    return user_id in ADMIN_USER_IDS


def is_staff(user_id):
    return user_id in ADMIN_USER_IDS or user_id in OPERATOR_USER_IDS


def count_source_ids():
    ids = set()
    if COUNT_SOURCE_ID:
        ids.add(COUNT_SOURCE_ID)

    # 운영진방 여러 개 카운트 지원
    for admin_source_id in ADMIN_SOURCE_IDS:
        ids.add(admin_source_id)

    return ids


# =========================
# 시간
# =========================
def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def parse_date(text: str):
    parts = text.strip().split()
    if len(parts) >= 2:
        try:
            datetime.strptime(parts[1], "%Y-%m-%d")
            return parts[1]
        except ValueError:
            pass
    return today()


# =========================
# DB
# =========================
def db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def seed_default_shop_items(cur):
    default_items = [
        (
            "단벙주최권",
            60,
            "참가인원 전체 코인 차감없이 벙 / 주최권 단벙은 인원제한 12명"
        ),
        (
            "봇등록권",
            100,
            "꽃봇이 봇에 자기소개 등록가능 / 두개 사면 두 칸 등록 가능"
        ),
        (
            "미션클리어권",
            200,
            "노미클자🔰 > 미클 🔹가능"
        ),
        (
            "닉변권",
            200,
            "닉네임 변경 가능 / 유사닉·혐오닉 등 제한 있음 / 재변경 시 다시 구입"
        ),
        (
            "임티권",
            500,
            "닉네임 앞이나 나이를 지우고 임티 달 수 있음 / 임티 제한 있음 / 고유임티 / 재변경 시 다시 구입"
        ),
        (
            "칭호권",
            500,
            "닉네임 뒤 [ ] 사이에 호칭 넣기 가능 / 띄어쓰기 가능 5글자 제한 / 워딩 제한 있음 / 재변경 시 다시 구입"
        ),
    ]

    for name, price, description in default_items:
        cur.execute("""
        INSERT INTO shop_items (name, price, description, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(name)
        DO UPDATE SET
            price = excluded.price,
            description = excluded.description,
            is_active = 1
        """, (name, price, description, now_str()))


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL,
        gender TEXT DEFAULT 'unknown',
        is_nomicl INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        last_seen_source_id TEXT,
        updated_at TEXT NOT NULL
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        source_id TEXT NOT NULL,
        user_id TEXT,
        user_name TEXT,
        message_type TEXT,
        text TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS counts (
        date TEXT NOT NULL,
        source_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (date, source_id, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS currency (
        user_id TEXT PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS currency_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        amount INTEGER NOT NULL,
        reason TEXT,
        staff_user_id TEXT,
        staff_user_name TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        price INTEGER NOT NULL,
        description TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        item_name TEXT NOT NULL,
        price INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'owned',
        created_at TEXT NOT NULL,
        processed_at TEXT,
        processed_by TEXT,
        used_at TEXT,
        used_by TEXT,
        use_note TEXT
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_flags (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        date TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        reward INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (date, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS mission_claims (
        date TEXT NOT NULL,
        user_id TEXT NOT NULL,
        mission_key TEXT NOT NULL,
        user_name TEXT NOT NULL,
        reward INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (date, user_id, mission_key)
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS hidden_rewards (
        date TEXT NOT NULL,
        mission_key TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        reward INTEGER NOT NULL,
        meta TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (date, mission_key)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_lucky_numbers (
        date TEXT PRIMARY KEY,
        lucky_number INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS gacha_settings (
        user_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL,
        gacha_type TEXT NOT NULL DEFAULT 'random',
        updated_at TEXT NOT NULL
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS gacha_pity (
        user_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL,
        pity_points INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS gacha_pieces (
        user_id TEXT NOT NULL,
        piece_key TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, piece_key)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS weekly_rewards (
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        rank INTEGER NOT NULL,
        count INTEGER NOT NULL,
        reward INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (week_start, week_end, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sns_lucky_draw_entries (
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        tickets INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY (week_start, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sns_lucky_draw_results (
        week_start TEXT PRIMARY KEY,
        week_end TEXT NOT NULL,
        winner_user_id TEXT NOT NULL,
        winner_user_name TEXT NOT NULL,
        participants INTEGER NOT NULL,
        total_sales INTEGER NOT NULL,
        prize INTEGER NOT NULL,
        burned INTEGER NOT NULL,
        settled_by TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sns_pinball_entries (
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        tickets INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (week_start, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sns_pinball_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        winner_user_id TEXT NOT NULL,
        winner_user_name TEXT NOT NULL,
        winner_count INTEGER NOT NULL,
        total_sales INTEGER NOT NULL,
        total_prize INTEGER NOT NULL,
        prize_each INTEGER NOT NULL,
        burned INTEGER NOT NULL,
        settled_by TEXT,
        created_at TEXT NOT NULL
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS achievements (
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        achievement_key TEXT NOT NULL,
        achievement_name TEXT NOT NULL,
        reward INTEGER NOT NULL DEFAULT 0,
        meta TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, achievement_key)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS weekly_bounties (
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        hunter_user_id TEXT NOT NULL,
        hunter_user_name TEXT NOT NULL,
        target_user_id TEXT NOT NULL,
        target_user_name TEXT NOT NULL,
        mention_count INTEGER NOT NULL DEFAULT 0,
        required_count INTEGER NOT NULL DEFAULT 5,
        reward INTEGER NOT NULL DEFAULT 10,
        completed INTEGER NOT NULL DEFAULT 0,
        last_text_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY (week_start, hunter_user_id)
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_last_speakers (
        source_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        last_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS affinity_scores (
        week_start TEXT NOT NULL,
        user_a TEXT NOT NULL,
        user_b TEXT NOT NULL,
        user_a_name TEXT NOT NULL,
        user_b_name TEXT NOT NULL,
        score INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (week_start, user_a, user_b)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS affinity_pair_cooldowns (
        source_id TEXT NOT NULL,
        week_start TEXT NOT NULL,
        user_a TEXT NOT NULL,
        user_b TEXT NOT NULL,
        last_at TEXT NOT NULL,
        PRIMARY KEY (source_id, week_start, user_a, user_b)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS manitto_assignments (
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        hunter_user_id TEXT NOT NULL,
        hunter_user_name TEXT NOT NULL,
        target_user_id TEXT NOT NULL,
        target_user_name TEXT NOT NULL,
        required_score INTEGER NOT NULL DEFAULT 15,
        reward_min INTEGER NOT NULL DEFAULT 10,
        reward_max INTEGER NOT NULL DEFAULT 50,
        reward INTEGER,
        manitto_type TEXT NOT NULL DEFAULT 'normal',
        completed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY (week_start, hunter_user_id)
    )
    """)

    # 기존 DB 마이그레이션: 예전 버전 DB를 새 코드에 맞게 자동 보정
    cur.execute("PRAGMA table_info(users)")
    user_cols = {row["name"] for row in cur.fetchall()}

    for col, col_type, default_value in [
        ("gender", "TEXT", "'unknown'"),
        ("is_nomicl", "INTEGER", "0"),
        ("is_active", "INTEGER", "1"),
    ]:
        if col not in user_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type} DEFAULT {default_value}")

    cur.execute("PRAGMA table_info(purchases)")
    purchase_cols = {row["name"] for row in cur.fetchall()}

    for col, col_type in [
        ("used_at", "TEXT"),
        ("used_by", "TEXT"),
        ("use_note", "TEXT"),
    ]:
        if col not in purchase_cols:
            cur.execute(f"ALTER TABLE purchases ADD COLUMN {col} {col_type}")


    # 기존 정수 코인 DB를 0.1 단위 포인트 시스템으로 1회 변환
    cur.execute("SELECT value FROM system_flags WHERE key = 'currency_scaled_v1'")
    scaled = cur.fetchone()

    if not scaled:
        cur.execute("UPDATE currency SET balance = balance * 10")
        cur.execute("UPDATE currency_logs SET amount = amount * 10")
        cur.execute("UPDATE shop_items SET price = price * 10")
        cur.execute("UPDATE purchases SET price = price * 10")
        cur.execute(
            "INSERT INTO system_flags (key, value) VALUES ('currency_scaled_v1', 'done')"
        )

    conn.commit()
    conn.close()


init_db()


# =========================
# LINE 공통
# =========================
def get_source_id(event):
    """그룹/룸/1:1 대화의 source id를 안전하게 반환합니다."""
    source = event.source

    if source.type == "group":
        return getattr(source, "group_id", None) or "NO_SOURCE_ID"

    if source.type == "room":
        return getattr(source, "room_id", None) or "NO_SOURCE_ID"

    return getattr(source, "user_id", None) or "NO_SOURCE_ID"


def get_event_user_id(event):
    """이벤트 발신자 userId를 안전하게 반환합니다.

    LINE이 userId를 주지 않는 이벤트면 None을 반환합니다.
    이메일/전화번호 등록 여부와는 무관합니다.
    """
    user_id = getattr(event.source, "user_id", None)

    if not user_id or str(user_id).strip() in ("", "NO_USER_ID", "None"):
        return None

    return str(user_id).strip()


def get_user_name(event):
    """그룹/룸/1:1 환경별 프로필 조회. 실패 시 닉네임 기본값을 분리합니다."""
    user_id = get_event_user_id(event)
    source = event.source

    if not user_id:
        return "NO_NICKNAME"

    try:
        with ApiClient(config) as client:
            api = MessagingApi(client)

            if source.type == "group":
                group_id = getattr(source, "group_id", None)
                if group_id:
                    profile = api.get_group_member_profile(group_id, user_id)
                    return profile.display_name or f"user_{user_id[-4:]}"

            if source.type == "room":
                room_id = getattr(source, "room_id", None)
                if room_id:
                    profile = api.get_room_member_profile(room_id, user_id)
                    return profile.display_name or f"user_{user_id[-4:]}"

            profile = api.get_profile(user_id)
            return profile.display_name or f"user_{user_id[-4:]}"

    except Exception as e:
        print("닉네임 조회 실패:", e)
        return f"user_{user_id[-4:]}"


def reply(reply_token, text):
    if len(text) > 4900:
        text = text[:4800] + "\n...\n내용이 길어서 일부만 표시됐습니다."

    with ApiClient(config) as client:
        api = MessagingApi(client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )


# =========================
# 유저 / 카운트
# =========================
def upsert_user(user_id, user_name, source_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO users (
        user_id, user_name, gender, is_nomicl, is_active, last_seen_source_id, updated_at
    )
    VALUES (?, ?, 'unknown', 0, 1, ?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        user_name = excluded.user_name,
        is_active = 1,
        last_seen_source_id = excluded.last_seen_source_id,
        updated_at = excluded.updated_at
    """, (user_id, user_name, source_id, now_str()))

    cur.execute("UPDATE counts SET user_name = ? WHERE user_id = ?", (user_name, user_id))

    conn.commit()
    conn.close()



def clean_keyword(text_value):
    return "".join(ch for ch in str(text_value) if ch.isalnum() or ("가" <= ch <= "힣")).lower()


def find_users(keyword, limit=10):
    clean = clean_keyword(keyword)
    results = {}
    conn = db()
    cur = conn.cursor()

    search_sqls = [
        ("""
        SELECT user_id, user_name, updated_at, COALESCE(is_active, 1) AS is_active
        FROM users
        WHERE user_name LIKE ?
        ORDER BY updated_at DESC
        LIMIT ?
        """, (f"%{keyword}%", limit)),
        ("""
        SELECT user_id, user_name, MAX(date) AS updated_at, 1 AS is_active
        FROM counts
        WHERE user_name LIKE ?
        GROUP BY user_id
        ORDER BY updated_at DESC
        LIMIT ?
        """, (f"%{keyword}%", limit)),
        ("""
        SELECT user_id, user_name, MAX(created_at) AS updated_at, 1 AS is_active
        FROM currency_logs
        WHERE user_name LIKE ?
        GROUP BY user_id
        ORDER BY updated_at DESC
        LIMIT ?
        """, (f"%{keyword}%", limit)),
        ("""
        SELECT user_id, user_name, MAX(created_at) AS updated_at, 1 AS is_active
        FROM purchases
        WHERE user_name LIKE ?
        GROUP BY user_id
        ORDER BY updated_at DESC
        LIMIT ?
        """, (f"%{keyword}%", limit)),
    ]

    for sql, params in search_sqls:
        if len(results) >= limit:
            break
        try:
            cur.execute(sql, params)
            for row in cur.fetchall():
                if row["user_id"] not in results:
                    results[row["user_id"]] = dict(row)
        except Exception as e:
            print("FIND USERS SQL ERROR:", e)

    # 이모지/기호 제거 검색
    if len(results) < limit and clean:
        for table, time_col in [
            ("users", "updated_at"),
            ("counts", "date"),
            ("currency_logs", "created_at"),
            ("purchases", "created_at"),
        ]:
            if len(results) >= limit:
                break
            try:
                if table == "users":
                    cur.execute("""
                    SELECT user_id, user_name, updated_at, COALESCE(is_active, 1) AS is_active
                    FROM users
                    ORDER BY updated_at DESC
                    """)
                else:
                    cur.execute(f"""
                    SELECT user_id, user_name, MAX({time_col}) AS updated_at, 1 AS is_active
                    FROM {table}
                    GROUP BY user_id
                    ORDER BY updated_at DESC
                    """)

                for row in cur.fetchall():
                    if row["user_id"] not in results and clean in clean_keyword(row["user_name"]):
                        results[row["user_id"]] = dict(row)
                        if len(results) >= limit:
                            break
            except Exception as e:
                print("FIND USERS CLEAN ERROR:", e)

    conn.close()
    return list(results.values())[:limit]


def find_user(keyword):
    rows = find_users(keyword, limit=1)
    return rows[0] if rows else None


def add_count(date_str, source_id, user_id, user_name):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO counts (date, source_id, user_id, user_name, count)
    VALUES (?, ?, ?, ?, 1)
    ON CONFLICT(date, source_id, user_id)
    DO UPDATE SET
        count = count + 1,
        user_name = excluded.user_name
    """, (date_str, source_id, user_id, user_name))
    conn.commit()
    conn.close()


def save_chat_log(date_str, source_id, user_id, user_name, message_type, text_value):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO chat_logs (date, source_id, user_id, user_name, message_type, text, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (date_str, source_id, user_id, user_name, message_type, text_value, now_str()))
    conn.commit()
    conn.close()


def collection_status(source_id, date_str):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT COUNT(*) AS total_logs, COUNT(DISTINCT user_id) AS active_users
    FROM chat_logs
    WHERE source_id=? AND date=?
    """, (source_id, date_str))
    log_row = cur.fetchone()

    cur.execute("""
    SELECT COUNT(*) AS rows_count, COALESCE(SUM(count),0) AS total_madi, COUNT(DISTINCT user_id) AS counted_users
    FROM counts
    WHERE source_id=? AND date=?
    """, (source_id, date_str))
    count_row = cur.fetchone()

    cur.execute("""
    SELECT user_id, user_name, count
    FROM counts
    WHERE source_id=? AND date=?
    ORDER BY count DESC, user_name ASC
    """, (source_id, date_str))
    all_rows = cur.fetchall()

    conn.close()
    return log_row, count_row, all_rows



def collection_missing(source_id, date_str):
    conn = db()
    cur = conn.cursor()

    # users에는 있는데 오늘 counts가 없는 활성 유저
    cur.execute("""
    SELECT u.user_id, u.user_name
    FROM users u
    LEFT JOIN counts c
      ON u.user_id = c.user_id
     AND c.source_id = ?
     AND c.date = ?
    WHERE COALESCE(u.is_active, 1) = 1
      AND c.user_id IS NULL
    ORDER BY u.user_name ASC
    """, (source_id, date_str))
    users_no_count = cur.fetchall()

    # chat_logs에는 있는데 counts가 없는 유저
    cur.execute("""
    SELECT l.user_id, MAX(l.user_name) AS user_name, COUNT(*) AS logs
    FROM chat_logs l
    LEFT JOIN counts c
      ON l.user_id = c.user_id
     AND l.source_id = c.source_id
     AND l.date = c.date
    WHERE l.source_id = ?
      AND l.date = ?
      AND l.user_id IS NOT NULL
      AND c.user_id IS NULL
    GROUP BY l.user_id
    ORDER BY user_name ASC
    """, (source_id, date_str))
    logs_no_count = cur.fetchall()

    # counts에는 있는데 users가 없는 유저
    cur.execute("""
    SELECT c.user_id, c.user_name, c.count
    FROM counts c
    LEFT JOIN users u
      ON c.user_id = u.user_id
    WHERE c.source_id = ?
      AND c.date = ?
      AND u.user_id IS NULL
    ORDER BY c.count DESC, c.user_name ASC
    """, (source_id, date_str))
    counts_no_user = cur.fetchall()

    conn.close()
    return users_no_count, logs_no_count, counts_no_user


def format_long_lines(title, lines, max_chars=4500):
    msg = title + "\\n\\n"
    used = len(msg)
    output = [title, ""]

    for line in lines:
        extra = len(line) + 1
        if used + extra > max_chars:
            output.append("...")
            output.append("내용이 길어서 일부만 표시됐습니다.")
            break
        output.append(line)
        used += extra

    return "\\n".join(output)


def recent_chat_logs(source_id, limit=20):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT created_at, user_name, user_id, text
    FROM chat_logs
    WHERE source_id=?
    ORDER BY id DESC
    LIMIT ?
    """, (source_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def user_debug(keyword):
    users = find_users(keyword, limit=10)
    conn = db()
    cur = conn.cursor()
    result = []

    for user in users:
        cur.execute("SELECT COALESCE(SUM(count),0) AS total_count, COUNT(DISTINCT date) AS active_days FROM counts WHERE user_id=?", (user["user_id"],))
        c = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS log_count, MAX(created_at) AS last_log FROM chat_logs WHERE user_id=?", (user["user_id"],))
        l = cur.fetchone()
        cur.execute("SELECT balance FROM currency WHERE user_id=?", (user["user_id"],))
        b = cur.fetchone()

        result.append({
            "user_id": user["user_id"],
            "user_name": user["user_name"],
            "is_active": user["is_active"],
            "total_count": c["total_count"] if c else 0,
            "active_days": c["active_days"] if c else 0,
            "log_count": l["log_count"] if l else 0,
            "last_log": l["last_log"] if l else None,
            "balance": b["balance"] if b else 0,
        })

    conn.close()
    return result


def set_gender(user_name_keyword, gender):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET gender = ? WHERE user_name LIKE ?", (gender, f"%{user_name_keyword}%"))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def set_nomicl(user_name_keyword, value):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_nomicl = ? WHERE user_name LIKE ?", (value, f"%{user_name_keyword}%"))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def set_user_active_by_id(user_id, value):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE users
    SET is_active = ?,
        updated_at = ?
    WHERE user_id = ?
    """, (value, now_str(), user_id))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def set_user_active_by_name(keyword, value):
    rows = find_users(keyword, limit=20)

    if not rows:
        return 0, []

    conn = db()
    cur = conn.cursor()

    changed = 0
    names = []

    for row in rows:
        cur.execute("""
        UPDATE users
        SET is_active = ?,
            updated_at = ?
        WHERE user_id = ?
        """, (value, now_str(), row["user_id"]))

        changed += cur.rowcount
        names.append(row["user_name"])

    conn.commit()
    conn.close()

    return changed, names




def get_user_by_id(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_id, user_name, COALESCE(is_active, 1) AS is_active
    FROM users
    WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_user_active_by_id(user_id, value):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE users
    SET is_active = ?,
        updated_at = ?
    WHERE user_id = ?
    """, (value, now_str(), user_id))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def set_user_active_by_id_with_name(user_id, value):
    user = get_user_by_id(user_id)

    if not user:
        return 0, None

    changed = set_user_active_by_id(user_id, value)
    return changed, user["user_name"]


def set_user_active_by_name(keyword, value):
    rows = find_users(keyword, limit=20)

    if not rows:
        return 0, []

    conn = db()
    cur = conn.cursor()

    changed = 0
    names = []

    for row in rows:
        cur.execute("""
        UPDATE users
        SET is_active = ?,
            updated_at = ?
        WHERE user_id = ?
        """, (value, now_str(), row["user_id"]))

        changed += cur.rowcount
        names.append(row["user_name"])

    conn.commit()
    conn.close()

    return changed, names



def sync_users_from_history():
    conn = db()
    cur = conn.cursor()
    inserted = 0
    updated = 0

    for table, time_col in [("counts", "date"), ("currency_logs", "created_at"), ("purchases", "created_at")]:
        try:
            cur.execute(f"""
            SELECT user_id, user_name, MAX({time_col}) AS last_time
            FROM {table}
            WHERE user_id IS NOT NULL AND user_id != ''
            GROUP BY user_id
            """)
            rows = cur.fetchall()
        except Exception as e:
            print("SYNC ERROR:", e)
            continue

        for row in rows:
            cur.execute("SELECT user_id FROM users WHERE user_id = ?", (row["user_id"],))
            if cur.fetchone():
                cur.execute("""
                UPDATE users
                SET user_name = ?,
                    is_active = COALESCE(is_active, 1),
                    updated_at = ?
                WHERE user_id = ?
                """, (row["user_name"], now_str(), row["user_id"]))
                updated += cur.rowcount
            else:
                cur.execute("""
                INSERT INTO users (
                    user_id, user_name, gender, is_nomicl, is_active,
                    last_seen_source_id, updated_at
                )
                VALUES (?, ?, 'unknown', 0, 1, ?, ?)
                """, (row["user_id"], row["user_name"], COUNT_SOURCE_ID, now_str()))
                inserted += 1

    conn.commit()
    conn.close()
    return inserted, updated



# =========================
# 마디수 조회
# =========================
def ranking(date_str, source_id, limit=None):
    conn = db()
    cur = conn.cursor()

    # 중요:
    # 기존 코드는 users.last_seen_source_id = source_id 인 사람만 보여줘서
    # 메인방에서 말한 뒤 운영진방에서 /방정보 등을 치면 last_seen_source_id가 운영진방으로 바뀌어
    # 메인방 순위에서 사라질 수 있었습니다.
    # 아래 쿼리는 "해당 방에서 카운트가 있거나, 현재 그 방에 마지막으로 보인 사람"을 모두 표시합니다.
    sql = """
    SELECT
        u.user_id,
        u.user_name,
        u.gender,
        u.is_nomicl,
        COALESCE(c.count, 0) AS count
    FROM users u
    LEFT JOIN counts c
      ON u.user_id = c.user_id
     AND c.date = ?
     AND c.source_id = ?
    WHERE (u.last_seen_source_id = ?
       OR c.user_id IS NOT NULL)
      AND COALESCE(u.is_active, 1) = 1
    ORDER BY count DESC, u.user_name ASC
    """

    params = [date_str, source_id, source_id]

    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def total_ranking(source_id, limit=None):
    conn = db()
    cur = conn.cursor()

    sql = """
    SELECT
        u.user_id,
        u.user_name,
        u.gender,
        u.is_nomicl,
        COALESCE(SUM(c.count), 0) AS count
    FROM users u
    LEFT JOIN counts c
      ON u.user_id = c.user_id
     AND c.source_id = ?
    WHERE (u.last_seen_source_id = ?
       OR c.user_id IS NOT NULL)
      AND COALESCE(u.is_active, 1) = 1
    GROUP BY u.user_id
    ORDER BY count DESC, u.user_name ASC
    """

    params = [source_id, source_id]

    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def warning_list(date_str, source_id):
    rows = ranking(date_str, source_id)
    result = []
    for row in rows:
        if row["count"] < WARNING_LIMIT:
            result.append(row)
    return result


# =========================
# 화폐 기능
# =========================
def get_balance(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["balance"] if row else 0


def change_money(user_id, user_name, amount, reason, staff_user_id=None, staff_user_name=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO currency (user_id, balance, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        balance = balance + excluded.balance,
        updated_at = excluded.updated_at
    """, (user_id, amount, now_str()))

    cur.execute("""
    INSERT INTO currency_logs (
        user_id, user_name, amount, reason,
        staff_user_id, staff_user_name, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, user_name, amount, reason, staff_user_id, staff_user_name, now_str()))

    cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
    balance = cur.fetchone()["balance"]
    conn.commit()
    conn.close()
    return balance


def currency_ranking(limit=20):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT u.user_name, COALESCE(c.balance, 0) AS balance
    FROM users u
    LEFT JOIN currency c ON u.user_id = c.user_id
    WHERE COALESCE(c.balance, 0) != 0
      AND COALESCE(u.is_active, 1) = 1
    ORDER BY balance DESC, u.user_name ASC
    LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def currency_history(user_id, limit=10):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT amount, reason, staff_user_name, created_at
    FROM currency_logs
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def reset_currency():
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM currency")
    deleted_currency = cur.rowcount
    cur.execute("DELETE FROM currency_logs")
    deleted_logs = cur.rowcount
    conn.commit()
    conn.close()
    return deleted_currency, deleted_logs


# =========================
# 상점 기능
# =========================
def add_shop_item(name, price, description):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO shop_items (name, price, description, is_active, created_at)
    VALUES (?, ?, ?, 1, ?)
    ON CONFLICT(name)
    DO UPDATE SET
        price = excluded.price,
        description = excluded.description,
        is_active = 1
    """, (name, price, description, now_str()))
    conn.commit()
    conn.close()


def remove_shop_item(name):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE shop_items SET is_active = 0 WHERE name = ?", (name,))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def list_shop_items():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, name, price, description
    FROM shop_items
    WHERE is_active = 1
    ORDER BY price ASC, name ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_shop_item(name):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, name, price, description
    FROM shop_items
    WHERE name = ?
      AND is_active = 1
    """, (name,))
    row = cur.fetchone()
    conn.close()
    return row


def buy_item(user_id, user_name, item_name):
    item = get_shop_item(item_name)
    if not item:
        return False, "상품을 찾을 수 없습니다."

    balance = get_balance(user_id)
    if balance < item["price"]:
        return False, (
            f"{CURRENCY_NAME}이 부족합니다.\n\n"
            f"보유: {coin_text(balance)}\n"
            f"필요: {coin_text(item['price'])}"
        )

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE currency
    SET balance = balance - ?,
        updated_at = ?
    WHERE user_id = ?
    """, (item["price"], now_str(), user_id))

    cur.execute("""
    INSERT INTO currency_logs (
        user_id, user_name, amount, reason,
        staff_user_id, staff_user_name, created_at
    )
    VALUES (?, ?, ?, ?, NULL, NULL, ?)
    """, (user_id, user_name, -item["price"], f"상점 구매: {item['name']}", now_str()))

    cur.execute("""
    INSERT INTO purchases (
        user_id, user_name, item_name, price, status, created_at
    )
    VALUES (?, ?, ?, ?, 'owned', ?)
    """, (user_id, user_name, item["name"], item["price"], now_str()))

    purchase_id = cur.lastrowid

    cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
    new_balance = cur.fetchone()["balance"]

    conn.commit()
    conn.close()

    return True, (
        f"🛒 구매 완료\n\n"
        f"구매번호: {purchase_id}\n"
        f"상품: {item['name']}\n"
        f"차감: {coin_text(item['price'])}\n"
        f"잔액: {coin_text(new_balance)}\n\n"
        f"보유 확인: /내보유\n"
        f"사용 신청: /사용 {purchase_id}"
    )


def list_purchases(status=None, limit=30):
    conn = db()
    cur = conn.cursor()
    if status:
        cur.execute("""
        SELECT id, user_name, item_name, price, status, created_at, used_at, used_by, use_note
        FROM purchases
        WHERE status = ?
        ORDER BY id DESC
        LIMIT ?
        """, (status, limit))
    else:
        cur.execute("""
        SELECT id, user_name, item_name, price, status, created_at, used_at, used_by, use_note
        FROM purchases
        ORDER BY id DESC
        LIMIT ?
        """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def list_user_purchases(user_id, status=None, limit=30):
    conn = db()
    cur = conn.cursor()
    if status:
        cur.execute("""
        SELECT id, item_name, price, status, created_at, used_at, used_by, use_note
        FROM purchases
        WHERE user_id = ?
          AND status = ?
        ORDER BY id DESC
        LIMIT ?
        """, (user_id, status, limit))
    else:
        cur.execute("""
        SELECT id, item_name, price, status, created_at, used_at, used_by, use_note
        FROM purchases
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def use_purchase(purchase_id, requester_user_id, requester_user_name, note=""):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,))
    purchase = cur.fetchone()

    if not purchase:
        conn.close()
        return False, "구매번호를 찾을 수 없습니다."

    if purchase["user_id"] != requester_user_id:
        conn.close()
        return False, "본인이 구매한 상품만 사용할 수 있습니다."

    if purchase["status"] == "used":
        conn.close()
        return False, f"이미 사용된 상품입니다.\n사용일: {purchase['used_at']}"

    if purchase["status"] == "cancel":
        conn.close()
        return False, "취소된 상품은 사용할 수 없습니다."

    cur.execute("""
    UPDATE purchases
    SET status = 'used',
        used_at = ?,
        used_by = ?,
        use_note = ?
    WHERE id = ?
    """, (now_str(), requester_user_name, note, purchase_id))

    conn.commit()
    conn.close()
    return True, (
        f"✅ 상품 사용 처리 완료\n\n"
        f"구매번호: {purchase_id}\n"
        f"상품: {purchase['item_name']}\n"
        f"사용자: {requester_user_name}"
    )


def staff_use_purchase(purchase_id, staff_user_name, note="운영진 사용 처리"):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,))
    purchase = cur.fetchone()

    if not purchase:
        conn.close()
        return False, "구매번호를 찾을 수 없습니다."

    if purchase["status"] == "used":
        conn.close()
        return False, f"이미 사용된 상품입니다.\n사용일: {purchase['used_at']}"

    if purchase["status"] == "cancel":
        conn.close()
        return False, "취소된 상품은 사용할 수 없습니다."

    cur.execute("""
    UPDATE purchases
    SET status = 'used',
        used_at = ?,
        used_by = ?,
        use_note = ?
    WHERE id = ?
    """, (now_str(), staff_user_name, note, purchase_id))

    conn.commit()
    conn.close()
    return True, (
        f"✅ 사용 처리 완료\n\n"
        f"구매번호: {purchase_id}\n"
        f"구매자: {purchase['user_name']}\n"
        f"상품: {purchase['item_name']}"
    )


def cancel_purchase(purchase_id, staff_user_name):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,))
    purchase = cur.fetchone()

    if not purchase:
        conn.close()
        return False, "구매번호를 찾을 수 없습니다."

    if purchase["status"] == "cancel":
        conn.close()
        return False, "이미 취소된 구매입니다."

    if purchase["status"] == "used":
        conn.close()
        return False, "이미 사용된 상품은 취소할 수 없습니다."

    cur.execute("""
    UPDATE purchases
    SET status = 'cancel',
        processed_at = ?,
        processed_by = ?
    WHERE id = ?
    """, (now_str(), staff_user_name, purchase_id))

    refund_amount = purchase["price"] // 2

    cur.execute("""
    INSERT INTO currency (user_id, balance, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        balance = balance + excluded.balance,
        updated_at = excluded.updated_at
    """, (purchase["user_id"], refund_amount, now_str()))

    cur.execute("""
    INSERT INTO currency_logs (
        user_id, user_name, amount, reason,
        staff_user_id, staff_user_name, created_at
    )
    VALUES (?, ?, ?, ?, NULL, ?, ?)
    """, (
        purchase["user_id"],
        purchase["user_name"],
        refund_amount,
        f"구매 취소 50% 환불: {purchase['item_name']}",
        staff_user_name,
        now_str()
    ))

    conn.commit()
    conn.close()
    return True, f"구매 취소 및 50% 환불 처리했습니다.\n환불: {coin_text(refund_amount)}"


def status_text(status):
    if status == "owned":
        return "보유중"
    if status == "used":
        return "사용완료"
    if status == "cancel":
        return "취소됨"
    if status == "pending":
        return "대기중"
    if status == "done":
        return "완료"
    return status



# =========================
# 출석 / 미션 / 주간정산
# =========================
MISSION_REWARDS = [
    ("daily_100", 100, 1),  # 100마디 = 0.1코인
    ("daily_200", 200, 1),  # 200마디 = 0.1코인
    ("daily_300", 300, 1),  # 300마디 = 0.1코인
]


def get_user_count(date_str, source_id, user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT count
    FROM counts
    WHERE date = ?
      AND source_id = ?
      AND user_id = ?
    """, (date_str, source_id, user_id))
    row = cur.fetchone()
    conn.close()
    return row["count"] if row else 0


def attendance_check(date_str, user_id, user_name):
    reward = 2  # 0.2코인

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT reward
    FROM attendance
    WHERE date = ?
      AND user_id = ?
    """, (date_str, user_id))

    if cur.fetchone():
        conn.close()
        return False, get_balance(user_id)

    cur.execute("""
    INSERT INTO attendance (date, user_id, user_name, reward, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (date_str, user_id, user_name, reward, now_str()))

    conn.commit()
    conn.close()

    balance = change_money(
        user_id,
        user_name,
        reward,
        f"출석체크 {date_str}",
        None,
        "출석시스템"
    )

    return True, balance


def claimed_missions(date_str, user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT mission_key
    FROM mission_claims
    WHERE date = ?
      AND user_id = ?
    """, (date_str, user_id))
    rows = cur.fetchall()
    conn.close()
    return {row["mission_key"] for row in rows}


def mission_status(date_str, source_id, user_id):
    count = get_user_count(date_str, source_id, user_id)
    claimed = claimed_missions(date_str, user_id)

    result = []
    for key, required, reward in MISSION_REWARDS:
        done = count >= required
        received = key in claimed
        result.append({
            "key": key,
            "required": required,
            "reward": reward,
            "done": done,
            "received": received,
        })

    return count, result


def claim_missions(date_str, source_id, user_id, user_name):
    count, missions = mission_status(date_str, source_id, user_id)
    claimable = [m for m in missions if m["done"] and not m["received"]]

    if not claimable:
        return 0, count, []

    conn = db()
    cur = conn.cursor()

    total_reward = 0
    claimed_names = []

    for mission in claimable:
        cur.execute("""
        INSERT OR IGNORE INTO mission_claims (
            date, user_id, mission_key, user_name, reward, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            user_id,
            mission["key"],
            user_name,
            mission["reward"],
            now_str()
        ))

        if cur.rowcount > 0:
            total_reward += mission["reward"]
            claimed_names.append(f"{mission['required']}마디")

    conn.commit()
    conn.close()

    if total_reward > 0:
        change_money(
            user_id,
            user_name,
            total_reward,
            f"일일미션 보상: {', '.join(claimed_names)}",
            None,
            "미션시스템"
        )

    return total_reward, count, claimed_names





# =========================
# 가챠 시스템
# =========================
GACHA_COSTS = {
    "하": 10,  # 1코인
    "중": 30,  # 3코인
    "상": 50,  # 5코인
}

GACHA_TYPE_LABELS = {
    "coin": "코인형",
    "piece": "조각형",
    "random": "랜덤형",
}

PIECE_INFO = {
    "선갠라": {"label": "💠 선갠라조각", "need": 10, "item": "선갠라권"},
    "단벙": {"label": "💠 단벙조각", "need": 12, "item": "단벙주최권"},
    "봇등록": {"label": "📸 봇등록조각", "need": 20, "item": "봇등록권"},
    "미션": {"label": "🛟 미션조각", "need": 40, "item": "미션클리어권"},
    "임티": {"label": "🎟 임티조각", "need": 100, "item": "임티권"},
    "칭호": {"label": "🔤 칭호조각", "need": 100, "item": "칭호권"},
}


def weighted_pick(weighted_items):
    total = sum(weight for weight, _ in weighted_items)
    point = random.uniform(0, total)
    upto = 0

    for weight, item in weighted_items:
        upto += weight
        if point <= upto:
            return item

    return weighted_items[-1][1]


def get_gacha_type(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT gacha_type
    FROM gacha_settings
    WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return "random"

    return row["gacha_type"]


def set_gacha_type(user_id, user_name, gacha_type):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO gacha_settings (user_id, user_name, gacha_type, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        user_name = excluded.user_name,
        gacha_type = excluded.gacha_type,
        updated_at = excluded.updated_at
    """, (user_id, user_name, gacha_type, now_str()))
    conn.commit()
    conn.close()


def add_reward_purchase(user_id, user_name, item_name):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO purchases (
        user_id, user_name, item_name, price, status, created_at
    )
    VALUES (?, ?, ?, 0, 'owned', ?)
    """, (user_id, user_name, item_name, now_str()))
    purchase_id = cur.lastrowid
    conn.commit()
    conn.close()
    return purchase_id


def add_gacha_piece(user_id, user_name, piece_key, amount):
    info = PIECE_INFO[piece_key]

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO gacha_pieces (user_id, piece_key, count, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id, piece_key)
    DO UPDATE SET
        count = count + excluded.count,
        updated_at = excluded.updated_at
    """, (user_id, piece_key, amount, now_str()))

    cur.execute("""
    SELECT count
    FROM gacha_pieces
    WHERE user_id = ?
      AND piece_key = ?
    """, (user_id, piece_key))
    total_count = cur.fetchone()["count"]

    completed = []
    need = info["need"]

    while total_count >= need:
        total_count -= need
        completed.append(info["item"])

    cur.execute("""
    UPDATE gacha_pieces
    SET count = ?,
        updated_at = ?
    WHERE user_id = ?
      AND piece_key = ?
    """, (total_count, now_str(), user_id, piece_key))

    conn.commit()
    conn.close()

    purchase_ids = []
    blacksmith_paid = False
    for item in completed:
        purchase_ids.append(add_reward_purchase(user_id, user_name, item))
        if grant_blacksmith_if_first(user_id, user_name, piece_key):
            blacksmith_paid = True

    return {
        "piece_key": piece_key,
        "label": info["label"],
        "amount": amount,
        "current": total_count,
        "need": need,
        "completed": completed,
        "purchase_ids": purchase_ids,
        "blacksmith_paid": blacksmith_paid,
    }


def get_all_gacha_pieces(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT piece_key, count
    FROM gacha_pieces
    WHERE user_id = ?
    ORDER BY piece_key ASC
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows



def add_gacha_pity_point(user_id, user_name):
    """
    코인형 가챠 F등급 보정:
    F등급 1회 = 행운포인트 1
    10포인트 달성 시 1코인 자동 지급 후 10포인트 차감.
    """
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO gacha_pity (user_id, user_name, pity_points, updated_at)
    VALUES (?, ?, 1, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        user_name = excluded.user_name,
        pity_points = pity_points + 1,
        updated_at = excluded.updated_at
    """, (user_id, user_name, now_str()))

    cur.execute("""
    SELECT pity_points
    FROM gacha_pity
    WHERE user_id = ?
    """, (user_id,))
    pity_points = cur.fetchone()["pity_points"]

    bonus_paid = 0

    if pity_points >= 10:
        bonus_paid = pity_points // 10
        pity_points = pity_points % 10

        cur.execute("""
        UPDATE gacha_pity
        SET pity_points = ?,
            updated_at = ?
        WHERE user_id = ?
        """, (pity_points, now_str(), user_id))

    conn.commit()
    conn.close()

    if bonus_paid > 0:
        change_money(
            user_id,
            user_name,
            bonus_paid * 10,
            f"코인형 가챠 행운포인트 {bonus_paid * 10}점 보상",
            None,
            "가챠시스템"
        )

    return pity_points, bonus_paid


def get_gacha_pity_point(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT pity_points
    FROM gacha_pity
    WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["pity_points"] if row else 0


def gacha_grade(gacha_type, tier):
    if gacha_type == "coin":
        # 코인형: 손해 40%, 본전 30%, 이득 30%
        if tier == "하":
            return weighted_pick([
                (40, "F"), (30, "E"), (18, "D"), (9, "C"), (3, "B")
            ])
        if tier == "중":
            return weighted_pick([
                (40, "F"), (30, "E"), (18, "D"), (9, "C"), (2.5, "B"), (0.5, "A")
            ])
        return weighted_pick([
            (40, "F"), (30, "E"), (18, "D"), (8, "C"), (3, "B"), (0.8, "A"), (0.2, "S")
        ])

    # 조각형 / 랜덤형: 손해 구간 약 40%
    if tier == "하":
        return weighted_pick([
            (40, "F"), (30, "E"), (20, "D"), (8, "C"), (2, "B")
        ])
    if tier == "중":
        return weighted_pick([
            (40, "F"), (25, "E"), (20, "D"), (10, "C"), (4, "B"), (1, "A")
        ])
    return weighted_pick([
        (40, "F"), (20, "E"), (20, "D"), (12, "C"), (6, "B"), (1.8, "A"), (0.2, "S")
    ])


def random_piece_by_group(group):
    low = ["선갠라", "단벙"]
    mid = ["봇등록", "미션"]
    high = ["임티", "칭호"]

    if group == "low":
        return random.choice(low)
    if group == "mid":
        return random.choice(mid)
    if group == "high":
        return random.choice(high)
    return random.choice(low + mid + high)


def coin_prize_for(tier, grade):
    prize_table = {
        "하": {
            "F": [3, 5, 7],      # 0.3~0.7코인
            "E": [10],           # 본전 1코인
            "D": [15],           # 1.5코인
            "C": [20],           # 2코인
            "B": [30],           # 3코인
        },
        "중": {
            "F": [10, 15, 20],   # 1~2코인
            "E": [30],           # 본전 3코인
            "D": [40, 50],       # 4~5코인
            "C": [60, 80],       # 6~8코인
            "B": [100],          # 10코인
            "A": [150],          # 15코인
        },
        "상": {
            "F": [20, 30, 40],   # 2~4코인
            "E": [50],           # 본전 5코인
            "D": [70, 100],      # 7~10코인
            "C": [150],          # 15코인
            "B": [200],          # 20코인
            "A": [300],          # 30코인
            "S": [500],          # 50코인
        },
    }

    return random.choice(prize_table[tier][grade])


def piece_prize_for(tier, grade):
    if tier == "하":
        table = {
            "F": None,
            "E": ("low", 1),
            "D": ("low", 2),
            "C": ("mid", 1),
            "B": ("high", 1),
        }
    elif tier == "중":
        table = {
            "F": ("low", 1),
            "E": ("low", 3),
            "D": ("mid", 2),
            "C": ("high", 2),
            "B": ("high", 5),
            "A": ("all", 10),
        }
    else:
        table = {
            "F": ("mid", 2),
            "E": ("mid", 5),
            "D": ("high", 5),
            "C": ("high", 10),
            "B": ("all", 15),
            "A": ("all", 25),
            "S": ("all", 50),
        }

    value = table[tier if False else grade] if False else table[grade]
    if value is None:
        return None

    group, amount = value
    piece_key = random_piece_by_group(group)
    return piece_key, amount


def random_prize_kind(tier, grade):
    # 랜덤형은 코인/조각 혼합.
    # F는 손해 구간이라 코인 소액 또는 꽝 위주.
    if grade == "F":
        return weighted_pick([(70, "coin"), (30, "piece")])
    return weighted_pick([(50, "coin"), (50, "piece")])


def run_gacha(user_id, user_name, tier):
    if tier not in GACHA_COSTS:
        return False, "사용법\n\n/가챠 하\n/가챠 중\n/가챠 상"

    gacha_type = get_gacha_type(user_id)
    cost = GACHA_COSTS[tier]
    balance = get_balance(user_id)

    if balance < cost:
        return False, (
            f"코인이 부족합니다.\n\n"
            f"필요: {coin_text(cost)}\n"
            f"보유: {coin_text(balance)}"
        )

    change_money(user_id, user_name, -cost, f"{tier} 가챠 이용", None, "가챠시스템")

    grade = gacha_grade(gacha_type, tier)
    lines = [
        f"🎰 {tier}급 가챠 결과",
        "",
        f"타입: {GACHA_TYPE_LABELS[gacha_type]}",
        f"등급: {grade}",
        "",
    ]

    if gacha_type == "coin":
        prize = coin_prize_for(tier, grade)

        if prize > 0:
            change_money(user_id, user_name, prize, f"{tier} 가챠 {grade}등급 코인 보상", None, "가챠시스템")
            lines.append(f"획득: 💰{coin_text(prize)}")
        else:
            lines.append("획득: 꽝")
            lines.append("다음 기회에...")

        if grade == "F":
            pity_points, bonus_paid = add_gacha_pity_point(user_id, user_name)
            lines.append("")
            lines.append("🎁 행운포인트 +1")
            lines.append(f"현재 행운포인트: {pity_points} / 10")

            if bonus_paid > 0:
                lines.append("")
                lines.append(f"🎉 행운포인트 보상 +{coin_text(bonus_paid * 10)}")

    elif gacha_type == "piece":
        prize = piece_prize_for(tier, grade)
        if prize is None:
            lines.append("획득: 꽝")
            lines.append("다음 기회에...")
        else:
            piece_key, amount = prize
            result = add_gacha_piece(user_id, user_name, piece_key, amount)
            lines.append(f"획득: {result['label']} x{amount}")
            lines.append(f"진행도: {result['current']} / {result['need']}")

            if result["completed"]:
                lines.append("")
                lines.append("🎉 조각 완성!")
                for item, purchase_id in zip(result["completed"], result["purchase_ids"]):
                    lines.append(f"{item} 획득 / 구매번호 #{purchase_id}")
                if result.get("blacksmith_paid"):
                    lines.append("🔨 대장장이 최초 완성 보상 +2코인")

    else:
        kind = random_prize_kind(tier, grade)

        if kind == "coin":
            prize = coin_prize_for(tier, grade)
            if prize > 0:
                change_money(user_id, user_name, prize, f"{tier} 가챠 {grade}등급 코인 보상", None, "가챠시스템")
                lines.append(f"획득: 💰{coin_text(prize)}")
            else:
                lines.append("획득: 꽝")
                lines.append("다음 기회에...")
        else:
            prize = piece_prize_for(tier, grade)
            if prize is None:
                lines.append("획득: 꽝")
                lines.append("다음 기회에...")
            else:
                piece_key, amount = prize
                result = add_gacha_piece(user_id, user_name, piece_key, amount)
                lines.append(f"획득: {result['label']} x{amount}")
                lines.append(f"진행도: {result['current']} / {result['need']}")

                if result["completed"]:
                    lines.append("")
                    lines.append("🎉 조각 완성!")
                    for item, purchase_id in zip(result["completed"], result["purchase_ids"]):
                        lines.append(f"{item} 획득 / 구매번호 #{purchase_id}")
                    if result.get("blacksmith_paid"):
                        lines.append("🔨 대장장이 최초 완성 보상 +2코인")

    lines.append("")
    lines.append(f"현재 잔액: {coin_text(get_balance(user_id))}")

    return True, "\n".join(lines)


def gacha_system_text():
    return (
        "🎰 가챠 시스템 🎰\n\n"
        "━━━━━━━━━━\n"
        "🎲 가챠 종류\n"
        "━━━━━━━━━━\n\n"
        "🟢 하급 가챠\n"
        "비용 : 💰1코인\n\n"
        "🟡 중급 가챠\n"
        "비용 : 💰3코인\n\n"
        "🔴 상급 가챠\n"
        "비용 : 💰5코인\n\n"
        "사용법\n"
        "/가챠 하\n"
        "/가챠 중\n"
        "/가챠 상\n\n"
        "━━━━━━━━━━\n"
        "⚙️ 가챠 타입\n"
        "━━━━━━━━━━\n\n"
        "💰 코인형\n"
        "→ 코인만 획득\n\n"
        "🧩 조각형\n"
        "→ 조각만 획득\n\n"
        "🎲 랜덤형\n"
        "→ 코인 + 조각 랜덤 획득\n\n"
        "변경 방법\n"
        "/가챠타입 코인\n"
        "/가챠타입 조각\n"
        "/가챠타입 랜덤\n\n"
        "━━━━━━━━━━\n"
        "🧩 조각 합성\n"
        "━━━━━━━━━━\n\n"
        "💠 선갠라권 : 조각 10개\n"
        "💠 단벙주최권 : 조각 12개\n"
        "📸 봇등록권 : 조각 20개\n"
        "🛟 미션클리어권 : 조각 40개\n"
        "🎟 임티권 : 조각 100개\n"
        "🔤 칭호권 : 조각 100개\n\n"
        "※ 조각은 자동 합성됩니다.\n\n"
        "━━━━━━━━━━\n"
        "📊 확률\n"
        "━━━━━━━━━━\n\n"
        "💰 코인형 : 손해 40% / 본전 30% / 이득 30%\n"
        "🧩 조각형 : 손해 확률 약 40%\n"
        "🎲 랜덤형 : 손해 확률 약 40%\n\n"
        "━━━━━━━━━━\n"
        "📦 보유 확인\n"
        "━━━━━━━━━━\n\n"
        "/조각보유\n"
        "/내보유\n\n"
        "행운도 실력이다 🍀"
    )


# =========================
# 히든 미션
# =========================

def broadcast_hidden_reward(reason, user_name, reward):
    try:
        from linebot.v3.messaging import PushMessageRequest, TextMessage

        msg = (
            "🎉 히든 미션 달성!\n\n"
            f"{reason}\n"
            f"달성자: {user_name}\n"
            f"보상: 💰{coin_text(reward)}"
        )

        messaging_api.push_message(
            PushMessageRequest(
                to=COUNT_SOURCE_ID,
                messages=[TextMessage(text=msg)]
            )
        )
    except Exception as e:
        print("HIDDEN_BROADCAST_ERROR:", e)


def grant_hidden_reward_once(date_str, mission_key, user_id, user_name, reward, reason, meta=""):
    """
    같은 날짜 + 같은 미션키는 1번만 지급.
    선착순/행운번호 보상에 사용.
    """
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT OR IGNORE INTO hidden_rewards (
        date, mission_key, user_id, user_name, reward, meta, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        date_str,
        mission_key,
        user_id,
        user_name,
        reward,
        meta,
        now_str()
    ))

    inserted = cur.rowcount
    conn.commit()
    conn.close()

    if inserted:
        change_money(
            user_id,
            user_name,
            reward,
            reason,
            None,
            "히든이벤트"
        )

        broadcast_hidden_reward(reason, user_name, reward)
        return True

    return False


def check_hidden_1000_reward(date_str, source_id, user_id, user_name):
    """
    하루 1000마디 최초 달성자 1명에게 1코인 자동 지급.
    """
    if source_id != COUNT_SOURCE_ID:
        return False

    count = get_user_count(date_str, source_id, user_id)

    if count < 1000:
        return False

    return grant_hidden_reward_once(
        date_str,
        "first_1000",
        user_id,
        user_name,
        10,
        "숨겨진 이벤트 보상: 당일 첫 1000마디 달성",
        f"count={count}"
    )


def check_hidden_2000_reward(date_str, source_id, user_id, user_name):
    """
    사이버망령:
    하루 2000마디 최초 달성자 1명에게 3코인 자동 지급.
    """
    if source_id != COUNT_SOURCE_ID:
        return False

    count = get_user_count(date_str, source_id, user_id)

    if count < 2000:
        return False

    return grant_hidden_reward_once(
        date_str,
        "cyber_ghost_2000",
        user_id,
        user_name,
        30,
        "숨겨진 이벤트 보상: 사이버망령 당일 첫 2000마디 달성",
        f"count={count}"
    )


def attendance_streak_days(user_id, date_str):
    """
    date_str 기준으로 오늘 포함 연속 출석일 계산.
    """
    base = datetime.strptime(date_str, "%Y-%m-%d").date()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT date
    FROM attendance
    WHERE user_id = ?
    """, (user_id,))
    dates = {row["date"] for row in cur.fetchall()}
    conn.close()

    streak = 0
    day = base

    while day.strftime("%Y-%m-%d") in dates:
        streak += 1
        day -= timedelta(days=1)

    return streak


def check_attendance_streak_reward(date_str, user_id, user_name):
    """
    연속출석:
    7일 1코인, 14일 2코인, 28일 5코인.
    각 구간별 1회 지급.
    """
    streak = attendance_streak_days(user_id, date_str)

    rewards = [
        (7, 10),
        (14, 20),
        (28, 50),
    ]

    paid = []

    for required_days, reward in rewards:
        if streak >= required_days:
            mission_key = f"attendance_streak_{required_days}_{user_id}"
            ok = grant_hidden_reward_once(
                date_str,
                mission_key,
                user_id,
                user_name,
                reward,
                f"연속출석 보상: {required_days}일 연속 출석",
                f"streak={streak}"
            )
            if ok:
                paid.append((required_days, reward))

    return streak, paid


def get_today_chat_log_sequence(source_id, date_str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT COUNT(*) AS total_logs
    FROM chat_logs
    WHERE source_id = ?
      AND date = ?
      AND user_id IS NOT NULL
      AND user_id != ''
      AND user_id != 'NO_USER_ID'
    """, (source_id, date_str))
    row = cur.fetchone()
    conn.close()
    return row["total_logs"] if row else 0


def check_lucky_log_rewards(date_str, source_id, user_id, user_name):
    """
    행운의 숫자:
    메인방 일일 chat_logs 기준 777번째 = 0.5코인,
    7777번째 = 1코인.
    해당 순번 메시지를 친 사람에게 자동 지급.
    """
    if source_id != COUNT_SOURCE_ID:
        return []

    seq = get_today_chat_log_sequence(source_id, date_str)
    paid = []

    if seq == 777:
        ok = grant_hidden_reward_once(
            date_str,
            "log_777",
            user_id,
            user_name,
            5,
            "행운의 숫자 보상: 오늘 777번째 대화",
            f"seq={seq}"
        )
        if ok:
            paid.append(("777번째", 5))

    if seq == 7777:
        ok = grant_hidden_reward_once(
            date_str,
            "log_7777",
            user_id,
            user_name,
            10,
            "행운의 숫자 보상: 오늘 7777번째 대화",
            f"seq={seq}"
        )
        if ok:
            paid.append(("7777번째", 10))

    return paid


def get_daily_lucky_number(date_str):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT lucky_number
    FROM daily_lucky_numbers
    WHERE date = ?
    """, (date_str,))
    row = cur.fetchone()

    if row:
        conn.close()
        return row["lucky_number"]

    lucky_number = random.randint(1, 10000)

    cur.execute("""
    INSERT INTO daily_lucky_numbers (date, lucky_number, created_at)
    VALUES (?, ?, ?)
    """, (date_str, lucky_number, now_str()))

    conn.commit()
    conn.close()

    return lucky_number


def check_lucky_guy_reward(date_str, source_id, user_id, user_name):
    """
    럭키가이:
    매일 1~10000 사이 숫자 자동 지정.
    메인방 일일 chat_logs 순번이 그 숫자와 일치하면 1코인 지급.
    """
    if source_id != COUNT_SOURCE_ID:
        return False

    lucky_number = get_daily_lucky_number(date_str)
    seq = get_today_chat_log_sequence(source_id, date_str)

    if seq != lucky_number:
        return False

    return grant_hidden_reward_once(
        date_str,
        "lucky_guy",
        user_id,
        user_name,
        10,
        f"럭키가이 보상: 오늘 {lucky_number}번째 대화",
        f"lucky_number={lucky_number};seq={seq}"
    )


def hidden_reward_status(date_str):
    lucky_number = get_daily_lucky_number(date_str)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT mission_key, user_name, reward, meta, created_at
    FROM hidden_rewards
    WHERE date = ?
    ORDER BY created_at ASC
    """, (date_str,))
    rows = cur.fetchall()

    conn.close()

    return lucky_number, rows


def week_range_for_today():
    now = datetime.now(KST).date()
    start = now - timedelta(days=now.weekday())
    end = start + timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def weekly_ranking_rows(source_id, week_start, week_end, limit=10):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        u.user_id,
        u.user_name,
        COALESCE(SUM(c.count), 0) AS total_count
    FROM counts c
    JOIN users u
      ON u.user_id = c.user_id
    WHERE c.source_id = ?
      AND c.date BETWEEN ? AND ?
      AND COALESCE(u.is_active, 1) = 1
    GROUP BY c.user_id
    HAVING total_count > 0
    ORDER BY total_count DESC, u.user_name ASC
    LIMIT ?
    """, (source_id, week_start, week_end, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def weekly_reward_amount(rank):
    if rank == 1:
        return 100  # 10코인
    if rank == 2:
        return 70   # 7코인
    if rank == 3:
        return 50   # 5코인
    if 4 <= rank <= 10:
        return 20   # 2코인
    return 0


def settle_weekly_rewards(source_id, week_start, week_end):
    rows = weekly_ranking_rows(source_id, week_start, week_end, limit=10)
    paid = []

    conn = db()
    cur = conn.cursor()

    for idx, row in enumerate(rows, 1):
        reward = weekly_reward_amount(idx)
        if reward <= 0:
            continue

        cur.execute("""
        INSERT OR IGNORE INTO weekly_rewards (
            week_start, week_end, user_id, user_name,
            rank, count, reward, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            week_start,
            week_end,
            row["user_id"],
            row["user_name"],
            idx,
            row["total_count"],
            reward,
            now_str()
        ))

        if cur.rowcount > 0:
            paid.append({
                "rank": idx,
                "user_id": row["user_id"],
                "user_name": row["user_name"],
                "count": row["total_count"],
                "reward": reward,
            })

    conn.commit()
    conn.close()

    for item in paid:
        change_money(
            item["user_id"],
            item["user_name"],
            item["reward"],
            f"주간 마디수 랭킹 보상 {week_start}~{week_end} {item['rank']}위",
            None,
            "주간정산"
        )

    return paid


# =========================
# S.N.S 럭키드로우 / 핀볼
# =========================
EVENT_TICKET_PRICE = 10          # 1코인
EVENT_BASE_PRIZE = 50            # 기본 부스팅 5코인
EVENT_PAYOUT_RATE = 0.8          # 판매액 80% 지급
PINBALL_MAX_TICKETS = 3


def event_week_key():
    return week_range_for_today()


def is_saturday_draw_time():
    now = datetime.now(KST)
    return now.weekday() == 5 and now.hour >= 21


def buy_lucky_draw_ticket(user_id, user_name):
    week_start, week_end = event_week_key()

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sns_lucky_draw_results WHERE week_start = ?", (week_start,))
    if cur.fetchone():
        conn.close()
        return False, "이번 주 S.N.S 럭키드로우는 이미 추첨 완료되었습니다."

    cur.execute("SELECT tickets FROM sns_lucky_draw_entries WHERE week_start = ? AND user_id = ?", (week_start, user_id))
    if cur.fetchone():
        conn.close()
        return False, "이미 이번 주 S.N.S 럭키드로우에 참여했습니다.\n구매 제한: 1인 1장"

    conn.close()

    balance = get_balance(user_id)
    if balance < EVENT_TICKET_PRICE:
        return False, f"코인이 부족합니다.\n\n필요: {coin_text(EVENT_TICKET_PRICE)}\n보유: {coin_text(balance)}"

    change_money(user_id, user_name, -EVENT_TICKET_PRICE, "S.N.S 럭키드로우 티켓 구매", None, "S.N.S이벤트")

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO sns_lucky_draw_entries (week_start, week_end, user_id, user_name, tickets, created_at)
    VALUES (?, ?, ?, ?, 1, ?)
    """, (week_start, week_end, user_id, user_name, now_str()))
    conn.commit()
    conn.close()

    return True, lucky_draw_status_text(week_start, week_end, title="🎟️ S.N.S 럭키드로우 참여 완료")


def lucky_draw_rows(week_start, week_end):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_id, user_name, tickets, created_at
    FROM sns_lucky_draw_entries
    WHERE week_start = ? AND week_end = ?
    ORDER BY created_at ASC
    """, (week_start, week_end))
    rows = cur.fetchall()
    conn.close()
    return rows


def lucky_draw_status_text(week_start=None, week_end=None, title="🎟️ S.N.S 럭키드로우 현황"):
    if not week_start or not week_end:
        week_start, week_end = event_week_key()
    rows = lucky_draw_rows(week_start, week_end)
    total_sales = len(rows) * EVENT_TICKET_PRICE
    prize = EVENT_BASE_PRIZE + int(total_sales * EVENT_PAYOUT_RATE)

    lines = [
        title,
        f"기간: {week_start} ~ {week_end}",
        "",
        f"참여자: {len(rows)}명",
        f"기본 부스팅: {coin_text(EVENT_BASE_PRIZE)}",
        f"현재 예상 당첨금: {coin_text(prize)}",
        "추첨: 매주 토요일 21:00",
        "",
        "구매: /SNS럭키구매",
    ]

    if rows:
        lines.append("")
        lines.append("참여자 목록")
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']}")

    return format_long_lines("", lines).strip()


def settle_lucky_draw(settled_by="자동추첨"):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM sns_lucky_draw_results WHERE week_start = ?", (week_start,))
    if cur.fetchone():
        conn.close()
        return False, "이번 주 S.N.S 럭키드로우는 이미 추첨 완료되었습니다."

    cur.execute("""
    SELECT e.user_id, e.user_name, e.tickets
    FROM sns_lucky_draw_entries e
    JOIN users u ON u.user_id = e.user_id
    WHERE e.week_start = ? AND e.week_end = ? AND COALESCE(u.is_active, 1) = 1
    ORDER BY e.created_at ASC
    """, (week_start, week_end))
    rows = cur.fetchall()

    if not rows:
        conn.close()
        return False, "이번 주 S.N.S 럭키드로우 참여자가 없습니다."

    winner = random.choice(rows)
    total_sales = len(rows) * EVENT_TICKET_PRICE
    prize = EVENT_BASE_PRIZE + int(total_sales * EVENT_PAYOUT_RATE)
    burned = total_sales - int(total_sales * EVENT_PAYOUT_RATE)

    cur.execute("""
    INSERT INTO sns_lucky_draw_results (
        week_start, week_end, winner_user_id, winner_user_name,
        participants, total_sales, prize, burned, settled_by, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (week_start, week_end, winner["user_id"], winner["user_name"], len(rows), total_sales, prize, burned, settled_by, now_str()))
    conn.commit()
    conn.close()

    change_money(winner["user_id"], winner["user_name"], prize, f"S.N.S 럭키드로우 당첨 {week_start}~{week_end}", None, settled_by)

    return True, (
        "🎉 S.N.S 럭키드로우 추첨 결과\n\n"
        f"기간: {week_start} ~ {week_end}\n"
        f"참여자: {len(rows)}명\n"
        f"총 판매액: {coin_text(total_sales)}\n"
        f"기본 부스팅: {coin_text(EVENT_BASE_PRIZE)}\n"
        f"소각: {coin_text(burned)}\n\n"
        f"🏆 당첨자: {winner['user_name']}\n"
        f"지급: {coin_text(prize)}"
    )


def buy_pinball_ticket(user_id, user_name, amount=1):
    amount = max(1, min(int(amount), PINBALL_MAX_TICKETS))
    week_start, week_end = event_week_key()

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(tickets, 0) AS tickets FROM sns_pinball_entries WHERE week_start = ? AND user_id = ?", (week_start, user_id))
    row = cur.fetchone()
    current = row["tickets"] if row else 0
    can_buy = PINBALL_MAX_TICKETS - current
    if can_buy <= 0:
        conn.close()
        return False, "이번 주 S.N.S 핀볼 참여권은 이미 최대치입니다.\n구매 제한: 1인 최대 3장"
    buy_count = min(amount, can_buy)
    conn.close()

    cost = buy_count * EVENT_TICKET_PRICE
    balance = get_balance(user_id)
    if balance < cost:
        return False, f"코인이 부족합니다.\n\n필요: {coin_text(cost)}\n보유: {coin_text(balance)}"

    change_money(user_id, user_name, -cost, f"S.N.S 핀볼 참여권 {buy_count}장 구매", None, "S.N.S이벤트")

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO sns_pinball_entries (week_start, week_end, user_id, user_name, tickets, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(week_start, user_id)
    DO UPDATE SET
        user_name = excluded.user_name,
        tickets = tickets + excluded.tickets,
        updated_at = excluded.updated_at
    """, (week_start, week_end, user_id, user_name, buy_count, now_str(), now_str()))
    conn.commit()
    conn.close()

    return True, pinball_status_text(week_start, week_end, title=f"🎱 S.N.S 핀볼 참여 완료 / 구매 {buy_count}장")


def pinball_rows(week_start, week_end):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_id, user_name, tickets, updated_at
    FROM sns_pinball_entries
    WHERE week_start = ? AND week_end = ?
    ORDER BY tickets DESC, updated_at ASC
    """, (week_start, week_end))
    rows = cur.fetchall()
    conn.close()
    return rows


def pinball_status_text(week_start=None, week_end=None, title="🎱 S.N.S 핀볼 현황"):
    if not week_start or not week_end:
        week_start, week_end = event_week_key()
    rows = pinball_rows(week_start, week_end)
    total_tickets = sum(row["tickets"] for row in rows)
    total_sales = total_tickets * EVENT_TICKET_PRICE
    prize = EVENT_BASE_PRIZE + int(total_sales * EVENT_PAYOUT_RATE)

    lines = [
        title,
        f"기간: {week_start} ~ {week_end}",
        "",
        f"참여자: {len(rows)}명",
        f"총 참여권: {total_tickets}장",
        f"기본 부스팅: {coin_text(EVENT_BASE_PRIZE)}",
        f"현재 지급풀: {coin_text(prize)}",
        "구매 제한: 1인 최대 3장",
        "",
        "구매: /SNS핀볼구매 또는 /SNS핀볼구매 3",
    ]

    if rows:
        lines.append("")
        lines.append("참여자 목록")
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']} - {row['tickets']}장")

    return format_long_lines("", lines).strip()


def settle_pinball_by_winners(winner_keywords, settled_by):
    week_start, week_end = event_week_key()
    clean_keywords = [x.strip() for x in winner_keywords if x.strip()]
    if not clean_keywords:
        return False, "사용법\n\n/SNS핀볼정산 닉네임1 닉네임2"

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sns_pinball_results WHERE week_start = ? LIMIT 1", (week_start,))
    if cur.fetchone():
        conn.close()
        return False, "이번 주 S.N.S 핀볼은 이미 정산 완료되었습니다."
    conn.close()

    rows = pinball_rows(week_start, week_end)
    total_tickets = sum(row["tickets"] for row in rows)
    if total_tickets <= 0:
        return False, "이번 주 S.N.S 핀볼 참여자가 없습니다."

    participant_ids = {row["user_id"] for row in rows}
    winners = []
    seen = set()
    for keyword in clean_keywords:
        target = find_user(keyword)
        if not target:
            return False, f"당첨자를 찾을 수 없습니다.\n검색어: {keyword}"
        if target["user_id"] not in participant_ids:
            return False, f"S.N.S 핀볼 참여자가 아닙니다.\n대상: {target['user_name']}"
        if target["user_id"] not in seen:
            winners.append(target)
            seen.add(target["user_id"])

    total_sales = total_tickets * EVENT_TICKET_PRICE
    total_prize = EVENT_BASE_PRIZE + int(total_sales * EVENT_PAYOUT_RATE)
    prize_each = total_prize // len(winners)
    burned = total_sales - int(total_sales * EVENT_PAYOUT_RATE)

    conn = db()
    cur = conn.cursor()
    for winner in winners:
        cur.execute("""
        INSERT INTO sns_pinball_results (
            week_start, week_end, winner_user_id, winner_user_name,
            winner_count, total_sales, total_prize, prize_each, burned, settled_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (week_start, week_end, winner["user_id"], winner["user_name"], len(winners), total_sales, total_prize, prize_each, burned, settled_by, now_str()))
    conn.commit()
    conn.close()

    for winner in winners:
        change_money(winner["user_id"], winner["user_name"], prize_each, f"S.N.S 핀볼 당첨 {week_start}~{week_end}", None, settled_by)

    lines = [
        "🎱 S.N.S 핀볼 정산 완료",
        f"기간: {week_start} ~ {week_end}",
        "",
        f"총 참여권: {total_tickets}장",
        f"총 판매액: {coin_text(total_sales)}",
        f"기본 부스팅: {coin_text(EVENT_BASE_PRIZE)}",
        f"총 지급풀: {coin_text(total_prize)}",
        f"소각: {coin_text(burned)}",
        f"당첨 인원: {len(winners)}명",
        f"1인 지급: {coin_text(prize_each)}",
        "",
        "당첨자",
    ]
    for i, winner in enumerate(winners, 1):
        lines.append(f"{i}. {winner['user_name']}")
    return True, "\n".join(lines)


def maybe_auto_lucky_draw():
    if not is_saturday_draw_time():
        return
    ok, msg = settle_lucky_draw("자동추첨")
    if not ok:
        return
    try:
        from linebot.v3.messaging import PushMessageRequest, TextMessage
        with ApiClient(config) as client:
            api = MessagingApi(client)
            api.push_message(PushMessageRequest(to=COUNT_SOURCE_ID, messages=[TextMessage(text=msg)]))
    except Exception as e:
        print("SNS_LUCKY_AUTO_PUSH_ERROR:", e)


# =========================
# 업적 / 현상금
# =========================
BOUNTY_REQUIRED_COUNT = 5
BOUNTY_REWARD = 10  # 1코인

ACHIEVEMENT_CATALOG = [
    ("first_attendance", "✅ 첫 출석", "출석을 처음 완료", 2),
    ("first_gacha", "🎰 첫 가챠", "가챠를 처음 이용", 2),
    ("first_lucky", "🎟️ 첫 럭키드로우", "S.N.S 럭키드로우 첫 참여", 2),
    ("first_pinball", "🎱 첫 핀볼", "S.N.S 핀볼 첫 참여", 2),
    ("bounty_complete", "🎯 첫 현상금", "현상금을 처음 완료", 5),
    ("first_manitto", "🎭 첫 마니또", "마니또를 처음 성공", 5),
]


def grant_achievement_once(user_id, user_name, achievement_key, achievement_name, reward=0, meta=""):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO achievements (
        user_id, user_name, achievement_key, achievement_name, reward, meta, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, user_name, achievement_key, achievement_name, reward, meta, now_str()))
    inserted = cur.rowcount
    conn.commit()
    conn.close()

    if inserted and reward > 0:
        change_money(user_id, user_name, reward, f"업적 보상: {achievement_name}", None, "업적시스템")

    return bool(inserted)


def get_user_achievements(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT achievement_key, achievement_name, reward, meta, created_at
    FROM achievements
    WHERE user_id = ?
    ORDER BY created_at ASC
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def achievement_status_text(user_id, user_name):
    rows = get_user_achievements(user_id)
    owned = {row["achievement_key"] for row in rows}

    dynamic = []
    for key, info in PIECE_INFO.items():
        dynamic.append((f"blacksmith_{key}", f"🔨 대장장이: {info['item']}", f"{info['label']} 최초 완성", 20))

    catalog = ACHIEVEMENT_CATALOG + dynamic
    lines = [
        "🎖 업적 현황",
        f"대상: {user_name}",
        "",
        f"완료: {len(rows)}개",
        "",
    ]
    for key, name, desc, reward in catalog:
        mark = "✅" if key in owned else "⬜"
        lines.append(f"{mark} {name}")
        lines.append(f"   {desc} / 보상 {coin_text(reward)}")
    return "\n".join(lines)


def grant_blacksmith_if_first(user_id, user_name, piece_key):
    info = PIECE_INFO.get(piece_key)
    if not info:
        return False
    return grant_achievement_once(
        user_id,
        user_name,
        f"blacksmith_{piece_key}",
        f"🔨 대장장이: {info['item']}",
        20,
        f"piece_key={piece_key}"
    )


def active_bounty_targets(exclude_user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_id, user_name
    FROM users
    WHERE COALESCE(is_active, 1) = 1
      AND user_id IS NOT NULL
      AND user_id != ''
      AND user_id != ?
    ORDER BY RANDOM()
    LIMIT 1
    """, (exclude_user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def ensure_weekly_bounty(user_id, user_name):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM weekly_bounties
    WHERE week_start = ? AND hunter_user_id = ?
    """, (week_start, user_id))
    row = cur.fetchone()
    if row:
        conn.close()
        return row, None
    conn.close()

    target = active_bounty_targets(user_id)
    if not target:
        return None, "현상금 타깃을 지정할 활성 유저가 부족합니다."

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO weekly_bounties (
        week_start, week_end, hunter_user_id, hunter_user_name,
        target_user_id, target_user_name, mention_count, required_count,
        reward, completed, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)
    """, (
        week_start, week_end, user_id, user_name,
        target["user_id"], target["user_name"],
        BOUNTY_REQUIRED_COUNT, BOUNTY_REWARD, now_str(), now_str()
    ))
    conn.commit()
    cur.execute("""
    SELECT * FROM weekly_bounties
    WHERE week_start = ? AND hunter_user_id = ?
    """, (week_start, user_id))
    row = cur.fetchone()
    conn.close()
    return row, None


def bounty_status_text(user_id, user_name):
    row, err = ensure_weekly_bounty(user_id, user_name)
    if err:
        return err

    status = "완료" if row["completed"] else "진행중"
    return (
        "🎯 S.N.S 현상금\n\n"
        f"이번 주 타깃: {row['target_user_name']}\n"
        f"조건: 메인방에서 타깃 닉네임 언급 {row['required_count']}회\n"
        f"진행도: {row['mention_count']} / {row['required_count']}\n"
        f"보상: {coin_text(row['reward'])}\n"
        f"상태: {status}\n\n"
        "※ 같은 문장 반복은 카운트되지 않습니다.\n"
        "※ 명령어 메시지는 제외됩니다."
    )


def process_bounty_mention(source_id, user_id, user_name, text_value):
    if source_id != COUNT_SOURCE_ID or not user_id or not text_value:
        return None
    if text_value.startswith('/'):
        return None

    row, err = ensure_weekly_bounty(user_id, user_name)
    if err or not row or row["completed"]:
        return None

    target_key = clean_keyword(row["target_user_name"])
    text_key = clean_keyword(text_value)
    if not target_key or target_key not in text_key:
        return None
    if row["target_user_id"] == user_id:
        return None
    if row["last_text_key"] and row["last_text_key"] == text_key:
        return None

    new_count = row["mention_count"] + 1
    completed = 1 if new_count >= row["required_count"] else 0
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE weekly_bounties
    SET mention_count = ?,
        completed = ?,
        last_text_key = ?,
        updated_at = ?,
        completed_at = CASE WHEN ? = 1 THEN ? ELSE completed_at END
    WHERE week_start = ? AND hunter_user_id = ?
    """, (
        new_count, completed, text_key, now_str(), completed, now_str(),
        row["week_start"], user_id
    ))
    conn.commit()
    conn.close()

    if completed:
        change_money(user_id, user_name, row["reward"], f"현상금 완료: {row['target_user_name']} 언급", None, "현상금시스템")
        grant_achievement_once(user_id, user_name, "bounty_complete", "🎯 첫 현상금", 5, f"target={row['target_user_name']}")
        return (
            "🎯 현상금 달성!\n\n"
            f"타깃: {row['target_user_name']}\n"
            f"달성자: {user_name}\n"
            f"보상: {coin_text(row['reward'])}\n"
            f"현재 잔액: {coin_text(get_balance(user_id))}"
        )
    return None


def bounty_admin_status_text():
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT hunter_user_name, target_user_name, mention_count, required_count, reward, completed
    FROM weekly_bounties
    WHERE week_start = ?
    ORDER BY completed DESC, mention_count DESC, hunter_user_name ASC
    LIMIT 50
    """, (week_start,))
    rows = cur.fetchall()
    conn.close()
    lines = ["🎯 현상금 현황", f"기간: {week_start} ~ {week_end}", ""]
    if not rows:
        lines.append("아직 발급된 현상금이 없습니다.")
    else:
        for i, row in enumerate(rows, 1):
            mark = "✅" if row["completed"] else "진행"
            lines.append(
                f"{i}. {row['hunter_user_name']} → {row['target_user_name']} "
                f"{row['mention_count']}/{row['required_count']} / {coin_text(row['reward'])} / {mark}"
            )
    return format_long_lines("", lines).strip()

# =========================
# 초기화 / 삭제
# =========================
def reset_date(date_str, source_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM counts WHERE date = ? AND source_id = ?", (date_str, source_id))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def reset_all_counts():
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM counts")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def reset_all_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def reset_everything():
    conn = db()
    cur = conn.cursor()

    tables = ["counts", "users", "currency", "currency_logs", "purchases"]
    deleted = {}
    for table in tables:
        cur.execute(f"DELETE FROM {table}")
        deleted[table] = cur.rowcount

    conn.commit()
    conn.close()
    return deleted


def find_delete_candidates(keyword, limit=20):
    """
    닉네임 삭제 후보 검색.
    users에 없는 오래된 기록까지 포함해서 user_id 단위로 후보를 모읍니다.
    """
    keyword = keyword.strip()
    if not keyword:
        return []

    conn = db()
    cur = conn.cursor()

    candidate_sqls = [
        ("users", "user_id", "user_name"),
        ("counts", "user_id", "user_name"),
        ("currency_logs", "user_id", "user_name"),
        ("purchases", "user_id", "user_name"),
        ("attendance", "user_id", "user_name"),
        ("mission_claims", "user_id", "user_name"),
        ("hidden_rewards", "user_id", "user_name"),
        ("gacha_settings", "user_id", "user_name"),
        ("gacha_pity", "user_id", "user_name"),
        ("weekly_rewards", "user_id", "user_name"),
        ("sns_lucky_draw_entries", "user_id", "user_name"),
        ("sns_pinball_entries", "user_id", "user_name"),
        ("achievements", "user_id", "user_name"),
        ("weekly_bounties", "hunter_user_id", "hunter_user_name"),
        ("weekly_bounties", "target_user_id", "target_user_name"),
        ("chat_last_speakers", "user_id", "user_name"),
        ("affinity_scores", "user_a", "user_a_name"),
        ("affinity_scores", "user_b", "user_b_name"),
        ("manitto_assignments", "hunter_user_id", "hunter_user_name"),
        ("manitto_assignments", "target_user_id", "target_user_name"),
    ]

    targets = {}
    like = f"%{keyword}%"

    for table, id_col, name_col in candidate_sqls:
        try:
            cur.execute(f"""
            SELECT {id_col} AS user_id, {name_col} AS user_name
            FROM {table}
            WHERE {name_col} LIKE ?
              AND {id_col} IS NOT NULL
              AND {id_col} != ''
            ORDER BY {name_col} ASC
            LIMIT ?
            """, (like, limit))
            for row in cur.fetchall():
                uid = row["user_id"]
                name = row["user_name"]
                if uid not in targets:
                    targets[uid] = name
        except Exception as e:
            print("DELETE USER SEARCH SKIP:", table, e)

    conn.close()

    # 완전 일치 후보를 위로 올림
    candidates = [{"user_id": uid, "user_name": name} for uid, name in targets.items()]
    candidates.sort(key=lambda x: (0 if x["user_name"] == keyword else 1, x["user_name"]))
    return candidates[:limit]


def delete_users_by_ids(targets):
    """
    targets: {user_id: user_name}
    지정된 user_id의 주요 기록을 모두 삭제합니다.
    """
    if not targets:
        return 0, 0, [], {}

    conn = db()
    cur = conn.cursor()
    deleted = {}

    def add_deleted(name, count):
        deleted[name] = deleted.get(name, 0) + int(count or 0)

    simple_user_id_tables = [
        "users",
        "chat_logs",
        "counts",
        "currency",
        "currency_logs",
        "purchases",
        "attendance",
        "mission_claims",
        "hidden_rewards",
        "gacha_settings",
        "gacha_pity",
        "gacha_pieces",
        "weekly_rewards",
        "sns_lucky_draw_entries",
        "sns_pinball_entries",
        "achievements",
        "chat_last_speakers",
    ]

    for target_user_id in targets.keys():
        for table in simple_user_id_tables:
            try:
                cur.execute(f"DELETE FROM {table} WHERE user_id = ?", (target_user_id,))
                add_deleted(table, cur.rowcount)
            except Exception as e:
                print("DELETE USER TABLE SKIP:", table, e)

        relation_deletes = [
            ("sns_lucky_draw_results", "winner_user_id"),
            ("sns_pinball_results", "winner_user_id"),
            ("weekly_bounties", "hunter_user_id"),
            ("weekly_bounties", "target_user_id"),
            ("affinity_scores", "user_a"),
            ("affinity_scores", "user_b"),
            ("affinity_pair_cooldowns", "user_a"),
            ("affinity_pair_cooldowns", "user_b"),
            ("manitto_assignments", "hunter_user_id"),
            ("manitto_assignments", "target_user_id"),
        ]
        for table, col in relation_deletes:
            try:
                cur.execute(f"DELETE FROM {table} WHERE {col} = ?", (target_user_id,))
                add_deleted(table, cur.rowcount)
            except Exception as e:
                print("DELETE USER RELATION SKIP:", table, col, e)

    conn.commit()
    conn.close()

    deleted_users = deleted.get("users", 0)
    deleted_counts = deleted.get("counts", 0)
    deleted_names = list(dict.fromkeys(targets.values()))
    return deleted_users, deleted_counts, deleted_names, deleted


def delete_user_by_name(keyword):
    """기존 호환용: 검색어에 걸린 모든 후보를 삭제합니다."""
    candidates = find_delete_candidates(keyword)
    targets = {row["user_id"]: row["user_name"] for row in candidates}
    return delete_users_by_ids(targets)


def format_delete_done(keyword, deleted_users, deleted_counts, deleted_names, deleted_detail):
    names_text = "\n".join([f"- {name}" for name in deleted_names])
    return (
        f"❌ 닉네임 삭제 완료\n\n"
        f"검색어: {keyword}\n"
        f"삭제 유저DB: {deleted_users}명\n"
        f"삭제 마디수 데이터: {deleted_counts}개\n"
        f"삭제 전체 기록: {sum(deleted_detail.values())}개\n\n"
        f"삭제된 닉네임:\n{names_text}"
    )





# =========================
# 마니또 / 친밀도
# =========================
AFFINITY_REPLY_WINDOW_SECONDS = 180
AFFINITY_PAIR_COOLDOWN_SECONDS = 30
MANITTO_REQUIRED_SCORE = 15
MANITTO_REWARD_MIN = 10   # 1코인
MANITTO_REWARD_MAX = 50   # 5코인
MANITTO_TARGET_MIN_BALANCE = 50  # 5코인 이상 보유자
GOLDEN_MANITTO_RATE = 5  # 5%


def parse_time_kst(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None


def pair_key(user_id_1, user_id_2):
    return tuple(sorted([user_id_1, user_id_2]))


def manitto_target_candidates(exclude_user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT u.user_id, u.user_name, COALESCE(c.balance, 0) AS balance
    FROM users u
    JOIN currency c ON c.user_id = u.user_id
    WHERE COALESCE(u.is_active, 1) = 1
      AND u.user_id IS NOT NULL
      AND u.user_id != ''
      AND u.user_id != ?
      AND COALESCE(c.balance, 0) >= ?
    ORDER BY RANDOM()
    LIMIT 1
    """, (exclude_user_id, MANITTO_TARGET_MIN_BALANCE))
    row = cur.fetchone()
    conn.close()
    return row


def ensure_weekly_manitto(user_id, user_name):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM manitto_assignments
    WHERE week_start = ? AND hunter_user_id = ?
    """, (week_start, user_id))
    row = cur.fetchone()
    if row:
        conn.close()
        return row, None
    conn.close()

    target = manitto_target_candidates(user_id)
    if not target:
        return None, "마니또 대상을 지정할 수 없습니다. 5코인 이상 보유한 활성 유저가 부족합니다."

    manitto_type = "golden" if random.randint(1, 100) <= GOLDEN_MANITTO_RATE else "normal"
    reward_min = MANITTO_REWARD_MIN
    reward_max = 80 if manitto_type == "golden" else MANITTO_REWARD_MAX
    required_score = 10 if manitto_type == "golden" else MANITTO_REQUIRED_SCORE

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO manitto_assignments (
        week_start, week_end, hunter_user_id, hunter_user_name,
        target_user_id, target_user_name, required_score,
        reward_min, reward_max, manitto_type, completed,
        created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
    """, (
        week_start, week_end, user_id, user_name,
        target["user_id"], target["user_name"], required_score,
        reward_min, reward_max, manitto_type, now_str(), now_str()
    ))
    conn.commit()
    cur.execute("""
    SELECT * FROM manitto_assignments
    WHERE week_start = ? AND hunter_user_id = ?
    """, (week_start, user_id))
    row = cur.fetchone()
    conn.close()
    return row, None


def get_affinity_score(user_id_1, user_id_2, week_start=None):
    if not week_start:
        week_start, _ = event_week_key()
    a, b = pair_key(user_id_1, user_id_2)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT score FROM affinity_scores
    WHERE week_start = ? AND user_a = ? AND user_b = ?
    """, (week_start, a, b))
    row = cur.fetchone()
    conn.close()
    return row["score"] if row else 0


def push_private_message(user_id, text_value):
    try:
        from linebot.v3.messaging import PushMessageRequest, TextMessage
        with ApiClient(config) as client:
            api = MessagingApi(client)
            api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=text_value)]))
        return True
    except Exception as e:
        print("PRIVATE_PUSH_ERROR:", e)
        return False


def manitto_private_text(row):
    title = "🌈 황금 마니또" if row["manitto_type"] == "golden" else "🎭 S.N.S 마니또"
    score = get_affinity_score(row["hunter_user_id"], row["target_user_id"], row["week_start"])
    status = "완료" if row["completed"] else "진행중"
    reward_text = coin_text(row["reward"]) if row["reward"] else f"{coin_text(row['reward_min'])} ~ {coin_text(row['reward_max'])} 랜덤"
    return (
        f"{title}\n\n"
        f"이번 주 대상\n{row['target_user_name']}\n\n"
        f"조건\n메인방에서 대상과 친밀도 {row['required_score']} 달성\n\n"
        f"현재 친밀도\n{score} / {row['required_score']}\n\n"
        f"성공 보상\n{reward_text}\n\n"
        f"상태\n{status}\n\n"
        "※ 마니또 대상은 본인에게만 공개됩니다.\n"
        "※ 같은 사람 연속 발화, 3분 초과 응답, 30초 내 반복 대화는 제외됩니다."
    )


def send_manitto_dm(user_id, user_name):
    row, err = ensure_weekly_manitto(user_id, user_name)
    if err:
        return err
    ok = push_private_message(user_id, manitto_private_text(row))
    if ok:
        return "🎭 마니또 정보를 개인 메시지로 보내드렸습니다."
    return (
        "🎭 개인 메시지 전송에 실패했습니다.\n\n"
        "봇을 친구추가한 뒤 다시 /마니또 를 입력해주세요.\n"
        "대상 보호를 위해 공방에는 마니또 정보를 공개하지 않습니다."
    )


def complete_manitto_if_ready(hunter_user_id, hunter_user_name, other_user_id):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM manitto_assignments
    WHERE week_start = ?
      AND hunter_user_id = ?
      AND target_user_id = ?
      AND completed = 0
    """, (week_start, hunter_user_id, other_user_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None

    score = get_affinity_score(hunter_user_id, other_user_id, week_start)
    if score < row["required_score"]:
        return None

    reward = random.randint(row["reward_min"], row["reward_max"])
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE manitto_assignments
    SET completed = 1,
        reward = ?,
        updated_at = ?,
        completed_at = ?
    WHERE week_start = ? AND hunter_user_id = ?
    """, (reward, now_str(), now_str(), week_start, hunter_user_id))
    conn.commit()
    conn.close()

    change_money(hunter_user_id, hunter_user_name, reward, f"마니또 성공: {row['target_user_name']}", None, "마니또시스템")
    grant_achievement_once(hunter_user_id, hunter_user_name, "first_manitto", "🎭 첫 마니또", 5, f"target={row['target_user_name']}")

    dm_text = (
        "🎭 마니또 성공!\n\n"
        f"대상: {row['target_user_name']}\n"
        f"친밀도: {score} / {row['required_score']}\n"
        f"보상: {coin_text(reward)}\n\n"
        f"현재 잔액: {coin_text(get_balance(hunter_user_id))}"
    )
    push_private_message(hunter_user_id, dm_text)
    return f"🎭 누군가의 마니또 미션이 성공했습니다."


def process_affinity_message(source_id, user_id, user_name, text_value):
    if source_id != COUNT_SOURCE_ID or not user_id or not text_value:
        return None
    if text_value.startswith('/'):
        return None

    now_dt = datetime.now(KST)
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT user_id, user_name, last_at FROM chat_last_speakers WHERE source_id = ?", (source_id,))
    last = cur.fetchone()

    cur.execute("""
    INSERT INTO chat_last_speakers (source_id, user_id, user_name, last_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(source_id)
    DO UPDATE SET user_id = excluded.user_id,
                  user_name = excluded.user_name,
                  last_at = excluded.last_at
    """, (source_id, user_id, user_name, now_str()))

    if not last or last["user_id"] == user_id:
        conn.commit()
        conn.close()
        return None

    last_dt = parse_time_kst(last["last_at"])
    if not last_dt or (now_dt - last_dt).total_seconds() > AFFINITY_REPLY_WINDOW_SECONDS:
        conn.commit()
        conn.close()
        return None

    a, b = pair_key(user_id, last["user_id"])
    cur.execute("""
    SELECT last_at FROM affinity_pair_cooldowns
    WHERE source_id = ? AND week_start = ? AND user_a = ? AND user_b = ?
    """, (source_id, week_start, a, b))
    cooldown = cur.fetchone()
    if cooldown:
        cooldown_dt = parse_time_kst(cooldown["last_at"])
        if cooldown_dt and (now_dt - cooldown_dt).total_seconds() < AFFINITY_PAIR_COOLDOWN_SECONDS:
            conn.commit()
            conn.close()
            return None

    if a == user_id:
        a_name, b_name = user_name, last["user_name"]
    else:
        a_name, b_name = last["user_name"], user_name

    cur.execute("""
    INSERT INTO affinity_scores (week_start, user_a, user_b, user_a_name, user_b_name, score, updated_at)
    VALUES (?, ?, ?, ?, ?, 1, ?)
    ON CONFLICT(week_start, user_a, user_b)
    DO UPDATE SET score = score + 1,
                  user_a_name = excluded.user_a_name,
                  user_b_name = excluded.user_b_name,
                  updated_at = excluded.updated_at
    """, (week_start, a, b, a_name, b_name, now_str()))

    cur.execute("""
    INSERT INTO affinity_pair_cooldowns (source_id, week_start, user_a, user_b, last_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(source_id, week_start, user_a, user_b)
    DO UPDATE SET last_at = excluded.last_at
    """, (source_id, week_start, a, b, now_str()))

    conn.commit()
    conn.close()

    messages = []
    msg1 = complete_manitto_if_ready(user_id, user_name, last["user_id"])
    if msg1:
        messages.append(msg1)
    msg2 = complete_manitto_if_ready(last["user_id"], last["user_name"], user_id)
    if msg2:
        messages.append(msg2)

    if messages:
        return "\n".join(dict.fromkeys(messages))
    return None


def affinity_status_text(user_id, user_name):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_a, user_b, user_a_name, user_b_name, score
    FROM affinity_scores
    WHERE week_start = ? AND (user_a = ? OR user_b = ?)
    ORDER BY score DESC, updated_at DESC
    LIMIT 10
    """, (week_start, user_id, user_id))
    rows = cur.fetchall()
    conn.close()

    lines = ["💞 내 친밀도", f"기간: {week_start} ~ {week_end}", ""]
    if not rows:
        lines.append("이번 주 친밀도 기록이 없습니다.")
    else:
        for i, row in enumerate(rows, 1):
            other_name = row["user_b_name"] if row["user_a"] == user_id else row["user_a_name"]
            lines.append(f"{i}. {other_name} - {row['score']}")
    lines.append("")
    lines.append("※ 3분 이내 서로 번갈아 대화하면 친밀도가 오릅니다.")
    return "\n".join(lines)


def affinity_ranking_text(limit=20):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_a_name, user_b_name, score
    FROM affinity_scores
    WHERE week_start = ?
    ORDER BY score DESC, updated_at DESC
    LIMIT ?
    """, (week_start, limit))
    rows = cur.fetchall()
    conn.close()

    lines = ["💞 친밀도 랭킹", f"기간: {week_start} ~ {week_end}", ""]
    if not rows:
        lines.append("이번 주 친밀도 기록이 없습니다.")
    else:
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_a_name']} ↔ {row['user_b_name']} - {row['score']}")
    return format_long_lines("", lines).strip()


def manitto_admin_status_text():
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT hunter_user_name, target_user_name, required_score, reward, reward_min, reward_max, completed, manitto_type
    FROM manitto_assignments
    WHERE week_start = ?
    ORDER BY completed DESC, hunter_user_name ASC
    LIMIT 80
    """, (week_start,))
    rows = cur.fetchall()
    conn.close()

    lines = ["🎭 마니또 운영 현황", f"기간: {week_start} ~ {week_end}", ""]
    if not rows:
        lines.append("아직 발급된 마니또가 없습니다.")
    else:
        for i, row in enumerate(rows, 1):
            mark = "✅" if row["completed"] else "진행"
            kind = "황금" if row["manitto_type"] == "golden" else "일반"
            reward = coin_text(row["reward"]) if row["reward"] else f"{coin_text(row['reward_min'])}~{coin_text(row['reward_max'])}"
            lines.append(f"{i}. {row['hunter_user_name']} → {row['target_user_name']} / {kind} / 목표 {row['required_score']} / {reward} / {mark}")
    return format_long_lines("", lines).strip()

# =========================
# 출력 포맷
# =========================
def gender_icon(gender):
    return ""


def nomicl_text(is_nomicl):
    return ""


def format_rows(title, date_str, rows):
    lines = [title, f"날짜: {date_str}", ""]
    if not rows:
        lines.append("데이터가 없습니다.")
        return "\n".join(lines)

    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. {row['user_name']} - {row['count']}"
        )
    return "\n".join(lines)


def format_total_rows(title, rows):
    lines = [title, ""]
    if not rows:
        lines.append("데이터가 없습니다.")
        return "\n".join(lines)

    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. {row['user_name']} - {row['count']}"
        )
    return "\n".join(lines)


# =========================
# WEBHOOK
# =========================
@app.route("/", methods=["GET"])
def home():
    return "LINE MADI COUNTER BOT RUNNING"


@app.route("/", methods=["POST"])
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print("ERROR:", e)
        abort(500)

    return "OK"


# =========================
# EVENT
# =========================
@handler.add(MessageEvent)
def handle(event):
    source_id = get_source_id(event)
    user_id = get_event_user_id(event)
    user_name = get_user_name(event)
    date_str = today()

    print("SOURCE_ID:", source_id)
    print("USER_ID:", user_id)
    print("USER_NAME:", user_name)

    if user_id:
        upsert_user(user_id, user_name, source_id)

    # 메인방 + 운영진방 둘 다 마디수 카운트
    if source_id in count_source_ids() and user_id:
        add_count(date_str, source_id, user_id, user_name)

        # 히든 미션 자동 체크
        try:
            check_hidden_1000_reward(date_str, source_id, user_id, user_name)
            check_hidden_2000_reward(date_str, source_id, user_id, user_name)
            check_lucky_log_rewards(date_str, source_id, user_id, user_name)
            check_lucky_guy_reward(date_str, source_id, user_id, user_name)
        except Exception as e:
            print("HIDDEN_REWARD_ERROR:", e)

    if not isinstance(event.message, TextMessageContent):
        return

    text = (event.message.text or "").strip()

    try:
        affinity_msg = process_affinity_message(source_id, user_id, user_name, text)
        if affinity_msg:
            reply(event.reply_token, affinity_msg)
            return
    except Exception as e:
        print("AFFINITY_PROCESS_ERROR:", e)

    # 토요일 21시 이후 첫 메시지에서 S.N.S 럭키드로우 자동 추첨
    try:
        maybe_auto_lucky_draw()
    except Exception as e:
        print("SNS_LUCKY_AUTO_ERROR:", e)

    # =========================
    # 누구나 사용 가능한 명령어
    # =========================
    if text == "/방정보":
        reply(
            event.reply_token,
            f"방정보\n\n"
            f"SOURCE_ID:\n{source_id}\n\n"
            f"USER_ID:\n{user_id or 'NO_USER_ID'}\n\n"
            f"닉네임:\n{user_name}\n\n"
            f"관리자방 여부:\n{source_id in ADMIN_SOURCE_IDS}\n\n"
            f"관리자 권한 여부:\n{is_staff(user_id) if user_id else False}\n\n"
            f"버전:\n{BOT_VERSION}"
        )
        return

    if text == "/버전":
        reply(event.reply_token, f"봇 버전\n\n{BOT_VERSION}")
        return

    if text == "/상태확인":
        reply(
            event.reply_token,
            f"봇 상태 확인\n\n"
            f"버전:\n{BOT_VERSION}\n\n"
            f"현재 SOURCE_ID:\n{source_id}\n\n"
            f"등록된 관리자방 수:\n{len(ADMIN_SOURCE_IDS)}\n"
            f"현재 방 관리자방 여부:\n{source_id in ADMIN_SOURCE_IDS}\n\n"
            f"현재 USER_ID:\n{user_id or 'NO_USER_ID'}\n"
            f"관리자/운영자 권한 여부:\n{is_staff(user_id)}\n\n"
            f"메인방 여부:\n{source_id == COUNT_SOURCE_ID}"
        )
        return

    if text == "/잔액":
        balance = get_balance(user_id)
        reply(event.reply_token, f"💰 {user_name}님의 보유 {CURRENCY_NAME}\n\n{coin_text(balance)}")
        return

    if text == "/도움말":
        reply(event.reply_token, "일반 명령어: /명령어\n운영 명령어: /운영명령어")
        return

    if text == "/명령어":
        reply(
            event.reply_token,
            "📌 일반 명령어\n\n"
            "🎁 출석·미션\n"
            "/출석\n"
            "/미션\n"
            "/미션수령\n\n"
            "🛒 상점\n"
            "/상점\n"
            "/구매 상품명\n"
            "/내보유\n"
            "/사용 구매번호\n\n"
            "🎰 가챠\n"
            "/가챠시스템\n\n"
            "🎖 업적·현상금\n"
            "/업적\n"
            "/현상금\n\n"
            "🎟️ S.N.S 럭키드로우\n"
            "/SNS럭키구매\n"
            "/SNS럭키현황\n"
            "/SNS럭키설명\n\n"
            "🎱 S.N.S 핀볼\n"
            "/SNS핀볼구매\n"
            "/SNS핀볼현황\n"
            "/SNS핀볼설명"
        )
        return

    if text == "/가챠시스템":
        reply(event.reply_token, gacha_system_text())
        return


    if text == "/업적":
        ok = push_private_message(user_id, achievement_status_text(user_id, user_name))
        if ok:
            reply(event.reply_token, "🎖 업적 현황을 개인 메시지로 보내드렸습니다.")
        else:
            reply(
                event.reply_token,
                "🎖 개인 메시지 전송에 실패했습니다.\n\n"
                "봇을 친구추가한 뒤 다시 /업적 을 입력해주세요.\n"
                "업적 현황은 본인에게만 보이도록 공방에는 공개하지 않습니다."
            )
        return

    if text in ["/마니또", "/마니또확인"]:
        reply(event.reply_token, send_manitto_dm(user_id, user_name))
        return

    if text in ["/친밀도", "/내친밀도"]:
        reply(event.reply_token, affinity_status_text(user_id, user_name))
        return

    if text in ["/친밀도랭킹", "/친밀도순위"]:
        reply(event.reply_token, affinity_ranking_text())
        return

    if text in ["/현상금", "/현상금현황"]:
        reply(event.reply_token, "현상금은 🎭 S.N.S 마니또로 개편되었습니다.\n확인: /마니또")
        return

    if text.startswith("/가챠타입"):
        parts = text.split(maxsplit=1)

        if len(parts) == 1:
            current_type = get_gacha_type(user_id)
            reply(
                event.reply_token,
                f"🎰 현재 가챠 타입\n\n"
                f"{GACHA_TYPE_LABELS[current_type]}\n\n"
                f"변경 방법\n"
                f"/가챠타입 코인\n"
                f"/가챠타입 조각\n"
                f"/가챠타입 랜덤"
            )
            return

        raw_type = parts[1].strip()

        aliases = {
            "코인": "coin",
            "코인형": "coin",
            "조각": "piece",
            "조각형": "piece",
            "랜덤": "random",
            "랜덤형": "random",
        }

        if raw_type not in aliases:
            reply(event.reply_token, "사용법\n\n/가챠타입 코인\n/가챠타입 조각\n/가챠타입 랜덤")
            return

        gacha_type = aliases[raw_type]
        set_gacha_type(user_id, user_name, gacha_type)

        reply(
            event.reply_token,
            f"✅ 가챠 타입 변경 완료\n\n"
            f"현재 타입: {GACHA_TYPE_LABELS[gacha_type]}"
        )
        return

    if text.startswith("/가챠"):
        parts = text.split(maxsplit=1)

        if len(parts) == 1:
            reply(event.reply_token, "사용법\n\n/가챠 하\n/가챠 중\n/가챠 상\n\n자세한 안내: /가챠시스템")
            return

        tier = parts[1].strip()

        success, message = run_gacha(user_id, user_name, tier)
        if success:
            grant_achievement_once(user_id, user_name, "first_gacha", "🎰 첫 가챠", 2, tier)
        reply(event.reply_token, message)
        return

    if text in ["/행운포인트", "/가챠포인트"]:
        pity_points = get_gacha_pity_point(user_id)
        reply(
            event.reply_token,
            f"🎁 가챠 행운포인트\n\n"
            f"현재: {pity_points} / 10\n\n"
            f"코인형 가챠 F등급 1회마다 1점 적립\n"
            f"10점 달성 시 💰1코인 자동 지급"
        )
        return

    if text in ["/조각", "/조각보유"]:
        rows = get_all_gacha_pieces(user_id)

        if not rows:
            reply(event.reply_token, "보유한 조각이 없습니다.")
            return

        lines = ["🧩 보유 조각", ""]
        for row in rows:
            info = PIECE_INFO.get(row["piece_key"])
            if not info:
                continue
            lines.append(f"{info['label']} {row['count']} / {info['need']}")

        reply(event.reply_token, "\n".join(lines))
        return


    if text == "/출석":
        ok, balance = attendance_check(date_str, user_id, user_name)
        if not ok:
            reply(event.reply_token, f"이미 오늘 출석했습니다.\n현재 잔액: {coin_text(balance)}")
            return

        grant_achievement_once(user_id, user_name, "first_attendance", "✅ 첫 출석", 2, date_str)
        streak, paid = check_attendance_streak_reward(date_str, user_id, user_name)
        balance = get_balance(user_id)

        lines = [
            "✅ 출석 완료",
            "",
            f"+0.2{CURRENCY_NAME} 지급",
            f"연속출석: {streak}일",
        ]

        if paid:
            lines.append("")
            lines.append("🎁 연속출석 보상")
            for days, reward in paid:
                lines.append(f"{days}일 달성 +{coin_text(reward)}")

        lines.append("")
        lines.append(f"현재 잔액: {coin_text(balance)}")

        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/미션":
        count, missions = mission_status(date_str, COUNT_SOURCE_ID, user_id)
        lines = [
            "🎯 오늘의 미션",
            f"오늘 마디수: {count}",
            "",
        ]

        total_waiting = 0
        for mission in missions:
            mark = "✅" if mission["done"] else "❌"
            received = " / 수령완료" if mission["received"] else ""
            if mission["done"] and not mission["received"]:
                total_waiting += mission["reward"]

            lines.append(
                f"{mark} {mission['required']}마디 달성 "
                f"+{coin_text(mission['reward'])}{received}"
            )

        lines.append("")
        lines.append(f"수령 가능: {coin_text(total_waiting)}")
        lines.append("수령 방법: /미션수령")

        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/미션수령":
        reward, count, claimed_names = claim_missions(date_str, COUNT_SOURCE_ID, user_id, user_name)
        if reward <= 0:
            reply(
                event.reply_token,
                f"수령 가능한 미션 보상이 없습니다.\n오늘 마디수: {count}\n확인: /미션"
            )
            return

        balance = get_balance(user_id)
        reply(
            event.reply_token,
            f"🎁 미션 보상 수령 완료\n\n"
            f"달성: {', '.join(claimed_names)}\n"
            f"지급: {coin_text(reward)}\n"
            f"잔액: {coin_text(balance)}"
        )
        return



    if text in ["/SNS럭키설명", "/럭키드로우설명"]:
        reply(
            event.reply_token,
            "🎟️ S.N.S 럭키드로우\n\n"
            "1인 1장만 구매 가능합니다.\n"
            "가격: 1장 = 1코인\n"
            "추첨: 매주 토요일 21:00\n\n"
            "구매자 중 1명을 랜덤 추첨하여\n"
            "기본 부스팅 5코인 + 총 판매액의 80%를 지급합니다.\n"
            "나머지 20%는 시스템 소각됩니다.\n\n"
            "구매: /SNS럭키구매\n"
            "현황: /SNS럭키현황"
        )
        return

    if text in ["/SNS럭키구매", "/럭키드로우구매"]:
        success, msg = buy_lucky_draw_ticket(user_id, user_name)
        if success:
            grant_achievement_once(user_id, user_name, "first_lucky", "🎟️ 첫 럭키드로우", 2, "S.N.S 럭키드로우")
        reply(event.reply_token, msg)
        return

    if text in ["/SNS럭키현황", "/럭키드로우현황"]:
        reply(event.reply_token, lucky_draw_status_text())
        return

    if text in ["/SNS핀볼설명", "/핀볼설명"]:
        reply(
            event.reply_token,
            "🎱 S.N.S 핀볼\n\n"
            "치지직 시청자 참여 핀볼 느낌의 주간 이벤트입니다.\n"
            "가격: 1장 = 1코인\n"
            "구매 제한: 1인 최대 3장\n\n"
            "운영진이 당첨 인원을 선정하면\n"
            "기본 부스팅 5코인 + 총 판매액의 80%를 당첨자에게 균등 지급합니다.\n"
            "나머지 20%는 시스템 소각됩니다.\n\n"
            "구매: /SNS핀볼구매 또는 /SNS핀볼구매 3\n"
            "현황: /SNS핀볼현황"
        )
        return

    if text.startswith("/SNS핀볼구매") or text.startswith("/핀볼구매"):
        parts = text.split(maxsplit=1)
        amount = 1
        if len(parts) == 2:
            try:
                amount = int(parts[1])
            except ValueError:
                reply(event.reply_token, "사용법\n\n/SNS핀볼구매\n/SNS핀볼구매 3")
                return
        success, msg = buy_pinball_ticket(user_id, user_name, amount)
        if success:
            grant_achievement_once(user_id, user_name, "first_pinball", "🎱 첫 핀볼", 2, "S.N.S 핀볼")
        reply(event.reply_token, msg)
        return

    if text in ["/SNS핀볼현황", "/핀볼현황"]:
        reply(event.reply_token, pinball_status_text())
        return

    if text == "/상점":
        rows = list_shop_items()
        if not rows:
            reply(event.reply_token, "상점에 등록된 상품이 없습니다.")
            return

        lines = [
            "✦ ⎯⎯🛒 코인 마켓 🛒⎯⎯ ✦",
            "-코인으로 아이템 구입가능 (양도불가)",
            "-이벤트 상품, 구입 아이템 환불 시 ➡️ 50% 환불",
            "----------------------------",
        ]

        for row in rows:
            lines.append(f"💠{row['name']} : 💰{points_to_coin(row['price'])}개")
            if row["description"]:
                for desc in str(row["description"]).split(" / "):
                    lines.append(f"-{desc}")
            lines.append("")

        lines.append("구매 방법: /구매 상품명")
        lines.append("보유 확인: /내보유")

        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/구매 "):
        item_name = text.replace("/구매", "", 1).strip()
        success, msg = buy_item(user_id, user_name, item_name)
        reply(event.reply_token, msg)
        return

    if text in ["/내보유", "/내구매", "/보유"]:
        rows = list_user_purchases(user_id)
        if not rows:
            reply(event.reply_token, "보유하거나 구매한 상품이 없습니다.")
            return

        lines = ["🎁 내 상품 보유 현황", ""]
        for row in rows:
            used_info = ""
            if row["status"] == "used":
                used_info = f"\n   사용일: {row['used_at']}"
            lines.append(
                f"#{row['id']} {row['item_name']} / {coin_text(row['price'])}\n"
                f"   상태: {status_text(row['status'])}{used_info}"
            )

        lines.append("")
        lines.append("사용 방법: /사용 구매번호")
        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/사용 "):
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            reply(event.reply_token, "사용법\n\n/사용 구매번호")
            return

        note = parts[2] if len(parts) >= 3 else ""
        success, msg = use_purchase(int(parts[1]), user_id, user_name, note)
        reply(event.reply_token, msg)
        return

    # 운영진방 아니면 관리자 명령어 무시
    if source_id not in ADMIN_SOURCE_IDS:
        return

    # 관리자/운영자 아니면 무시
    if not is_staff(user_id):
        return

    # =========================
    # 도움말
    # =========================
    if text == "/운영명령어":
        reply(
            event.reply_token,
            "🛠 운영 명령어\n\n"
            "📊 마디수\n"
            "/마디수\n"
            "/순위\n"
            "/경고\n"
            "/경고기준\n"
            "※ 날짜 조회 가능: /마디수 2026-06-10\n\n"
            "👥 유저관리\n"
            "/유저검색\n"
            "/유저동기화\n"
            "/퇴장처리\n"
            "/복구처리\n"
            "※ 닉네임 또는 USER_ID 사용 가능\n\n"
            "📡 수집확인\n"
            "/수집상태\n"
            "/최근로그\n"
            "/유저상세\n"
            "/DB상태\n\n"
            "💰 코인관리\n"
            "/지급\n"
            "/차감\n"
            "/코인순위\n"
            "/코인내역\n\n"
            "🏆 주간랭킹\n"
            "/주간랭킹\n"
            "/주간정산\n"
            "/주간초기화\n\n"
            "🛒 상점관리\n"
            "/상품등록\n"
            "/상품삭제\n"
            "/보유목록\n"
            "/사용처리\n\n"
            "📖 족보\n"
            "/족보저장\n"
            "/족보보기\n"
            "/족보코인\n\n"
            "🧹 초기화\n"
            "/초기화\n"
            "/전체초기화\n\n"
            "🎟️ S.N.S 이벤트\n"
            "/SNS럭키추첨\n"
            "/SNS핀볼정산 닉네임1 닉네임2\n"
            "/현상금목록\n\n"
            "⚙️ 시스템\n"
            "/방정보\n"
            "/버전"
        )
        return


    if text in ["/마니또목록", "/마니또현황전체"]:
        reply(event.reply_token, manitto_admin_status_text())
        return

    if text in ["/친밀도랭킹", "/친밀도순위"]:
        reply(event.reply_token, affinity_ranking_text())
        return

    if text in ["/현상금목록", "/현상금현황전체"]:
        reply(event.reply_token, bounty_admin_status_text())
        return

    if text in ["/SNS럭키추첨", "/럭키드로우추첨"]:
        ok, msg = settle_lucky_draw(user_name)
        reply(event.reply_token, msg)
        return

    if text.startswith("/SNS핀볼정산") or text.startswith("/핀볼정산"):
        parts = text.split()[1:]
        ok, msg = settle_pinball_by_winners(parts, user_name)
        reply(event.reply_token, msg)
        return

    # =========================
    # 마디수 조회
    # =========================
    if text.startswith("/마디수"):
        target_date = parse_date(text)
        rows = ranking(target_date, COUNT_SOURCE_ID)
        reply(event.reply_token, format_rows("📊 메인방 전체 마디수", target_date, rows))
        return

    if text.startswith("/순위"):
        target_date = parse_date(text)
        rows = ranking(target_date, COUNT_SOURCE_ID, limit=10)
        reply(event.reply_token, format_rows("🏆 메인방 순위 TOP 10", target_date, rows))
        return

    if text.startswith("/전체순위"):
        rows = total_ranking(COUNT_SOURCE_ID, limit=30)
        reply(event.reply_token, format_total_rows("🏆 전체 누적 순위 TOP 30", rows))
        return

    if text == "/경고기준":
        reply(
            event.reply_token,
            f"⚠️ 현재 마디수 경고 기준\n\n{WARNING_LIMIT}마디 미만"
        )
        return

    if text.startswith("/경고"):
        target_date = parse_date(text)
        rows = warning_list(target_date, COUNT_SOURCE_ID)

        lines = [
            "🚨 경고 대상",
            f"날짜: {target_date}",
            f"남자 기준: {MALE_LIMIT} 미만",
            f"여자 기준: {FEMALE_LIMIT} 미만",
            "",
        ]

        if not rows:
            lines.append("경고 대상 없음")
        else:
            for i, row in enumerate(rows, 1):
                gender = row["gender"]
                if gender == "male":
                    limit = MALE_LIMIT
                elif gender == "female":
                    limit = FEMALE_LIMIT
                else:
                    limit = "성별 미설정"

                lines.append(
                    f"{i}. {gender_icon(gender)} "
                    f"{row['user_name']} - {row['count']} "
                    f"/ 기준 {limit}"
                )

        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/유저동기화":
        inserted, updated = sync_users_from_history()
        reply(
            event.reply_token,
            f"🔄 유저 동기화 완료\n\n추가: {inserted}명\n갱신: {updated}건"
        )
        return

    if text.startswith("/유저검색ID "):
        target_user_id = text.replace("/유저검색ID", "", 1).strip()
        user = get_user_by_id(target_user_id)

        if not user:
            reply(event.reply_token, f"USER_ID를 찾지 못했습니다.\n{target_user_id}")
            return

        active_label = "활성" if user["is_active"] else "퇴장처리됨"
        reply(
            event.reply_token,
            f"🔎 유저검색ID 결과\n\n닉네임: {user['user_name']}\n상태: {active_label}\nUSER_ID:\n{user['user_id']}"
        )
        return

    if text.startswith("/미션확인 "):
        keyword = text.replace("/미션확인", "", 1).strip()
        matches = find_users(keyword, limit=5)

        if not matches:
            reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {keyword}\n\n/유저동기화 후 다시 시도해보세요.")
            return

        if len(matches) > 1:
            lines = [f"검색 결과가 여러 명입니다: {keyword}", ""]
            for i, row in enumerate(matches, 1):
                active_label = "활성" if row["is_active"] else "퇴장처리됨"
                lines.append(f"{i}. {row['user_name']} / {active_label}\n   USER_ID: {row['user_id']}")
            reply(event.reply_token, "\n".join(lines))
            return

        target = matches[0]
        count, missions = mission_status(date_str, COUNT_SOURCE_ID, target["user_id"])

        lines = [
            f"🎯 {target['user_name']} 미션 확인",
            f"오늘 마디수: {count}",
            "",
        ]

        for mission in missions:
            mark = "✅" if mission["done"] else "❌"
            received = " / 수령완료" if mission["received"] else ""
            lines.append(f"{mark} {mission['required']}마디 +{coin_text(mission['reward'])}{received}")

        reply(event.reply_token, "\n".join(lines))
        return


    if text.startswith("/유저검색"):
        keyword = text.replace("/유저검색", "", 1).strip()

        if not keyword:
            reply(event.reply_token, "사용법\n\n/유저검색 닉네임")
            return

        rows = find_users(keyword, limit=10)

        if not rows:
            reply(event.reply_token, f"검색 결과가 없습니다.\n검색어: {keyword}")
            return

        lines = [f"🔎 유저검색 결과: {keyword}", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']}\n   USER_ID: {row['user_id']}")

        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/주간랭킹":
        week_start, week_end = week_range_for_today()
        rows = weekly_ranking_rows(COUNT_SOURCE_ID, week_start, week_end, limit=10)

        if not rows:
            reply(event.reply_token, "이번 주 랭킹 데이터가 없습니다.")
            return

        lines = [
            "🏆 이번 주 마디수 랭킹",
            f"기간: {week_start} ~ {week_end}",
            "",
        ]

        for i, row in enumerate(rows, 1):
            reward = weekly_reward_amount(i)
            lines.append(
                f"{i}. {row['user_name']} - {row['total_count']}마디 "
                f"/ 보상 {coin_text(reward)}"
            )

        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/주간초기화":
        week_start, week_end = week_range_for_today()

        conn = db()
        cur = conn.cursor()
        cur.execute("""
        DELETE FROM weekly_rewards
        WHERE week_start = ?
          AND week_end = ?
        """, (week_start, week_end))
        deleted = cur.rowcount
        conn.commit()
        conn.close()

        reply(
            event.reply_token,
            f"🔄 이번 주 정산 초기화 완료\n\n삭제: {deleted}건\n기간: {week_start} ~ {week_end}"
        )
        return

    if text == "/주간초기화 전체":
        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM weekly_rewards")
        deleted = cur.rowcount
        conn.commit()
        conn.close()

        reply(
            event.reply_token,
            f"⚠️ 전체 주간보상 이력 삭제 완료\n\n삭제: {deleted}건"
        )
        return

    if text == "/주간정산":
        week_start, week_end = week_range_for_today()
        paid = settle_weekly_rewards(COUNT_SOURCE_ID, week_start, week_end)

        if not paid:
            reply(
                event.reply_token,
                f"이미 정산했거나 정산할 데이터가 없습니다.\n기간: {week_start} ~ {week_end}"
            )
            return

        lines = [
            "💰 주간 랭킹 코인 지급 완료",
            f"기간: {week_start} ~ {week_end}",
            "",
        ]

        for item in paid:
            lines.append(
                f"{item['rank']}위 {item['user_name']} "
                f"- {item['count']}마디 / +{coin_text(item['reward'])}"
            )

        reply(event.reply_token, "\n".join(lines))
        return


    if text.startswith("/유저검색"):
        keyword = text.replace("/유저검색", "", 1).strip()

        if not keyword:
            reply(event.reply_token, "사용법\n\n/유저검색 닉네임")
            return

        rows = find_users(keyword, limit=10)

        if not rows:
            reply(event.reply_token, f"검색 결과가 없습니다.\n검색어: {keyword}")
            return

        lines = [f"🔎 유저검색 결과: {keyword}", ""]
        for i, row in enumerate(rows, 1):
            active_label = "활성" if row["is_active"] else "퇴장처리됨"
            lines.append(f"{i}. {row['user_name']} / {active_label}\n   USER_ID: {row['user_id']}")

        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/퇴장처리ID "):
        target_user_id = text.replace("/퇴장처리ID", "", 1).strip()

        if not target_user_id:
            reply(event.reply_token, "사용법\n\n/퇴장처리ID USER_ID")
            return

        changed, target_name = set_user_active_by_id_with_name(target_user_id, 0)

        if changed == 0:
            reply(event.reply_token, f"처리할 유저를 찾지 못했습니다.\nUSER_ID: {target_user_id}")
            return

        reply(
            event.reply_token,
            "🚪 퇴장 처리 완료\n\n"
            f"닉네임: {target_name}\n"
            f"USER_ID: {target_user_id}\n\n"
            "이제 마디수/순위/경고 조회에서 제외됩니다."
        )
        return

    if text.startswith("/복구처리ID "):
        target_user_id = text.replace("/복구처리ID", "", 1).strip()

        if not target_user_id:
            reply(event.reply_token, "사용법\n\n/복구처리ID USER_ID")
            return

        changed, target_name = set_user_active_by_id_with_name(target_user_id, 1)

        if changed == 0:
            reply(event.reply_token, f"복구할 유저를 찾지 못했습니다.\nUSER_ID: {target_user_id}")
            return

        reply(
            event.reply_token,
            "✅ 복구 처리 완료\n\n"
            f"닉네임: {target_name}\n"
            f"USER_ID: {target_user_id}\n\n"
            "이제 마디수/순위/경고 조회에 다시 포함됩니다."
        )
        return

    if text.startswith("/퇴장처리 "):
        keyword = text.replace("/퇴장처리", "", 1).strip()

        if not keyword:
            reply(event.reply_token, "사용법\n\n/퇴장처리 닉네임")
            return

        changed, names = set_user_active_by_name(keyword, 0)

        if changed == 0:
            reply(event.reply_token, f"처리할 유저를 찾지 못했습니다.\n검색어: {keyword}")
            return

        reply(
            event.reply_token,
            "🚪 퇴장 처리 완료\n\n"
            f"처리 인원: {changed}명\n"
            + "\n".join([f"- {name}" for name in names])
            + "\n\n이제 마디수/순위/경고 조회에서 제외됩니다."
        )
        return

    if text.startswith("/복구처리 "):
        keyword = text.replace("/복구처리", "", 1).strip()

        if not keyword:
            reply(event.reply_token, "사용법\n\n/복구처리 닉네임")
            return

        changed, names = set_user_active_by_name(keyword, 1)

        if changed == 0:
            reply(event.reply_token, f"복구할 유저를 찾지 못했습니다.\n검색어: {keyword}")
            return

        reply(
            event.reply_token,
            "✅ 복구 처리 완료\n\n"
            f"처리 인원: {changed}명\n"
            + "\n".join([f"- {name}" for name in names])
            + "\n\n이제 마디수/순위/경고 조회에 다시 포함됩니다."
        )
        return


    if text == "/수집상태":
        log_row, count_row, all_rows = collection_status(COUNT_SOURCE_ID, date_str)

        lines = [
            "📡 메인방 수집 상태",
            f"날짜: {date_str}",
            "",
            f"chat_logs 메시지 수: {log_row['total_logs']}",
            f"chat_logs 발화 유저 수: {log_row['active_users']}",
            "",
            f"counts 전체 마디수: {count_row['total_madi']}",
            f"counts 집계 유저 수: {count_row['counted_users']}",
            "",
            f"전체 수집 인원: {len(all_rows)}명",
            "",
        ]

        if not all_rows:
            lines.append("데이터 없음")
        else:
            for i, row in enumerate(all_rows, 1):
                lines.append(f"{i}. {row['user_name']} - {row['count']}")

        reply(event.reply_token, format_long_lines("", lines).strip())
        return

    if text == "/수집누락":
        users_no_count, logs_no_count, counts_no_user = collection_missing(COUNT_SOURCE_ID, date_str)

        lines = [
            "🧩 메인방 수집 누락 점검",
            f"날짜: {date_str}",
            "",
            f"1) 활성 users 중 오늘 마디수 없음: {len(users_no_count)}명",
        ]

        for row in users_no_count:
            lines.append(f"- {row['user_name']} / {row['user_id']}")

        lines.append("")
        lines.append(f"2) chat_logs에는 있는데 counts 없음: {len(logs_no_count)}명")
        for row in logs_no_count:
            lines.append(f"- {row['user_name']} / 로그 {row['logs']}개 / {row['user_id']}")

        lines.append("")
        lines.append(f"3) counts에는 있는데 users 없음: {len(counts_no_user)}명")
        for row in counts_no_user:
            lines.append(f"- {row['user_name']} / {row['count']}마디 / {row['user_id']}")

        reply(event.reply_token, format_long_lines("", lines).strip())
        return

    if text == "/오늘수집":
        log_row, count_row, top_rows = collection_status(source_id, date_str)
        reply(
            event.reply_token,
            f"📡 현재 방 수집 상태\n\n"
            f"날짜: {date_str}\n"
            f"SOURCE_ID: {source_id}\n\n"
            f"chat_logs 메시지 수: {log_row['total_logs']}\n"
            f"chat_logs 발화 유저 수: {log_row['active_users']}\n\n"
            f"counts 전체 마디수: {count_row['total_madi']}\n"
            f"counts 집계 유저 수: {count_row['counted_users']}"
        )
        return

    if text.startswith("/최근로그"):
        parts = text.split(maxsplit=1)
        limit = 20
        if len(parts) == 2 and parts[1].isdigit():
            limit = min(max(int(parts[1]), 1), 50)
        rows = recent_chat_logs(COUNT_SOURCE_ID, limit)
        if not rows:
            reply(event.reply_token, "최근 로그가 없습니다.")
            return
        lines = [f"📝 메인방 최근 로그 {limit}개", ""]
        for row in rows:
            msg = row["text"] or ""
            if len(msg) > 30:
                msg = msg[:30] + "..."
            lines.append(f"{row['created_at']} / {row['user_name']}\n{msg}\nUSER_ID: {row['user_id']}")
        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/유저상세 "):
        keyword = text.replace("/유저상세", "", 1).strip()
        rows = user_debug(keyword)
        if not rows:
            reply(event.reply_token, f"검색 결과가 없습니다.\n검색어: {keyword}")
            return
        lines = [f"🔬 유저 상세: {keyword}", ""]
        for i, row in enumerate(rows, 1):
            active_label = "활성" if row["is_active"] else "퇴장처리됨"
            lines.append(
                f"{i}. {row['user_name']} / {active_label}\n"
                f"USER_ID: {row['user_id']}\n"
                f"누적 마디수: {row['total_count']}\n"
                f"활동일수: {row['active_days']}\n"
                f"로그 수: {row['log_count']}\n"
                f"마지막 로그: {row['last_log']}\n"
                f"잔액: {coin_text(row['balance'])}"
            )
        reply(event.reply_token, "\n\n".join(lines))
        return


    if text == "/럭키가이":
        lucky_number, rows = hidden_reward_status(date_str)
        current_seq = get_today_chat_log_sequence(COUNT_SOURCE_ID, date_str)

        lines = [
            "🍀 럭키가이",
            f"날짜: {date_str}",
            f"오늘의 행운 번호: {lucky_number}",
            f"현재 메인방 로그 순번: {current_seq}",
            "",
            "히든 지급 현황",
        ]

        if not rows:
            lines.append("아직 지급 없음")
        else:
            for row in rows:
                lines.append(
                    f"- {row['mission_key']} / {row['user_name']} / "
                    f"+{coin_text(row['reward'])} / {row['meta'] or '-'}"
                )

        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/히든현황":
        lucky_number, rows = hidden_reward_status(date_str)
        current_seq = get_today_chat_log_sequence(COUNT_SOURCE_ID, date_str)

        lines = [
            "🕵️ 히든 미션 현황",
            f"날짜: {date_str}",
            f"럭키가이 번호: {lucky_number}",
            f"현재 로그 순번: {current_seq}",
            "",
        ]

        if not rows:
            lines.append("지급 내역 없음")
        else:
            for row in rows:
                lines.append(
                    f"- {row['mission_key']} / {row['user_name']} / "
                    f"+{coin_text(row['reward'])} / {row['created_at']} / {row['meta'] or '-'}"
                )

        reply(event.reply_token, "\n".join(lines))
        return


    # =========================
    # 화폐
    # =========================
    if text.startswith("/지급 "):
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            reply(event.reply_token, f"사용법\n/지급 닉네임 금액 사유")
            return

        keyword = parts[1]
        try:
            amount = coin_to_points(parts[2])
        except ValueError as e:
            reply(event.reply_token, str(e))
            return

        if amount <= 0:
            reply(event.reply_token, "지급 금액은 0보다 커야 합니다.")
            return

        reason = parts[3] if len(parts) >= 4 else "운영진 지급"
        matches = find_users(keyword, limit=5)

        if not matches:
            reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {keyword}\n\n먼저 메인방에서 대상자가 아무 말이나 1번 입력해야 DB에 등록됩니다.")
            return

        if len(matches) > 1:
            lines = [f"검색 결과가 여러 명입니다: {keyword}", ""]
            for i, row in enumerate(matches, 1):
                lines.append(f"{i}. {row['user_name']}")
            lines.append("")
            lines.append("더 정확한 닉네임으로 다시 입력해주세요.")
            reply(event.reply_token, "\n".join(lines))
            return

        target = matches[0]

        balance = change_money(
            target["user_id"],
            target["user_name"],
            amount,
            reason,
            user_id,
            user_name
        )

        reply(
            event.reply_token,
            f"💰 {CURRENCY_NAME} 지급 완료\n\n"
            f"대상: {target['user_name']}\n"
            f"지급: {coin_text(amount)}\n"
            f"잔액: {coin_text(balance)}\n"
            f"사유: {reason}"
        )
        return

    if text.startswith("/차감 "):
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            reply(event.reply_token, f"사용법\n/차감 닉네임 금액 사유")
            return

        keyword = parts[1]
        try:
            amount = coin_to_points(parts[2])
        except ValueError as e:
            reply(event.reply_token, str(e))
            return

        if amount <= 0:
            reply(event.reply_token, "차감 금액은 0보다 커야 합니다.")
            return

        reason = parts[3] if len(parts) >= 4 else "운영진 차감"
        matches = find_users(keyword, limit=5)

        if not matches:
            reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {keyword}")
            return

        if len(matches) > 1:
            lines = [f"검색 결과가 여러 명입니다: {keyword}", ""]
            for i, row in enumerate(matches, 1):
                lines.append(f"{i}. {row['user_name']}")
            lines.append("")
            lines.append("더 정확한 닉네임으로 다시 입력해주세요.")
            reply(event.reply_token, "\n".join(lines))
            return

        target = matches[0]

        balance = change_money(
            target["user_id"],
            target["user_name"],
            -amount,
            reason,
            user_id,
            user_name
        )

        reply(
            event.reply_token,
            f"💸 {CURRENCY_NAME} 차감 완료\n\n"
            f"대상: {target['user_name']}\n"
            f"차감: {coin_text(amount)}\n"
            f"잔액: {coin_text(balance)}\n"
            f"사유: {reason}"
        )
        return

    if text.startswith("/잔액 "):
        keyword = text.replace("/잔액", "", 1).strip()
        target = find_user(keyword)
        if not target:
            reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {keyword}")
            return

        balance = get_balance(target["user_id"])
        reply(event.reply_token, f"💰 {target['user_name']}님의 보유 {CURRENCY_NAME}\n\n{coin_text(balance)}")
        return

    if text in ["/화폐순위", "/코인순위"]:
        rows = currency_ranking()
        if not rows:
            reply(event.reply_token, f"{CURRENCY_NAME} 보유 데이터가 없습니다.")
            return

        lines = [f"🏆 {CURRENCY_NAME} 보유 순위", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']} - {coin_text(row['balance'])}")

        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/화폐내역") or text.startswith("/코인내역"):
        parts = text.split(maxsplit=1)

        if len(parts) == 1:
            target_user_id = user_id
            target_user_name = user_name
        else:
            target = find_user(parts[1].strip())
            if not target:
                reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {parts[1].strip()}")
                return
            target_user_id = target["user_id"]
            target_user_name = target["user_name"]

        rows = currency_history(target_user_id)
        if not rows:
            reply(event.reply_token, "화폐 내역이 없습니다.")
            return

        lines = [f"📜 {target_user_name} {CURRENCY_NAME} 내역", ""]
        for row in rows:
            sign = "+" if row["amount"] > 0 else ""
            staff = f" / 처리: {row['staff_user_name']}" if row["staff_user_name"] else ""
            lines.append(f"{row['created_at']} {sign}{coin_text(row['amount'])} / {row['reason']}{staff}")

        reply(event.reply_token, "\n".join(lines))
        return

    # =========================
    # 상점 관리 / 보유 현황
    # =========================
    if text.startswith("/상품등록 "):
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            reply(event.reply_token, "사용법\n/상품등록 상품명 가격 설명")
            return

        name = parts[1]
        try:
            price = coin_to_points(parts[2])
        except ValueError as e:
            reply(event.reply_token, str(e))
            return

        if price < 0:
            reply(event.reply_token, "가격은 0 이상이어야 합니다.")
            return

        description = parts[3] if len(parts) >= 4 else ""
        add_shop_item(name, price, description)

        reply(
            event.reply_token,
            f"🛒 상품 등록 완료\n\n"
            f"상품명: {name}\n"
            f"가격: {coin_text(price)}\n"
            f"설명: {description}"
        )
        return

    if text.startswith("/상품삭제 "):
        name = text.replace("/상품삭제", "", 1).strip()
        changed = remove_shop_item(name)
        reply(event.reply_token, f"상품 삭제 완료\n\n상품명: {name}\n처리: {changed}개")
        return

    if text == "/구매목록":
        rows = list_purchases()
        if not rows:
            reply(event.reply_token, "구매 내역이 없습니다.")
            return

        lines = ["📦 전체 구매 목록", ""]
        for row in rows:
            used_info = f"\n   사용일: {row['used_at']}" if row["used_at"] else ""
            lines.append(
                f"#{row['id']} {row['user_name']} / {row['item_name']} / "
                f"{coin_text(row['price'])}\n"
                f"   상태: {status_text(row['status'])}{used_info}"
            )

        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/사용목록":
        rows = list_purchases(status="used")
        if not rows:
            reply(event.reply_token, "사용 완료된 상품이 없습니다.")
            return

        lines = ["✅ 사용 완료 목록", ""]
        for row in rows:
            lines.append(
                f"#{row['id']} {row['user_name']} / {row['item_name']}\n"
                f"   사용일: {row['used_at']} / 처리: {row['used_by'] or '-'}"
            )

        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/보유목록"):
        parts = text.split(maxsplit=1)

        if len(parts) == 1:
            rows = list_purchases(status="owned")
            title = "🎁 전체 보유 상품 목록"
        else:
            keyword = parts[1].strip()
            target = find_user(keyword)
            if not target:
                reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {keyword}")
                return
            rows = list_user_purchases(target["user_id"])
            title = f"🎁 {target['user_name']} 보유/사용 현황"

        if not rows:
            reply(event.reply_token, "보유 상품이 없습니다.")
            return

        lines = [title, ""]
        for row in rows:
            if "user_name" in row.keys():
                prefix = f"{row['user_name']} / "
            else:
                prefix = ""

            used_info = ""
            if row["status"] == "used":
                used_info = f"\n   사용일: {row['used_at']} / 처리: {row['used_by'] or '-'}"

            lines.append(
                f"#{row['id']} {prefix}{row['item_name']} / "
                f"{coin_text(row['price'])}\n"
                f"   상태: {status_text(row['status'])}{used_info}"
            )

        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/사용처리 "):
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            reply(event.reply_token, "사용법\n\n/사용처리 구매번호 메모")
            return

        note = parts[2] if len(parts) >= 3 else "운영진 사용 처리"
        success, msg = staff_use_purchase(int(parts[1]), user_name, note)
        reply(event.reply_token, msg)
        return

    if text.startswith("/구매취소 "):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            reply(event.reply_token, "사용법\n\n/구매취소 구매번호")
            return

        success, msg = cancel_purchase(int(parts[1]), user_name)
        reply(event.reply_token, msg)
        return

    # =========================
    # 초기화
    # =========================
    if text.startswith("/초기화"):
        target_date = parse_date(text)
        deleted = reset_date(target_date, COUNT_SOURCE_ID)
        reply(
            event.reply_token,
            f"🧹 날짜별 마디수 초기화 완료\n\n"
            f"날짜: {target_date}\n"
            f"삭제 데이터: {deleted}개"
        )
        return

    if text == "/전체초기화":
        reply(event.reply_token, "⚠️ 전체 마디수를 삭제하려면 아래처럼 입력하세요.\n\n/전체초기화 확인")
        return

    if text == "/전체초기화 확인":
        deleted = reset_all_counts()
        reply(event.reply_token, f"🧹 전체 마디수 초기화 완료\n\n삭제 데이터: {deleted}개\n멤버 목록은 유지됩니다.")
        return

    if text == "/멤버초기화":
        reply(event.reply_token, "⚠️ 전체 멤버 목록을 삭제하려면 아래처럼 입력하세요.\n\n/멤버초기화 확인")
        return

    if text == "/멤버초기화 확인":
        deleted = reset_all_users()
        reply(event.reply_token, f"👥 전체 멤버 초기화 완료\n\n삭제 인원: {deleted}명")
        return

    if text == "/화폐초기화":
        reply(event.reply_token, f"⚠️ 모든 {CURRENCY_NAME}과 화폐 내역을 삭제하려면 아래처럼 입력하세요.\n\n/화폐초기화 확인")
        return

    if text == "/화폐초기화 확인":
        deleted_currency, deleted_logs = reset_currency()
        reply(
            event.reply_token,
            f"💰 화폐 초기화 완료\n\n"
            f"삭제 잔액 데이터: {deleted_currency}개\n"
            f"삭제 내역 데이터: {deleted_logs}개"
        )
        return

    if text == "/완전초기화":
        reply(event.reply_token, "⚠️ 멤버, 마디수, 화폐, 구매내역을 전부 삭제하려면 아래처럼 입력하세요.\n\n/완전초기화 확인")
        return

    if text == "/완전초기화 확인":
        deleted = reset_everything()
        reply(
            event.reply_token,
            f"🔥 완전 초기화 완료\n\n"
            f"삭제 멤버: {deleted['users']}명\n"
            f"삭제 마디수: {deleted['counts']}개\n"
            f"삭제 화폐: {deleted['currency']}개\n"
            f"삭제 화폐내역: {deleted['currency_logs']}개\n"
            f"삭제 구매내역: {deleted['purchases']}개"
        )
        return

    if text.startswith("/닉삭제번호"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            reply(event.reply_token, "사용법\n\n/닉삭제번호 번호")
            return

        pending = DELETE_PENDING.get(user_id)
        if not pending or pending.get("mode") != "candidates":
            reply(event.reply_token, "삭제 후보가 없습니다. 먼저 /닉삭제 닉네임 으로 검색해주세요.")
            return

        index = int(parts[1]) - 1
        candidates = pending.get("candidates", [])
        if index < 0 or index >= len(candidates):
            reply(event.reply_token, f"번호가 올바르지 않습니다. 1~{len(candidates)}번 중에서 선택해주세요.")
            return

        target = candidates[index]
        DELETE_PENDING[user_id] = {"mode": "confirm", "target": target}
        reply(
            event.reply_token,
            "⚠ 정말 삭제하시겠습니까?\n\n"
            f"대상\n{target['user_name']}\n\n"
            "확인: /삭제확인\n"
            "취소: /삭제취소"
        )
        return

    if text == "/삭제취소":
        if user_id in DELETE_PENDING:
            DELETE_PENDING.pop(user_id, None)
            reply(event.reply_token, "삭제 요청을 취소했습니다.")
        else:
            reply(event.reply_token, "취소할 삭제 요청이 없습니다.")
        return

    if text == "/삭제확인":
        pending = DELETE_PENDING.get(user_id)
        if not pending or pending.get("mode") != "confirm":
            reply(event.reply_token, "확인할 삭제 요청이 없습니다. 먼저 /닉삭제 닉네임 으로 검색해주세요.")
            return

        target = pending["target"]
        DELETE_PENDING.pop(user_id, None)
        deleted_users, deleted_counts, deleted_names, deleted_detail = delete_users_by_ids({target["user_id"]: target["user_name"]})
        reply(event.reply_token, format_delete_done(target["user_name"], deleted_users, deleted_counts, deleted_names, deleted_detail))
        return

    if text.startswith("/닉삭제"):
        keyword = text.replace("/닉삭제", "", 1).strip()

        if not keyword:
            reply(event.reply_token, "사용법\n\n/닉삭제 닉네임")
            return

        candidates = find_delete_candidates(keyword)

        if not candidates:
            reply(event.reply_token, f"삭제 대상이 없습니다.\n\n검색어: {keyword}")
            return

        if len(candidates) > 1:
            DELETE_PENDING[user_id] = {"mode": "candidates", "keyword": keyword, "candidates": candidates}
            lines = [
                f"검색 결과가 여러 명입니다: @{keyword}",
                "",
            ]
            for i, row in enumerate(candidates, 1):
                lines.append(f"{i}. {row['user_name']}")
            lines.extend([
                "",
                "삭제하려면 아래처럼 입력해주세요.",
                "/닉삭제번호 1",
                "/닉삭제번호 2",
                "",
                "선택 후 /삭제확인 단계가 한 번 더 진행됩니다."
            ])
            reply(event.reply_token, "\n".join(lines))
            return

        target = candidates[0]
        DELETE_PENDING[user_id] = {"mode": "confirm", "target": target}
        reply(
            event.reply_token,
            "⚠ 정말 삭제하시겠습니까?\n\n"
            f"대상\n{target['user_name']}\n\n"
            "확인: /삭제확인\n"
            "취소: /삭제취소"
        )
        return




# =========================
# 입장 / 퇴장 이벤트
# =========================
if MemberLeftEvent is not None:
    @handler.add(MemberLeftEvent)
    def handle_member_left(event):
        try:
            source_id = get_source_id(event)

            for member in event.left.members:
                left_user_id = getattr(member, "user_id", None)

                if left_user_id:
                    set_user_active_by_id(left_user_id, 0)
                    print("MEMBER LEFT:", source_id, left_user_id)

        except Exception as e:
            print("MEMBER LEFT ERROR:", e)


if MemberJoinedEvent is not None:
    @handler.add(MemberJoinedEvent)
    def handle_member_joined(event):
        try:
            source_id = get_source_id(event)

            for member in event.joined.members:
                joined_user_id = getattr(member, "user_id", None)

                if joined_user_id:
                    # 닉네임은 첫 메시지 때 최신화되지만, 일단 재활성화
                    set_user_active_by_id(joined_user_id, 1)
                    print("MEMBER JOINED:", source_id, joined_user_id)

        except Exception as e:
            print("MEMBER JOINED ERROR:", e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
