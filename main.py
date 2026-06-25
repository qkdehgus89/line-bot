import os
import sqlite3
import random
import re
import threading
import time
import json
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

MALE_LIMIT = int(os.getenv("MALE_LIMIT", "10"))
FEMALE_LIMIT = int(os.getenv("FEMALE_LIMIT", "10"))
WARNING_LIMIT = int(os.getenv("WARNING_LIMIT", "10"))
CURRENCY_NAME = os.getenv("CURRENCY_NAME", "코인").strip()
BOT_VERSION = "sns-flowerbot-v10.6"
BOT_USER_ID = os.getenv("BOT_USER_ID", "").strip()

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

# 완전삭제 다중 검색/확인 임시 저장소
# key: 운영진 user_id / value: {mode, candidates|target}
HARD_DELETE_PENDING = {}

# 족보 입력 대기 저장소
# /족보입력 만 입력한 뒤 다음 메시지 전체를 족보로 저장
JOKBO_PENDING = {}


# =========================
# 권한
# =========================
def is_admin(user_id):
    return user_id in ADMIN_USER_IDS


def is_staff(user_id):
    return user_id in ADMIN_USER_IDS or user_id in OPERATOR_USER_IDS


def is_operator_command(text):
    """
    운영진 전용 명령어를 일반 유저가 입력했을 때
    기능별 다른 문구 대신 동일한 경고 문구를 출력하기 위한 통합 체크.
    """
    if not text:
        return False

    exact_commands = {
        "/운영명령어", "/방정보", "/DB상태", "/수집상태", "/최근로그", "/수집누락", "/전체유저",
        "/족보입력", "/족보", "/경고", "/완전삭제",
        "/삭제유저", "/경제현황", "/럭키정산", "/럭키초기화", "/럭키현황전체",
        "/럭키드로우", "/럭키드로우구매", "/럭키드로우현황", "/럭키드로우결과",
        "/가챠", "/가챠시스템", "/가챠횟수", "/상가챠", "/중가챠", "/하가챠",
        "/조각가챠", "/조각", "/대장장이", "/김미트상가챠", "/상점",
        "/회생초기화",
        "/설렘픽초기화", "/설렘픽정산", "/조각정리", "/경고누적일", "/단벙참여확인", "/단벙참석확인",
        "/유저아이템보유", "/유저아이템삭제", "/운영진친밀도", "/운영진친밀도확인",
        "/진실질문", "/진실목록", "/진실기록", "/진실질문추가",
        "/코인검증", "/정산검증", "/최근오류", "/버전",
    }

    prefix_commands = [
        "/유저검색 ", "/유저상세 ", "/닉삭제", "/닉삭제번호",
        "/지급 ", "/차감 ", "/코인내역 ", "/삭제복구",
        "/구매 ", "/가챠 ",
        "/회생초기화 ",
        "/상품추가 ", "/상품등록 ", "/상품삭제 ",
        "/사용처리 ", "/구매취소 ", "/아이템지급 ",
        "/유저아이템삭제 ",
        "/마디수 ", "/경고누적일 ", "/단벙참여확인 ", "/단벙참석확인 ",
        "/운영진친밀도 ", "/운영진친밀도확인 ",
        "/진실질문 ", "/진실기록 ", "/진실질문추가 ",
        "/코인검증 ", "/최근오류 ",
    ]

    return text in exact_commands or any(text.startswith(prefix) for prefix in prefix_commands)


def operator_only_warning():
    return "이 명령어는 운영진만 사용할 수 있어요."


def count_source_ids():
    ids = set()
    if COUNT_SOURCE_ID:
        ids.add(COUNT_SOURCE_ID)

    # 운영진방 여러 개 카운트 지원
    for admin_source_id in ADMIN_SOURCE_IDS:
        ids.add(admin_source_id)

    return ids



# 마니또 설정
MANITTO_REQUIRED_SCORE = 15
MANITTO_REROLL_LIMIT = 2
MANITTO_GOLD_RATE = 0.10
MANITTO_MIN_TARGET_BALANCE = 20  # 2코인
MANITTO_ACTIVE_DAYS = 7
MANITTO_NORMAL_REWARD_MIN = 15   # 1.5코인
MANITTO_NORMAL_REWARD_MAX = 60   # 6코인
MANITTO_GOLD_REWARD_MIN = 60     # 6코인
MANITTO_GOLD_REWARD_MAX = 150    # 15코인

# =========================
# 시간
# =========================
def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def parse_date_arg(text_value):
    value = (text_value or "").strip()
    if not value:
        return today(), None
    if value in ("오늘", "today"):
        return today(), None
    if value in ("어제", "yesterday"):
        return (datetime.now(KST).date() - timedelta(days=1)).strftime("%Y-%m-%d"), None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value, None
    except ValueError:
        return None, "날짜는 YYYY-MM-DD 형식으로 입력해주세요.\n예: /마디수 2026-06-19"


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


def log_error(context, error):
    detail = repr(error)
    print(f"{context}:", detail)
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO bot_errors (context, detail, created_at)
        VALUES (?, ?, ?)
        """, (str(context), detail[:1800], now_str()))
        conn.commit()
        conn.close()
    except Exception as e:
        print("BOT_ERROR_LOG_WRITE_ERROR:", repr(e))


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
    CREATE TABLE IF NOT EXISTS mention_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        week_start TEXT NOT NULL,
        source_id TEXT NOT NULL,
        sender_user_id TEXT NOT NULL,
        sender_user_name TEXT NOT NULL,
        target_user_id TEXT NOT NULL,
        target_user_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS anonymous_pokes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        week_start TEXT NOT NULL,
        sender_user_id TEXT NOT NULL,
        sender_user_name TEXT NOT NULL,
        target_user_id TEXT NOT NULL,
        target_user_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS heart_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        week_start TEXT NOT NULL,
        sender_user_id TEXT NOT NULL,
        sender_user_name TEXT NOT NULL,
        target_user_id TEXT NOT NULL,
        target_user_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS heart_pick_rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        rank INTEGER NOT NULL,
        pick_count INTEGER NOT NULL,
        reward INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(week_start, rank)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chemistry_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        week_start TEXT NOT NULL,
        sender_user_id TEXT NOT NULL,
        sender_user_name TEXT NOT NULL,
        target_user_id TEXT NOT NULL,
        target_user_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chemistry_rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        week_start TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        matched_user_id TEXT NOT NULL,
        matched_user_name TEXT NOT NULL,
        reward INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(date, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS public_announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        category TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        release_after_log_id INTEGER,
        delivered_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS truth_game_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        requester_user_id TEXT,
        requester_user_name TEXT,
        question TEXT NOT NULL,
        category TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        cost INTEGER NOT NULL DEFAULT 2,
        created_at TEXT NOT NULL,
        answered_at TEXT,
        answer_text TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS truth_game_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        question TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_by_name TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(category, question)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS truth_game_resets (
        user_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL,
        reset_at TEXT NOT NULL,
        reset_count INTEGER NOT NULL DEFAULT 0,
        reset_after_session_id INTEGER NOT NULL DEFAULT 0
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
    CREATE TABLE IF NOT EXISTS revival_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        reward INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bot_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        context TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settlement_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(date, week_start, week_end)
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
    CREATE TABLE IF NOT EXISTS attendance_streak_rewards (
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        streak_days INTEGER NOT NULL,
        reward INTEGER NOT NULL,
        achieved_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, streak_days)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS danbung_attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        event_name TEXT,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        cost INTEGER NOT NULL DEFAULT 10,
        created_at TEXT NOT NULL
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
    CREATE TABLE IF NOT EXISTS gacha_weekly_counts (
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (week_start, user_id)
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
    CREATE TABLE IF NOT EXISTS affinity_cumulative_scores (
        user_a TEXT NOT NULL,
        user_b TEXT NOT NULL,
        user_a_name TEXT NOT NULL,
        user_b_name TEXT NOT NULL,
        total_score INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_a, user_b)
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
        required_score INTEGER NOT NULL DEFAULT 30,
        reward_min INTEGER NOT NULL DEFAULT 15,
        reward_max INTEGER NOT NULL DEFAULT 75,
        reward INTEGER,
        manitto_type TEXT NOT NULL DEFAULT 'normal',
        completed INTEGER NOT NULL DEFAULT 0,
        reroll_count INTEGER NOT NULL DEFAULT 0,
        reroll_history TEXT,
        previous_target_ids TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY (week_start, hunter_user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS genealogy_text (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        content TEXT NOT NULL,
        updated_by TEXT,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_titles (
        user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        title TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
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

    cur.execute("PRAGMA table_info(manitto_assignments)")
    manitto_cols = {row["name"] for row in cur.fetchall()}

    for col, col_type, default_value in [
        ("reroll_count", "INTEGER", "0"),
        ("reroll_history", "TEXT", "NULL"),
        ("previous_target_ids", "TEXT", "NULL"),
    ]:
        if col not in manitto_cols:
            cur.execute(f"ALTER TABLE manitto_assignments ADD COLUMN {col} {col_type} DEFAULT {default_value}")

    cur.execute("PRAGMA table_info(public_announcements)")
    public_announcement_cols = {row["name"] for row in cur.fetchall()}

    if "release_after_log_id" not in public_announcement_cols:
        cur.execute("ALTER TABLE public_announcements ADD COLUMN release_after_log_id INTEGER")

    cur.execute("PRAGMA table_info(danbung_attendance)")
    danbung_attendance_cols = {row["name"] for row in cur.fetchall()}

    if "event_name" not in danbung_attendance_cols:
        cur.execute("ALTER TABLE danbung_attendance ADD COLUMN event_name TEXT")

    cur.execute("PRAGMA table_info(truth_game_sessions)")
    truth_game_cols = {row["name"] for row in cur.fetchall()}

    for col, col_type in [
        ("requester_user_id", "TEXT"),
        ("requester_user_name", "TEXT"),
    ]:
        if col not in truth_game_cols:
            cur.execute(f"ALTER TABLE truth_game_sessions ADD COLUMN {col} {col_type}")

    cur.execute("PRAGMA table_info(truth_game_resets)")
    truth_game_reset_cols = {row["name"] for row in cur.fetchall()}
    if "reset_after_session_id" not in truth_game_reset_cols:
        cur.execute("ALTER TABLE truth_game_resets ADD COLUMN reset_after_session_id INTEGER NOT NULL DEFAULT 0")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS deleted_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_user_id TEXT NOT NULL,
        user_name TEXT NOT NULL,
        deleted_by TEXT,
        deleted_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sns_lucky_draw_prizes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        rank INTEGER NOT NULL,
        winner_user_id TEXT NOT NULL,
        winner_user_name TEXT NOT NULL,
        prize INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    DELETE FROM heart_picks
    WHERE id NOT IN (
        SELECT MIN(id)
        FROM heart_picks
        GROUP BY date, sender_user_id
    )
    """)

    cur.execute("""
    DELETE FROM chemistry_signals
    WHERE id NOT IN (
        SELECT MIN(id)
        FROM chemistry_signals
        GROUP BY date, sender_user_id
    )
    """)

    cur.execute("""
    DELETE FROM danbung_attendance
    WHERE id NOT IN (
        SELECT MIN(id)
        FROM danbung_attendance
        GROUP BY date, COALESCE(event_name, ''), user_id
    )
    """)

    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_heart_picks_daily_sender
    ON heart_picks (date, sender_user_id)
    """)

    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_chemistry_signals_daily_sender
    ON chemistry_signals (date, sender_user_id)
    """)

    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_danbung_attendance_daily_event_user
    ON danbung_attendance (date, COALESCE(event_name, ''), user_id)
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_revival_claims_daily_user
    ON revival_claims (date, user_id)
    """)


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

    cur.execute("SELECT value FROM system_flags WHERE key = 'revival_balance_limit_reset_v1'")
    revival_reset_done = cur.fetchone()
    if not revival_reset_done:
        cur.execute("DELETE FROM revival_claims WHERE date = ?", (today(),))
        cur.execute(
            "INSERT INTO system_flags (key, value) VALUES ('revival_balance_limit_reset_v1', ?)",
            (now_str(),)
        )

    cur.execute("SELECT value FROM system_flags WHERE key = 'attendance_day3_reset_v1'")
    attendance_reset_done = cur.fetchone()
    if not attendance_reset_done:
        base_date = datetime.strptime(today(), "%Y-%m-%d").date()
        seed_dates = [
            (base_date - timedelta(days=2)).strftime("%Y-%m-%d"),
            (base_date - timedelta(days=1)).strftime("%Y-%m-%d"),
        ]
        created_at = now_str()

        cur.execute("DELETE FROM attendance")
        cur.execute("DELETE FROM attendance_streak_rewards")
        cur.execute("DELETE FROM hidden_rewards WHERE mission_key LIKE 'attendance_streak_%'")
        cur.execute("""
        SELECT user_id, user_name
        FROM users
        WHERE COALESCE(is_active, 1) = 1
          AND user_id IS NOT NULL
          AND TRIM(user_id) != ''
        """)
        active_users = cur.fetchall()
        for row in active_users:
            for seed_date in seed_dates:
                cur.execute("""
                INSERT OR IGNORE INTO attendance (
                    date, user_id, user_name, reward, created_at
                ) VALUES (?, ?, ?, 0, ?)
                """, (seed_date, row["user_id"], row["user_name"], created_at))

        cur.execute(
            "INSERT INTO system_flags (key, value) VALUES ('attendance_day3_reset_v1', ?)",
            (created_at,)
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


def split_text_messages(text, max_chars=4500, max_messages=5):
    """
    LINE reply는 최대 5개 메시지까지 보낼 수 있어서,
    긴 족보는 줄 단위로 나눠 전송한다.
    """
    text = str(text or "")
    if not text:
        return [""]

    lines = text.split("\n")
    chunks = []
    current = ""

    for line in lines:
        add = line if not current else "\n" + line
        if len(current) + len(add) > max_chars:
            if current:
                chunks.append(current)
            current = line
            if len(chunks) >= max_messages:
                break
        else:
            current += add

    if current and len(chunks) < max_messages:
        chunks.append(current)

    original = "\n".join(lines)
    shown = "\n".join(chunks)
    if len(chunks) >= max_messages and len(original) > len(shown):
        chunks[-1] += "\n\n...\n족보가 길어서 일부만 표시됐습니다."

    return chunks[:max_messages]


def reply_many(reply_token, texts):
    messages = [TextMessage(text=str(t)[:4900]) for t in texts if str(t).strip()]
    if not messages:
        messages = [TextMessage(text="내용이 없습니다.")]

    with ApiClient(config) as client:
        api = MessagingApi(client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages[:5]
            )
        )




# =========================
# v61 안정화 호환 함수
# =========================
def affinity_ranking_text(limit=10):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_a, user_b, user_a_name, user_b_name, score
    FROM affinity_scores
    WHERE week_start = ?
    ORDER BY score DESC, updated_at DESC
    LIMIT ?
    """, (week_start, limit))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return "💞 이번 주 친밀도 랭킹 데이터가 없습니다."

    lines = ["💞 이번 주 친밀도 랭킹", f"기간: {week_start} ~ {week_end}", ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['user_a_name']} ↔ {row['user_b_name']} - {row['score']}")
    return "\n".join(lines)


def manitto_admin_status_text():
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT hunter_user_name, target_user_name, required_score, reward, completed, reroll_count
    FROM manitto_assignments
    WHERE week_start = ?
    ORDER BY completed ASC, hunter_user_name ASC
    """, (week_start,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return "🎭 이번 주 마니또 배정 데이터가 없습니다."

    lines = ["🎭 이번 주 마니또 현황", f"기간: {week_start} ~ {week_end}", ""]
    for row in rows:
        status = "완료" if int(row['completed'] or 0) == 1 else "진행중"
        reward = int(row['reward'] or 0)
        lines.append(
            f"- {row['hunter_user_name']} → {row['target_user_name']} / {status} / "
            f"필요 {row['required_score']} / 보상 {coin_text(reward)} / 변경 {int(row['reroll_count'] or 0)}/{MANITTO_REROLL_LIMIT}"
        )
    return "\n".join(lines)


def weekly_settlement_text(source_id=None):
    """주간정산 실행 후 운영진에게 보여줄 문구를 반환합니다."""
    source_id = source_id or COUNT_SOURCE_ID
    week_start, week_end = week_range_for_today()
    paid = settle_weekly_rewards(source_id, week_start, week_end)
    heart_paid = []
    heart_error = None
    if "settle_heart_pick_rewards" in globals():
        try:
            heart_paid = settle_heart_pick_rewards(week_start, week_end)
        except Exception as e:
            heart_error = repr(e)
            log_error("WEEKLY_HEART_PICK_SETTLEMENT_ERROR", e)

    if not paid and not heart_paid and not heart_error:
        return (
            "🏆 주간정산\n\n"
            f"기간: {week_start} ~ {week_end}\n"
            "새로 지급할 주간 보상이나 설렘픽 보상이 없습니다.\n"
            "이미 정산했거나 랭킹 데이터가 없습니다."
        )

    lines = ["🏆 주간정산 완료", f"기간: {week_start} ~ {week_end}", ""]
    if paid:
        lines.append("마디수 랭킹")
        for item in paid:
            lines.append(f"{item['rank']}위 {item['user_name']} - {item['count']}마디 / {coin_text(item['reward'])}")
    if heart_paid:
        if paid:
            lines.append("")
        lines.append("설렘픽 랭킹")
        for item in heart_paid:
            lines.append(f"{item['rank']}위 {item['user_name']} - {item['pick_count']}표 / {coin_text(item['reward'])}")
    if heart_error:
        if paid or heart_paid:
            lines.append("")
        lines.append("설렘픽 정산 오류가 발생했습니다. 로그를 확인해주세요.")
    return "\n".join(lines)


# =========================
# 안정화 헬퍼
# =========================
def is_private_chat(event):
    """
    LINE 1:1 채팅 판별.
    group_id/room_id가 없고 user_id가 있으면 1:1로 판단합니다.
    """
    source = getattr(event, "source", None)
    if source is None:
        return False

    source_type = str(getattr(source, "type", "") or "").lower()
    if source_type == "user" or source_type.endswith(".user"):
        return True

    if getattr(source, "group_id", None) or getattr(source, "room_id", None):
        return False

    source_user_id = getattr(source, "user_id", None)
    return bool(source_user_id and str(source_user_id).strip() not in ("", "NO_USER_ID", "None"))


def one_to_one_command_notice(feature_name="해당 기능", command_hint=None):
    lines = [
        f"{feature_name} 안내",
        "",
        "이 기능은 꽃봇 1:1 채팅에서 이용해 주세요.",
    ]
    if command_hint:
        lines += ["", f"1:1에서 이렇게 입력하면 돼요: {command_hint}"]
    return "\n".join(lines)


def queue_public_announcement(source_id, text_value, category="general", release_after_log_id=None):
    source_id = str(source_id or "").strip()
    if not source_id:
        return False

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO public_announcements (
            source_id, category, message, created_at, release_after_log_id
        ) VALUES (?, ?, ?, ?, ?)
        """, (source_id, category, str(text_value), now_str(), release_after_log_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print("PUBLIC_ANNOUNCEMENT_QUEUE_ERROR:", repr(e))
        return False


def pop_public_announcements(source_id, current_log_id=None, limit=5):
    source_id = str(source_id or "").strip()
    if not source_id:
        return []

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT id, message
        FROM public_announcements
        WHERE source_id = ?
          AND delivered_at IS NULL
          AND (
              release_after_log_id IS NULL
              OR release_after_log_id <= ?
          )
        ORDER BY id ASC
        LIMIT ?
        """, (source_id, int(current_log_id or 0), limit))
        rows = cur.fetchall()
        if not rows:
            conn.close()
            return []

        ids = [row["id"] for row in rows]
        cur.execute(
            f"UPDATE public_announcements SET delivered_at = ? WHERE id IN ({','.join('?' for _ in ids)})",
            [now_str(), *ids]
        )
        conn.commit()
        conn.close()
        return [row["message"] for row in rows]
    except Exception as e:
        print("PUBLIC_ANNOUNCEMENT_POP_ERROR:", repr(e))
        return []


def push_or_reply_private_info(
    event,
    user_id,
    text_value,
    public_notice="📩 개인 메시지로 전송했습니다.",
    command_hint=None,
    allow_admin_room=False
):
    """
    1:1 채팅에서는 현재 대화에 바로 reply.
    공개방/그룹/룸에서는 Push를 쓰지 않고 1:1 직접 입력을 안내.
    allow_admin_room=True이면 운영방에서는 현재 대화에 바로 reply.
    """
    if is_private_chat(event) or (allow_admin_room and get_source_id(event) in ADMIN_SOURCE_IDS):
        reply_many(event.reply_token, split_text_messages(text_value))
        return

    reply(event.reply_token, one_to_one_command_notice("개인 정보 기능", command_hint))


def simplified_command_text(text):
    """
    짧은 별칭을 기존 명령어로 연결합니다.
    기존 긴 명령어는 그대로 유지하고, 입력만 가볍게 받기 위한 변환입니다.
    """
    text = (text or "").strip()
    if not text.startswith("/"):
        return text

    parts = text.split()
    command = parts[0]
    args = parts[1:]
    rest = " ".join(args).strip()

    if command == "/내정보":
        if not args:
            return "/내정보"
        sub = args[0]
        tail = " ".join(args[1:]).strip()
        if sub in ("보유", "아이템", "상품"):
            if tail in ("미사용", "사용"):
                return f"/내보유 {tail}"
            return "/내보유"
        if sub in ("미사용", "사용"):
            return f"/내보유 {sub}"
        if sub in ("업적", "칭호"):
            return "/업적"
        if sub in ("코인", "잔액"):
            return "/잔액"
        if sub in ("내역", "코인내역"):
            return "/코인내역"
        return "/내정보"

    if command == "/랭킹":
        if not args or args[0] in ("오늘", "마디수", "일간"):
            return "/마디수"
        sub = args[0]
        if sub in ("주간", "이번주"):
            return "/주간랭킹"
        if sub in ("전체", "누적"):
            return "/전체순위"
        if sub in ("코인", "잔액"):
            return "/코인랭킹"
        if sub in ("친밀도",):
            return "/친밀도랭킹"
        if sub in ("인기", "인기인"):
            return "/인기인"
        if sub in ("설렘", "설렘픽"):
            return "/설렘픽랭킹"
        return "/마디수"

    if command == "/가챠":
        if not args:
            return text
        sub = args[0]
        if sub in ("상", "상급"):
            return "/상가챠"
        if sub in ("중", "중급"):
            return "/중가챠"
        if sub in ("하", "하급"):
            return "/하가챠"
        if sub in ("조각", "조각가챠"):
            return "/조각가챠"
        if sub in ("횟수", "사용횟수"):
            return "/가챠횟수"
        if sub in ("시스템", "안내", "설명"):
            return "/가챠시스템"
        if sub in ("조각확인", "조각보유", "조각보기"):
            return "/조각"
        if sub in ("대장장이", "교환"):
            return "/대장장이"
        return text

    if command == "/설렘":
        if not args:
            return "/설렘픽"
        sub = args[0]
        if sub in ("랭킹", "순위"):
            return "/설렘픽랭킹"
        if sub in ("현황", "확인"):
            return "/설렘픽현황"
        return "/설렘픽 " + rest

    if command == "/케미":
        if args and args[0] in ("확인", "현황"):
            return "/케미확인"
        return text

    if command == "/진실":
        if not args:
            return "/진실게임"
        sub = args[0]
        tail = " ".join(args[1:]).strip()
        if sub in ("취소", "취소하기"):
            return ("/진실취소 " + tail).strip()
        if sub in ("초기화", "리셋"):
            return "/진실게임초기화"
        if sub in ("답변",):
            return ("/진실답변 " + tail).strip()
        if sub in ("패스", "넘기기"):
            return "/진실패스"
        if sub in ("목록",):
            return "/진실목록"
        if sub in ("기록",):
            return ("/진실기록 " + tail).strip()
        if sub in ("질문추가",):
            return ("/진실질문추가 " + tail).strip()
        return "/진실게임 " + rest

    if command == "/답변":
        return ("/진실답변 " + rest).strip()

    if command == "/패스":
        return "/진실패스"

    if command == "/운영":
        if not args:
            return "/운영명령어"
        sub = args[0]
        tail = " ".join(args[1:]).strip()
        operator_aliases = {
            "명령어": "/운영명령어",
            "도움말": "/운영명령어",
            "지급": "/지급",
            "차감": "/차감",
            "코인내역": "/코인내역",
            "코인검증": "/코인검증",
            "경제현황": "/경제현황",
            "유저검색": "/유저검색",
            "유저상세": "/유저상세",
            "아이템지급": "/아이템지급",
            "아이템보유": "/유저아이템보유",
            "유저아이템보유": "/유저아이템보유",
            "아이템삭제": "/유저아이템삭제",
            "유저아이템삭제": "/유저아이템삭제",
            "정산검증": "/정산검증",
            "정산": "/정산검증",
            "오류": "/최근오류",
            "최근오류": "/최근오류",
            "DB상태": "/DB상태",
            "디비상태": "/DB상태",
            "수집상태": "/수집상태",
            "최근로그": "/최근로그",
            "수집누락": "/수집누락",
            "경고": "/경고",
            "경고누적일": "/경고누적일",
            "마디수": "/마디수",
            "단벙참여확인": "/단벙참여확인",
            "설렘픽정산": "/설렘픽정산",
            "설렘픽초기화": "/설렘픽초기화",
            "럭키정산": "/럭키정산",
            "럭키초기화": "/럭키초기화",
            "럭키현황": "/럭키현황전체",
            "회생초기화": "/회생초기화",
            "전체유저": "/전체유저",
            "방정보": "/방정보",
            "버전": "/버전",
        }
        mapped = operator_aliases.get(sub)
        if not mapped:
            return text
        return (mapped + (" " + tail if tail else "")).strip()

    return text


def user_summary_text(user_id, user_name):
    try:
        rows = list_user_purchases(user_id, limit=None)
        owned = len([row for row in rows if row["status"] in ("owned", "pending")])
        used = len([row for row in rows if row["status"] in ("used", "done")])
        best_name, best_score = get_best_affinity(user_id)
        best_line = f"{best_name} ({best_score})" if best_name else "기록 없음"

        return "\n".join([
            "👤 내정보",
            "",
            f"대상: {user_name}",
            f"💰 보유 코인: {coin_text(get_balance(user_id))}",
            f"🎁 미사용 아이템: {owned}개",
            f"📦 사용완료 아이템: {used}개",
            f"📅 출석: {get_attendance_count(user_id)}일",
            f"🏆 업적: {get_achievement_count(user_id)}개",
            f"💕 최고 친밀도: {best_line}",
            "",
            "자세히 보기",
            "/내정보 보유",
            "/내정보 업적",
            "/내정보 코인",
        ])
    except Exception as e:
        log_error("USER_SUMMARY_ERROR", e)
        return "👤 내정보를 불러오는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def user_commands_text():
    return """🤖 S.N.S 꽃봇 명령어

━━━━━━━━━━
📖 정보
━━━━━━━━━━
/명령어
/가이드
/내정보

━━━━━━━━━━
🎯 활동
━━━━━━━━━━
/출석
/미션
/수령
/회생
/단벙
/단벙참여 단벙제목
/랭킹
/랭킹 주간
/랭킹 전체
/주사위

━━━━━━━━━━
💰 재화
━━━━━━━━━━
/내정보
/내정보 보유
/내정보 업적
/내정보 코인
/랭킹 코인
/코인내역

━━━━━━━━━━
🎭 마니또
━━━━━━━━━━
/마니또
/마니또확인
/마니또변경
/마니또보상

━━━━━━━━━━
❤️ 친밀도
━━━━━━━━━━
/친밀도랭킹

━━━━━━━━━━
👑 인기인
━━━━━━━━━━
/인기인
/오늘인기인
/주간인기인
/언급랭킹 닉네임

━━━━━━━━━━
💘 설렘픽
━━━━━━━━━━
/설렘 닉네임 (1:1)
/설렘 현황
/설렘 랭킹

━━━━━━━━━━
💞 케미
━━━━━━━━━━
/케미 닉네임 (1:1)
/케미 확인 (1:1)

━━━━━━━━━━
🎭 진실게임
━━━━━━━━━━
/진실
/진실 순한맛 닉네임
/답변 내용
/패스
/진실 취소
/진실 초기화

━━━━━━━━━━
🏆 업적
━━━━━━━━━━
/업적

※ 기존 긴 명령어도 그대로 사용할 수 있습니다."""

def beginner_guide_text():
    return """📖 S.N.S 가이드

환영합니다 😀

1️⃣ 공지사항을 먼저 읽어주세요.

2️⃣ 입장 인사를 작성해주세요.

3️⃣ 초대 게시판(족보)에 댓글을 작성해주세요.

4️⃣ 꽃봇을 친구추가 해주세요.
(미추가 시 일부 기능 사용 불가)

5️⃣ /명령어 를 입력하여 기능을 확인해주세요.

6️⃣ /미션 을 확인하고 /수령 으로 코인을 획득할 수 있습니다.

7️⃣ /내정보 로 보유 코인과 아이템을 확인할 수 있습니다.

8️⃣ 보유 코인이 10코인 미만일 때는 /회생 으로 하루 5회까지 10코인씩 복구할 수 있습니다.

9️⃣ /내정보 보유 로 보유 아이템을 확인할 수 있습니다.

🔟 /마니또 와 친밀도 시스템을 통해 추가 보상을 획득할 수 있습니다.

━━━━━━━━━━

📖 S.N.S 이모티콘 안내

🪩 방장
🔗 부방장
⚖️ 관리자

━━━━━━━━━━

🏁 인증자

🔹 남미클자
🔸 여미클자
🔰 노미클자

💊 STD 검사 완료
💉 피검사

👾 외출
🛸 바쁨

⚠️ 경고
🚫 벙금지

━━━━━━━━━━

💰 코인

💠 무제한단벙주최권
🛟 미션클리어권
📸 봇등록권
🔤 칭호권
🎫 닉변권
🎟 임티권

━━━━━━━━━━

🎁 추천 명령어

/명령어
/미션
/수령
/회생
/내정보
/마니또

━━━━━━━━━━

💘 설렘픽 / 케미

/설렘 닉네임
- 하루 1회 이성에게 익명 투표할 수 있습니다.
- 설렘픽 랭킹 1~3등은 주간 정산 보상을 받습니다.

/케미 닉네임
- 하루 1회 이성에게 케미를 보낼 수 있습니다.
- 서로 케미를 보낸 경우에만 공창에 익명 알림이 표시됩니다.
- 매칭에 성공하면 본인 1:1창에만 성공 안내가 표시됩니다.
- 최초 케미 성공 보상은 1코인, 이후 성공 보상은 0.2코인입니다.

/케미 확인
- 오늘 내가 보낸 케미의 매칭 성공 여부와 나에게 온 요청 수를 확인할 수 있습니다.

※ 노미클은 남자로 간주합니다.
※ 성별은 닉네임 인증 이모티콘과 저장된 족보를 기준으로 확인합니다.

좋은 인연과 즐거운 대화를 만들어보세요 😀"""

def operator_commands_text():
    return """🔒 운영진 전용 명령어

━━━━━━━━━━
⚡ 간단 명령어
━━━━━━━━━━
/운영 지급 닉네임 금액
/운영 차감 닉네임 금액
/운영 코인검증 닉네임
/운영 정산검증
/운영 오류
/운영 아이템보유

※ 기존 운영 명령어도 그대로 사용할 수 있습니다.

━━━━━━━━━━
💰 재화
━━━━━━━━━━
/지급 닉네임 금액
/차감 닉네임 금액
/코인내역 닉네임
/코인검증 닉네임
/경제현황
/회생초기화
/회생초기화 YYYY-MM-DD
/회생초기화 전체

━━━━━━━━━━
👤 유저 관리
━━━━━━━━━━
/전체유저
/유저검색 닉네임
/유저상세 닉네임
/닉삭제 닉네임
/닉삭제번호 번호
/완전삭제
/삭제유저
/삭제복구 번호

━━━━━━━━━━
📖 족보
━━━━━━━━━━
/족보입력
/족보

━━━━━━━━━━
🛒 상점/아이템 관리
━━━━━━━━━━
/상점
/구매 상품명
/상품등록 상품명 가격 설명
/상품추가 상품명 가격 설명
/상품삭제 상품명
/아이템지급 닉네임 상품명
/유저아이템보유
/유저아이템삭제 닉네임 아이템명 개수
/사용 구매번호
/사용처리 구매번호
/구매취소 구매번호

━━━━━━━━━━
🎰 가챠
━━━━━━━━━━
/가챠
/가챠 상
/가챠 중
/가챠 하
/가챠 조각
/가챠 횟수
/가챠 대장장이

━━━━━━━━━━
🎟 럭키드로우
━━━━━━━━━━
/럭키드로우
/럭키드로우구매
/럭키드로우현황
/럭키드로우결과
/럭키정산
/럭키초기화
/럭키현황전체

━━━━━━━━━━
👀 이벤트 관리
━━━━━━━━━━
/설렘픽초기화
/설렘픽정산
/운영진친밀도
/운영진친밀도 닉네임
/운영진친밀도확인
/운영진친밀도확인 닉네임

━━━━━━━━━━
🎭 진실게임 관리
━━━━━━━━━━
/진실질문 난이도 닉네임
/진실질문추가 난이도 질문내용
/진실목록
/진실기록 닉네임

━━━━━━━━━━
⚙️ 시스템
━━━━━━━━━━
/방정보
/DB상태
/수집상태
/최근로그
/수집누락
/정산검증
/최근오류
/경고
/경고누적일
/경고누적일 최소횟수
/마디수 YYYY-MM-DD
/단벙참여확인
/단벙참여확인 단벙제목
/단벙참여확인 YYYY-MM-DD
/조각정리
/버전"""

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


def remove_nickname_bracket_text(text_value):
    text_value = str(text_value or "")
    return re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|<[^>]*>|【[^】]*】|［[^］]*］|（[^）]*）", "", text_value)


def nickname_tokens(text_value):
    text_value = remove_nickname_bracket_text(text_value)
    tokens = []
    for token in re.findall(r"[0-9A-Za-z가-힣]+", text_value):
        token = re.sub(r"^\d+", "", token).strip().lower()
        if token:
            tokens.append(token)
    return tokens


def normalize_mention_name(text_value):
    """
    장식 이모지/앞 숫자/꼬리표를 제거하고 실제로 부르는 핵심 닉네임만 뽑습니다.
    예: 🪩미트🪩 -> 미트, 33무화🔸💉 -> 무화, ⚖️무화⚖️💉[밍구전용봊] -> 무화
    """
    tokens = nickname_tokens(text_value)
    if tokens:
        return tokens[0]
    return clean_keyword(remove_nickname_bracket_text(text_value))


def normalize_match_text(text_value):
    return clean_keyword(remove_nickname_bracket_text(text_value))


def display_nickname(user_name):
    return normalize_mention_name(user_name) or str(user_name or "").strip()


def gender_name_keys(user_name):
    keys = set()
    for value in [
        clean_keyword(user_name),
        normalize_mention_name(user_name),
        normalize_match_text(user_name),
    ]:
        value = str(value or "").strip()
        if not value:
            continue
        keys.add(value)
        without_age = re.sub(r"^\d+", "", value)
        if without_age:
            keys.add(without_age)
    return keys


def gender_from_text_markers(text_value):
    text_value = str(text_value or "")
    clean = clean_keyword(text_value)
    tokens = [token.lower() for token in re.findall(r"[0-9A-Za-z가-힣]+", text_value)]

    # 노미클은 남자로 간주합니다.
    if "🔰" in text_value or "노미클" in clean:
        return "male"

    if "🔸" in text_value or "🔻" in text_value or "여미클" in clean or "여자" in clean or "여성" in clean or "여" in tokens:
        return "female"

    if "🔹" in text_value or "남미클" in clean or "남자" in clean or "남성" in clean or "남" in tokens:
        return "male"

    return None


def gender_from_user_row(user_row):
    if not user_row:
        return None

    try:
        if int(user_row["is_nomicl"] or 0) == 1:
            return "male"
    except Exception:
        pass

    try:
        gender = str(user_row["gender"] or "").strip().lower()
    except Exception:
        gender = ""

    if gender in ("male", "m", "man", "남", "남자", "남성", "nomicl", "노미클"):
        return "male"
    if gender in ("female", "f", "woman", "여", "여자", "여성"):
        return "female"

    return None


def gender_from_genealogy(user_name):
    try:
        target_keys = gender_name_keys(user_name)
        if not target_keys:
            return None

        content = normalize_genealogy_content(get_genealogy_content())
        if not content:
            return None

        for line in content.splitlines():
            first_key = genealogy_first_member_key(line)
            if not first_key:
                continue
            line_keys = gender_name_keys(first_key)
            if not target_keys.intersection(line_keys):
                continue

            gender = gender_from_text_markers(line)
            if gender:
                return gender
    except Exception as e:
        print("GENEALOGY_GENDER_ERROR:", repr(e))
    return None


def effective_user_gender(user_row, fallback_name=None):
    user_name = fallback_name
    if user_row:
        try:
            user_name = user_row["user_name"] or user_name
        except Exception:
            pass

    return (
        gender_from_text_markers(user_name)
        or gender_from_user_row(user_row)
        or gender_from_genealogy(user_name)
    )


def gender_label(gender):
    if gender == "male":
        return "남성"
    if gender == "female":
        return "여성"
    return "확인불가"


def opposite_gender_check(sender_user_id, sender_user_name, target):
    sender_row = get_user_by_id(sender_user_id)
    sender = dict(sender_row) if sender_row else {"user_id": sender_user_id, "user_name": sender_user_name}
    if sender_user_name:
        sender["user_name"] = sender_user_name

    sender_gender = effective_user_gender(sender, sender_user_name)
    target_gender = effective_user_gender(target, target.get("user_name") if target else None)

    if not sender_gender or not target_gender:
        return False, (
            "성별을 확인하지 못해서 선택할 수 없어요.\n"
            "닉네임 인증 이모티콘 또는 족보 등록 상태를 한 번 확인해 주세요.\n"
            "노미클은 남자로 간주합니다."
        )

    if sender_gender == target_gender:
        return False, (
            "이성에게만 사용할 수 있어요.\n\n"
            f"내 성별: {gender_label(sender_gender)}\n"
            f"상대 성별: {gender_label(target_gender)}\n"
            "노미클은 남자로 간주합니다."
        )

    return True, None


def active_user_rows_for_matching():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        u.user_id,
        u.user_name,
        u.gender,
        u.is_nomicl,
        u.updated_at,
        COALESCE(u.is_active, 1) AS is_active
    FROM users u
    LEFT JOIN deleted_users d
      ON d.original_user_id = u.user_id
    WHERE u.user_id IS NOT NULL
      AND u.user_id != ''
      AND COALESCE(u.is_active, 1) = 1
      AND d.original_user_id IS NULL
    ORDER BY u.updated_at DESC, u.user_name ASC
    """)
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def user_match_score(keyword, user_name):
    query = normalize_mention_name(keyword)
    if len(query) < 2:
        return None

    core = normalize_mention_name(user_name)
    full = normalize_match_text(user_name)
    if len(core) < 2 and len(full) < 2:
        return None

    if core == query:
        return 0
    if full == query:
        return 1
    if core.startswith(query):
        return 2
    if query in core:
        return 3
    if query in full:
        return 4
    return None


def find_active_user_candidates(keyword, limit=10, exclude_user_id=None):
    candidates = []
    for row in active_user_rows_for_matching():
        if exclude_user_id and row["user_id"] == exclude_user_id:
            continue
        score = user_match_score(keyword, row["user_name"])
        if score is None:
            continue
        item = dict(row)
        item["_match_score"] = score
        item["_match_core"] = normalize_mention_name(row["user_name"])
        candidates.append(item)

    candidates.sort(key=lambda row: (
        row["_match_score"],
        len(row["_match_core"] or row["user_name"]),
        row["user_name"],
    ))
    return candidates[:limit]


def resolve_active_user_by_nickname(keyword, exclude_user_id=None, purpose="대상"):
    candidates = find_active_user_candidates(keyword, limit=10, exclude_user_id=exclude_user_id)
    if not candidates:
        return None, (
            f"{purpose}을 찾지 못했습니다.\n"
            "닉네임을 조금만 더 정확히 입력해 주세요."
        )

    best_score = candidates[0]["_match_score"]
    best = [row for row in candidates if row["_match_score"] == best_score]
    if len(best) == 1:
        return best[0], None

    lines = [
        f"{purpose}이 여러 명 검색되었습니다.",
        "닉네임을 더 정확히 입력해 주세요.",
        "",
    ]
    for row in best[:5]:
        lines.append(f"- {row['user_name']}")
    return None, "\n".join(lines)


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
    log_id = cur.lastrowid
    conn.commit()
    conn.close()
    return log_id


def latest_chat_log_id(source_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT COALESCE(MAX(id), 0) AS log_id
    FROM chat_logs
    WHERE source_id = ?
    """, (source_id,))
    row = cur.fetchone()
    conn.close()
    return int(row["log_id"] or 0) if row else 0


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


def all_registered_users_text():
    """
    현재 DB users 테이블에 등록된 전체 유저를 모두 조회합니다.
    LINE 메시지 길이 제한은 호출부에서 reply_many + split_text_messages로 자동 분할합니다.
    사용법: /전체유저
    """
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN COALESCE(is_active, 1) = 1 THEN 1 ELSE 0 END) AS active_count,
        SUM(CASE WHEN COALESCE(is_active, 1) = 0 THEN 1 ELSE 0 END) AS inactive_count
    FROM users
    """)
    summary = cur.fetchone()

    total = int(summary["total"] or 0)
    active_count = int(summary["active_count"] or 0)
    inactive_count = int(summary["inactive_count"] or 0)

    if total == 0:
        conn.close()
        return "📋 현재 DB에 등록된 유저가 없습니다."

    cur.execute("""
    SELECT
        u.user_id,
        u.user_name,
        COALESCE(u.is_active, 1) AS is_active,
        COALESCE(c.balance, 0) AS balance,
        u.updated_at
    FROM users u
    LEFT JOIN currency c ON c.user_id = u.user_id
    ORDER BY COALESCE(u.is_active, 1) DESC, u.user_name ASC
    """)

    rows = cur.fetchall()
    conn.close()

    lines = [
        "📋 전체 등록 유저",
        "",
        f"총 인원: {total}명",
        f"활성: {active_count}명",
        f"비활성: {inactive_count}명",
        "",
    ]

    for idx, row in enumerate(rows, 1):
        status = "활성" if int(row["is_active"]) == 1 else "비활성"
        lines.append(
            f"{idx}. {row['user_name']} / {status} / {coin_text(row['balance'])}"
        )

    return "\n".join(lines)


def get_user_by_id(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        user_id,
        user_name,
        gender,
        is_nomicl,
        COALESCE(is_active, 1) AS is_active
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


def warning_text_for_staff(date_str, source_id):
    rows = warning_list(date_str, source_id)
    rows = sorted(rows, key=lambda r: (int(r["count"] or 0), str(r["user_name"])))

    if not rows:
        return (
            "✅ 오늘의 경고 대상이 없습니다.\n\n"
            "기준\n"
            f"📌 {WARNING_LIMIT}마디 미만\n\n"
            "현재 모든 인원이 기준을 충족했습니다."
        )

    lines = [
        "⚠️ 오늘의 경고 대상",
        "",
        "기준",
        f"📌 {WARNING_LIMIT}마디 미만",
        "",
        "━━━━━━━━━━",
    ]

    for row in rows:
        lines.append(f"{row['user_name']} - {row['count']}마디")

    lines += [
        "━━━━━━━━━━",
        "",
        f"총 {len(rows)}명",
        "",
        "🚨 위험구간",
        f"{WARNING_LIMIT}마디 미만 인원입니다.",
    ]
    return "\n".join(lines)


def madi_history_text(date_str, source_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        c.user_id,
        c.user_name,
        c.count,
        COALESCE(u.is_active, 1) AS is_active
    FROM counts c
    LEFT JOIN users u
      ON u.user_id = c.user_id
    LEFT JOIN deleted_users d
      ON d.original_user_id = c.user_id
    WHERE c.date = ?
      AND c.source_id = ?
      AND d.original_user_id IS NULL
      AND COALESCE(u.is_active, 1) = 1
    ORDER BY c.count DESC, c.user_name ASC
    """, (date_str, source_id))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return (
            "📊 마디수 기록 조회\n\n"
            f"기준일: {date_str}\n\n"
            "해당 날짜의 마디수 기록이 없습니다."
        )

    total_count = sum(int(row["count"] or 0) for row in rows)
    lines = [
        "📊 마디수 기록 조회",
        "",
        f"기준일: {date_str}",
        f"참여자: {len(rows)}명",
        f"총 마디수: {total_count}마디",
        "",
        "━━━━━━━━━━",
    ]

    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['user_name']} - {int(row['count'] or 0)}마디")

    lines.append("━━━━━━━━━━")
    return "\n".join(lines)


def warning_accumulated_days_text(source_id, min_days=1):
    min_days = max(1, int(min_days or 1))
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        c.user_id,
        COALESCE(u.user_name, c.user_name) AS user_name,
        COUNT(DISTINCT c.date) AS warning_days,
        MIN(c.date) AS first_date,
        MAX(c.date) AS last_date
    FROM counts c
    LEFT JOIN users u
      ON u.user_id = c.user_id
    LEFT JOIN deleted_users d
      ON d.original_user_id = c.user_id
    WHERE c.source_id = ?
      AND c.count < ?
      AND d.original_user_id IS NULL
      AND COALESCE(u.is_active, 1) = 1
    GROUP BY c.user_id
    HAVING warning_days >= ?
    ORDER BY warning_days DESC, user_name ASC
    """, (source_id, WARNING_LIMIT, min_days))
    rows = cur.fetchall()
    conn.close()

    lines = [
        "⚠️ 경고 누적일",
        "",
        f"기준: 일별 {WARNING_LIMIT}마디 미만",
        f"표시: {min_days}회 이상 누적",
        "",
        "━━━━━━━━━━",
    ]

    if not rows:
        lines.append("누적 경고 대상이 없습니다.")
    else:
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']} 경고 {int(row['warning_days'] or 0)}회 누적")
            lines.append(f"   기간: {row['first_date']} ~ {row['last_date']}")

    lines.append("━━━━━━━━━━")
    return "\n".join(lines)


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


def apply_money_change(cur, user_id, user_name, amount, reason, staff_user_id=None, staff_user_name=None):
    created_at = now_str()
    cur.execute("""
    INSERT INTO currency (user_id, balance, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        balance = balance + excluded.balance,
        updated_at = excluded.updated_at
    """, (user_id, amount, created_at))

    cur.execute("""
    INSERT INTO currency_logs (
        user_id, user_name, amount, reason,
        staff_user_id, staff_user_name, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, user_name, amount, reason, staff_user_id, staff_user_name, created_at))

    cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
    return cur.fetchone()["balance"]


def change_money(user_id, user_name, amount, reason, staff_user_id=None, staff_user_name=None):
    conn = db()
    cur = conn.cursor()
    balance = apply_money_change(cur, user_id, user_name, amount, reason, staff_user_id, staff_user_name)
    conn.commit()
    conn.close()
    return balance


REVIVAL_DAILY_LIMIT = 5
REVIVAL_REWARD = 100  # 10코인
REVIVAL_BALANCE_LIMIT = 100  # 10코인 미만일 때만 사용 가능


def revival_claim(date_str, user_id, user_name):
    if not user_id:
        return False, "💊 회생 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT balance FROM currency WHERE user_id = ?", (user_id,))
        balance_row = cur.fetchone()
        current_balance = int(balance_row["balance"] or 0) if balance_row else 0

        if current_balance >= REVIVAL_BALANCE_LIMIT:
            conn.close()
            return False, (
                "💊 회생 안내\n\n"
                "회생은 보유 코인이 10코인 미만일 때만 사용할 수 있어요.\n\n"
                f"현재 보유: {coin_text(current_balance)}"
            )

        cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM revival_claims
        WHERE date = ?
          AND user_id = ?
        """, (date_str, user_id))
        row = cur.fetchone()
        used = int(row["cnt"] or 0) if row else 0

        if used >= REVIVAL_DAILY_LIMIT:
            conn.close()
            return False, (
                "💊 회생 안내\n\n"
                "오늘 회생 가능 횟수를 모두 사용했습니다.\n\n"
                f"사용: {used} / {REVIVAL_DAILY_LIMIT}회\n"
                "초기화: 매일 00:00(KST)"
            )

        cur.execute("""
        INSERT INTO revival_claims (
            date, user_id, user_name, reward, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """, (date_str, user_id, user_name, REVIVAL_REWARD, now_str()))

        balance = apply_money_change(
            cur,
            user_id,
            user_name,
            REVIVAL_REWARD,
            "회생 지원",
            None,
            "회생"
        )
        conn.commit()
        conn.close()

        used_after = used + 1
        return True, (
            "💊 회생 완료\n\n"
            f"지급: {coin_text(REVIVAL_REWARD)}\n"
            f"오늘 사용: {used_after} / {REVIVAL_DAILY_LIMIT}회\n"
            f"남은 횟수: {REVIVAL_DAILY_LIMIT - used_after}회\n\n"
            f"현재 보유: {coin_text(balance)}"
        )
    except Exception as e:
        conn.rollback()
        conn.close()
        log_error("REVIVAL_CLAIM_ERROR", e)
        return False, "💊 회생 처리 중 문제가 생겼어요. 최근오류를 확인해 주세요."


def reset_revival_claims(target_date=None):
    raw_target = str(target_date or "").strip()
    reset_all = raw_target in ("전체", "all", "ALL", "All")

    if raw_target and not reset_all and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_target):
        return False, "사용법: /회생초기화 또는 /회생초기화 YYYY-MM-DD"

    date_filter = None if reset_all else (raw_target or today())

    conn = db()
    cur = conn.cursor()
    try:
        if date_filter:
            cur.execute("DELETE FROM revival_claims WHERE date = ?", (date_filter,))
            target_text = date_filter
        else:
            cur.execute("DELETE FROM revival_claims")
            target_text = "전체"

        deleted = cur.rowcount or 0
        conn.commit()
        conn.close()
        return True, (
            "💊 회생 횟수 초기화 완료\n\n"
            f"대상: {target_text}\n"
            f"초기화 기록: {deleted}건"
        )
    except Exception as e:
        conn.rollback()
        conn.close()
        log_error("REVIVAL_RESET_ERROR", e)
        return False, "💊 회생 초기화 중 문제가 생겼어요. 최근오류를 확인해 주세요."


def danbung_info_text():
    return (
        "💠 단벙 안내\n\n"
        "단벙은 단체 벙 참여 비용을 명확하게 정리하기 위한 기능입니다.\n\n"
        "1. 일반 단벙\n"
        "- 주최자와 참여자 모두 각 1코인이 차감됩니다.\n"
        "- 참여자는 꽃봇에게 /단벙참여 단벙제목 을 입력해 참여 처리할 수 있습니다.\n"
        "- 예: /단벙참여 @@1번단벙\n\n"
        "2. 단벙주최권 사용 단벙\n"
        "- 주최자가 단벙주최권을 구매해 사용하면 참여자는 코인이 차감되지 않습니다.\n"
        "- 단벙주최권은 운영진에게 문의해 주세요."
    )


def charge_danbung_attendance(user_id, user_name, event_name=""):
    event_name = (event_name or "").strip()
    if not event_name:
        return False, (
            "💠 단벙 참여 처리 실패\n\n"
            "참여할 단벙 제목을 함께 입력해주세요.\n\n"
            "사용법: /단벙참여 단벙제목\n"
            "예: /단벙참여 @@1번단벙"
        )

    cost = coin_to_points("1")
    balance = get_balance(user_id)
    if balance < cost:
        return False, (
            "💠 단벙 참여 처리 실패\n\n"
            f"보유: {coin_text(balance)}\n"
            f"필요: {coin_text(cost)}"
        )

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT OR IGNORE INTO danbung_attendance (
            date, event_name, user_id, user_name, cost, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """, (today(), event_name, user_id, user_name, cost, now_str()))
        if cur.rowcount == 0:
            conn.close()
            return False, (
                "💠 단벙 참여 처리 안내\n\n"
                f"단벙: {event_name}\n"
                "이미 오늘 이 단벙에 참여 처리되어 있습니다."
            )
        new_balance = apply_money_change(cur, user_id, user_name, -cost, f"단벙 참여: {event_name}", None, "단벙")
        conn.commit()
        conn.close()
    except Exception as e:
        conn.rollback()
        conn.close()
        log_error("DANBUNG_ATTENDANCE_LOG_ERROR", e)
        return False, "💠 단벙 참여 처리 중 문제가 생겼어요. 최근오류를 확인해 주세요."

    return True, (
        "💠 단벙 참여 처리 완료\n\n"
        f"단벙: {event_name}\n"
        f"차감: {coin_text(cost)}\n"
        f"현재 보유: {coin_text(new_balance)}\n\n"
        "참여 기록이 저장되었습니다."
    )


def danbung_attendance_status_text(date_str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        COALESCE(event_name, '') AS event_name,
        user_id,
        user_name,
        COUNT(*) AS attend_count,
        COALESCE(SUM(cost), 0) AS total_cost,
        MIN(created_at) AS first_at,
        MAX(created_at) AS last_at
    FROM danbung_attendance
    WHERE date = ?
    GROUP BY COALESCE(event_name, ''), user_id
    ORDER BY COALESCE(event_name, '') ASC, attend_count DESC, last_at ASC, user_name ASC
    """, (date_str,))
    rows = cur.fetchall()
    conn.close()

    lines = [
        "💠 단벙 참여 확인",
        "",
        f"기준일: {date_str}",
    ]

    if not rows:
        lines += ["", "━━━━━━━━━━", "해당 날짜의 단벙 참여 기록이 없습니다.", "━━━━━━━━━━"]
        return "\n".join(lines)

    total_records = sum(int(row["attend_count"] or 0) for row in rows)
    total_cost = sum(int(row["total_cost"] or 0) for row in rows)
    lines += [
        f"참여자: {len(rows)}명 / 기록: {total_records}회",
        f"차감 합계: {coin_text(total_cost)}",
        "",
        "━━━━━━━━━━",
    ]

    for i, row in enumerate(rows, 1):
        event_line = row["event_name"] or "제목 없음"
        lines.append(
            f"{i}. {row['user_name']} - {event_line} / {int(row['attend_count'] or 0)}회 / {coin_text(int(row['total_cost'] or 0))}"
        )
        lines.append(f"   최근: {row['last_at']}")

    lines.append("━━━━━━━━━━")
    return "\n".join(lines)


def danbung_attendance_event_text(event_name):
    event_name = (event_name or "").strip()
    if not event_name:
        return "사용법: /단벙참여확인 단벙제목"

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        user_id,
        user_name,
        COUNT(*) AS attend_count,
        COALESCE(SUM(cost), 0) AS total_cost,
        MIN(date) AS first_date,
        MAX(date) AS last_date,
        MIN(created_at) AS first_at,
        MAX(created_at) AS last_at
    FROM danbung_attendance
    WHERE event_name = ?
    GROUP BY user_id
    ORDER BY attend_count DESC, last_at ASC, user_name ASC
    """, (event_name,))
    rows = cur.fetchall()
    conn.close()

    lines = [
        "💠 단벙 참여 확인",
        "",
        f"단벙: {event_name}",
    ]

    if not rows:
        lines += ["", "━━━━━━━━━━", "해당 단벙의 참여 기록이 없습니다.", "━━━━━━━━━━"]
        return "\n".join(lines)

    total_records = sum(int(row["attend_count"] or 0) for row in rows)
    total_cost = sum(int(row["total_cost"] or 0) for row in rows)
    lines += [
        f"참여자: {len(rows)}명 / 기록: {total_records}회",
        f"차감 합계: {coin_text(total_cost)}",
        "",
        "━━━━━━━━━━",
    ]

    for i, row in enumerate(rows, 1):
        date_text = row["first_date"] if row["first_date"] == row["last_date"] else f"{row['first_date']} ~ {row['last_date']}"
        lines.append(
            f"{i}. {row['user_name']} - {int(row['attend_count'] or 0)}회 / {coin_text(int(row['total_cost'] or 0))}"
        )
        lines.append(f"   날짜: {date_text}")
        lines.append(f"   최근: {row['last_at']}")

    lines.append("━━━━━━━━━━")
    return "\n".join(lines)


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

    try:
        new_balance = apply_money_change(
            cur,
            user_id,
            user_name,
            -item["price"],
            f"상점 구매: {item['name']}",
            None,
            "상점"
        )

        cur.execute("""
        INSERT INTO purchases (
            user_id, user_name, item_name, price, status, created_at
        )
        VALUES (?, ?, ?, ?, 'owned', ?)
        """, (user_id, user_name, item["name"], item["price"], now_str()))

        purchase_id = cur.lastrowid
        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("BUY_ITEM_ERROR", e)
        return False, "🛒 구매 처리 중 문제가 생겼어요. 최근오류를 확인해 주세요."
    finally:
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


def list_user_purchases(user_id, status=None, limit=None):
    """
    유저 구매/보유 아이템 목록 조회.
    limit=None이면 전체 조회합니다.
    status 예: owned, used, cancel
    """
    conn = db()
    cur = conn.cursor()

    base_sql = """
        SELECT id, item_name, price, status, created_at, used_at, used_by, use_note
        FROM purchases
        WHERE user_id = ?
    """
    params = [user_id]

    if status:
        base_sql += " AND status = ?"
        params.append(status)

    base_sql += """
        ORDER BY
            CASE status
                WHEN 'owned' THEN 0
                WHEN 'pending' THEN 1
                WHEN 'used' THEN 2
                WHEN 'done' THEN 3
                WHEN 'cancel' THEN 4
                ELSE 5
            END,
            id DESC
    """

    if limit is not None:
        base_sql += " LIMIT ?"
        params.append(limit)

    cur.execute(base_sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def user_purchases_text(user_id, filter_mode="all"):
    """
    /내보유 출력용.
    filter_mode: all / owned / used
    미사용 아이템과 사용 완료 아이템을 분리해서 전체 출력합니다.
    """
    rows = list_user_purchases(user_id, limit=None)

    if not rows:
        return "보유하거나 구매한 상품이 없습니다."

    owned_rows = [r for r in rows if r["status"] in ("owned", "pending")]
    used_rows = [r for r in rows if r["status"] in ("used", "done")]
    cancel_rows = [r for r in rows if r["status"] == "cancel"]
    other_rows = [r for r in rows if r["status"] not in ("owned", "pending", "used", "done", "cancel")]

    if filter_mode == "owned":
        shown_groups = [("🎁 미사용 아이템", owned_rows)]
        title = "🎁 내 미사용 아이템"
    elif filter_mode == "used":
        shown_groups = [("📦 사용 완료 아이템", used_rows)]
        title = "📦 내 사용 완료 아이템"
    else:
        shown_groups = [
            ("🎁 미사용 아이템", owned_rows),
            ("📦 사용 완료 아이템", used_rows),
        ]
        title = "🎁 내 상품 보유 현황"

    lines = [
        title,
        "",
        f"미사용: {len(owned_rows)}개",
        f"사용완료: {len(used_rows)}개",
    ]

    if filter_mode == "all" and cancel_rows:
        lines.append(f"취소됨: {len(cancel_rows)}개")

    for group_title, group_rows in shown_groups:
        lines += ["", "━━━━━━━━━━", group_title, "━━━━━━━━━━"]

        if not group_rows:
            lines.append("없음")
            continue

        for row in group_rows:
            if row["status"] in ("owned", "pending"):
                lines.append(
                    f"#{row['id']} {row['item_name']} / {coin_text(row['price'])}\n"
                    f"구매일: {row['created_at']}"
                )
            else:
                used_line = row["used_at"] or "기록 없음"
                note_line = f"\n메모: {row['use_note']}" if row["use_note"] else ""
                lines.append(
                    f"#{row['id']} {row['item_name']} / {coin_text(row['price'])}\n"
                    f"사용일: {used_line}{note_line}"
                )

    if filter_mode == "all" and other_rows:
        lines += ["", "━━━━━━━━━━", "기타 상태 아이템", "━━━━━━━━━━"]
        for row in other_rows:
            lines.append(
                f"#{row['id']} {row['item_name']} / {coin_text(row['price'])}\n"
                f"상태: {status_text(row['status'])}"
            )

    lines += [
        "",
        "━━━━━━━━━━",
        "사용 방법",
        "━━━━━━━━━━",
        "/사용 구매번호",
        "",
        "필터 보기",
        "/내보유 미사용",
        "/내보유 사용",
    ]

    return "\n".join(lines)


def user_item_holdings_text():
    """
    운영진용 전체 미사용 아이템 보유 현황.
    /내보유와 동일하게 owned/pending 상태를 미사용 아이템으로 봅니다.
    """
    conn = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT
            p.user_id,
            COALESCE(u.user_name, p.user_name, '알 수 없음') AS user_name,
            p.item_name,
            COUNT(*) AS cnt
        FROM purchases p
        LEFT JOIN users u ON u.user_id = p.user_id
        WHERE p.status IN ('owned', 'pending')
          AND p.user_id IS NOT NULL
          AND p.user_id != ''
          AND p.item_name IS NOT NULL
          AND TRIM(p.item_name) != ''
          AND COALESCE(u.is_active, 1) = 1
          AND NOT EXISTS (
              SELECT 1
              FROM deleted_users d
              WHERE d.original_user_id = p.user_id
          )
        GROUP BY
            p.user_id,
            COALESCE(u.user_name, p.user_name, '알 수 없음'),
            p.item_name
        ORDER BY
            p.user_id ASC,
            cnt DESC,
            p.item_name ASC
        """)
        rows = cur.fetchall()
    except Exception as e:
        print("USER ITEM HOLDINGS ERROR:", e)
        return "🎁 유저 아이템 보유 현황을 불러오지 못했습니다."
    finally:
        if conn:
            conn.close()

    if not rows:
        return "🎁 유저 아이템 보유 현황\n\n현재 미사용 아이템을 가진 활성 유저가 없습니다."

    users = {}
    for row in rows:
        uid = row["user_id"]
        item_name = row["item_name"]
        cnt = int(row["cnt"] or 0)
        if cnt <= 0:
            continue
        if uid not in users:
            users[uid] = {
                "user_name": row["user_name"],
                "total": 0,
                "items": [],
            }
        users[uid]["total"] += cnt
        users[uid]["items"].append((item_name, cnt))

    ordered_users = sorted(
        users.values(),
        key=lambda info: (-info["total"], info["user_name"])
    )
    total_items = sum(info["total"] for info in ordered_users)

    lines = [
        "🎁 유저 아이템 보유 현황",
        "",
        f"보유 유저: {len(ordered_users)}명",
        f"미사용 아이템: {total_items}개",
        "기준: 미사용 상태(owned/pending)",
    ]

    for idx, info in enumerate(ordered_users, 1):
        item_text = ", ".join(
            f"{item_name} {cnt}개"
            for item_name, cnt in sorted(info["items"], key=lambda x: (-x[1], x[0]))
        )
        lines += [
            "",
            f"{idx}. {info['user_name']} ({info['total']}개)",
            item_text,
        ]

    return "\n".join(lines)


def item_match_score(keyword, item_name):
    query = normalize_match_text(keyword)
    target = normalize_match_text(item_name)
    if not query or not target:
        return None
    if target == query:
        return 0
    if target.startswith(query):
        return 1
    if query in target:
        return 2
    return None


def remove_user_items_by_name(user_keyword, item_keyword, amount, staff_user_name):
    """
    운영진용 아이템 일괄 삭제.
    실제 row를 삭제하지 않고 cancel 상태로 변경해서 지급/회수 기록은 남깁니다.
    """
    amount_match = re.fullmatch(r"\s*(\d+)\s*개?\s*", str(amount or ""))
    if not amount_match:
        return False, "사용법: /유저아이템삭제 닉네임 아이템명 개수"
    amount = int(amount_match.group(1))

    if amount <= 0:
        return False, "삭제 개수는 1개 이상으로 입력해주세요."

    target, err = resolve_active_user_by_nickname(user_keyword, purpose="유저")
    if err:
        return False, err

    conn = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT item_name, COUNT(*) AS cnt
        FROM purchases
        WHERE user_id = ?
          AND status IN ('owned', 'pending')
          AND item_name IS NOT NULL
          AND TRIM(item_name) != ''
        GROUP BY item_name
        ORDER BY cnt DESC, item_name ASC
        """, (target["user_id"],))
        rows = cur.fetchall()

        if not rows:
            return False, f"{target['user_name']}님은 현재 미사용 아이템이 없습니다."

        candidates = []
        for row in rows:
            score = item_match_score(item_keyword, row["item_name"])
            if score is not None:
                candidates.append((score, row))

        if not candidates:
            owned_text = ", ".join(f"{row['item_name']} {int(row['cnt'] or 0)}개" for row in rows)
            return False, (
                "해당 아이템을 찾을 수 없습니다.\n\n"
                f"대상: {target['user_name']}\n"
                f"검색어: {item_keyword}\n"
                f"보유 아이템: {owned_text}"
            )

        candidates.sort(key=lambda x: (x[0], len(x[1]["item_name"]), x[1]["item_name"]))
        best_score = candidates[0][0]
        best = [row for score, row in candidates if score == best_score]
        if len(best) > 1:
            lines = [
                "아이템이 여러 개 검색되었습니다.",
                "아이템명을 더 정확히 입력해주세요.",
                "",
            ]
            for row in best[:5]:
                lines.append(f"- {row['item_name']} {int(row['cnt'] or 0)}개")
            return False, "\n".join(lines)

        item_name = best[0]["item_name"]
        available = int(best[0]["cnt"] or 0)
        if available < amount:
            return False, (
                "삭제할 수량이 보유 수량보다 많습니다.\n\n"
                f"대상: {target['user_name']}\n"
                f"아이템: {item_name}\n"
                f"보유: {available}개\n"
                f"요청: {amount}개"
            )

        cur.execute("""
        SELECT id
        FROM purchases
        WHERE user_id = ?
          AND item_name = ?
          AND status IN ('owned', 'pending')
        ORDER BY id ASC
        LIMIT ?
        """, (target["user_id"], item_name, amount))
        purchase_ids = [int(row["id"]) for row in cur.fetchall()]

        if len(purchase_ids) < amount:
            return False, "삭제 대상 구매 기록을 충분히 찾지 못했습니다. 다시 확인해주세요."

        placeholders = ",".join("?" for _ in purchase_ids)
        cur.execute(f"""
        UPDATE purchases
        SET status = 'cancel',
            processed_at = ?,
            processed_by = ?,
            use_note = ?
        WHERE id IN ({placeholders})
        """, [now_str(), staff_user_name, "운영진 일괄 아이템 삭제"] + purchase_ids)

        removed = cur.rowcount
        conn.commit()
        remaining = available - removed
        id_text = ", ".join(f"#{pid}" for pid in purchase_ids)
        return True, (
            "🗑️ 유저 아이템 삭제 완료\n\n"
            f"대상: {target['user_name']}\n"
            f"아이템: {item_name}\n"
            f"삭제 수량: {removed}개\n"
            f"남은 수량: {remaining}개\n"
            f"처리 구매번호: {id_text}\n"
            "환불: 없음"
        )
    except Exception as e:
        print("REMOVE USER ITEMS ERROR:", e)
        return False, "유저 아이템 삭제 처리 중 오류가 발생했습니다."
    finally:
        if conn:
            conn.close()


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
ATTENDANCE_REWARD = 20  # 2코인
ATTENDANCE_STREAK_INTERVAL = 7
ATTENDANCE_STREAK_BASE_REWARD = 20  # 7일차 2코인, 이후 7일마다 +1코인

MISSION_REWARDS = [
    ("daily_10", 10, 5),    # 10마디 = 0.5코인
    ("daily_20", 20, 10),   # 20마디 = 1코인
    ("daily_50", 50, 15),   # 50마디 = 1.5코인
    ("daily_100", 100, 30), # 100마디 = 3코인
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
    reward = ATTENDANCE_REWARD

    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("""
        INSERT OR IGNORE INTO attendance (date, user_id, user_name, reward, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (date_str, user_id, user_name, reward, now_str()))

        if cur.rowcount == 0:
            cur.execute("SELECT COALESCE(balance, 0) AS balance FROM currency WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            conn.close()
            return False, int(row["balance"] or 0) if row else 0

        balance = apply_money_change(
            cur,
            user_id,
            user_name,
            reward,
            f"출석체크 {date_str}",
            None,
            "출석시스템"
        )
        conn.commit()
        conn.close()
        return True, balance
    except Exception as e:
        conn.rollback()
        conn.close()
        log_error("ATTENDANCE_CHECK_ERROR", e)
        try:
            return False, get_balance(user_id)
        except Exception:
            return False, 0


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

    try:
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

        if total_reward > 0:
            apply_money_change(
                cur,
                user_id,
                user_name,
                total_reward,
                f"일일미션 보상: {', '.join(claimed_names)}",
                None,
                "미션시스템"
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        log_error("CLAIM_MISSIONS_ERROR", e)
        return 0, count, []

    conn.close()

    return total_reward, count, claimed_names





# =========================
# 가챠 시스템
# =========================
GACHA_COSTS = {
    "하": 10,  # 1코인
    "중": 30,  # 3코인
    "상": 50,  # 5코인
}

# 가챠 횟수는 제한하지 않고, KST 기준 매주 토요일 00:00에 기록만 새 주차로 전환합니다.

GACHA_TYPE_LABELS = {
    "coin": "코인형",
    "piece": "조각형",
    "random": "랜덤형",
}

COIN_GACHA_WEIGHTS = {
    "하": [(50, "F"), (36, "D"), (10, "C"), (4, "B")],
    "중": [(50, "F"), (33, "D"), (11, "C"), (5, "B"), (1, "A")],
    "상": [(50, "F"), (31, "D"), (10, "C"), (6, "B"), (2.5, "A"), (0.5, "S")],
}

PIECE_GACHA_WEIGHTS = {
    "하": [(50, "F"), (26, "E"), (16, "D"), (6, "C"), (2, "B")],
    "중": [(50, "F"), (22, "E"), (16, "D"), (8, "C"), (3, "B"), (1, "A")],
    "상": [(50, "F"), (18, "E"), (16, "D"), (10, "C"), (4, "B"), (1.8, "A"), (0.2, "S")],
}

PIECE_STANDALONE_GACHA_WEIGHTS = [(50, "F"), (50, "piece")]

KIMMEAT_SANG_GACHA_WEIGHTS = [
    (10, "F"),
    (20, "E"),
    (30, "D"),
    (20, "C"),
    (12, "B"),
    (6, "A"),
    (2, "S"),
]

PIECE_INFO = {
    "iron": {"label": "철 조각", "need": 10, "reward": 5},
    "silver": {"label": "은 조각", "need": 10, "reward": 10},
    "gold": {"label": "금 조각", "need": 10, "reward": 20},
}
OLD_PIECE_KEYS = {"선갠라", "단벙", "봇등록", "미션", "임티", "칭호"}
GACHA_PITY_REWARD = 50  # 행운포인트 10점 달성 보상: 5코인


def piece_item_name(info):
    return info.get("item") or f"{info['label']} 완성 보상"


def weighted_pick(weighted_items):
    total = sum(weight for weight, _ in weighted_items)
    point = random.uniform(0, total)
    upto = 0

    for weight, item in weighted_items:
        upto += weight
        if point <= upto:
            return item

    return weighted_items[-1][1]


def percent_text(value):
    value = float(value)
    if value.is_integer():
        return f"{int(value)}%"
    return f"{value:g}%"


def gacha_weight_line(label, weights):
    return f"{label}: " + " / ".join(f"{grade} {percent_text(weight)}" for weight, grade in weights)


def gacha_probability_text():
    lines = [
        "등급 분포",
        "",
        "코인 가챠",
        gacha_weight_line("하", COIN_GACHA_WEIGHTS["하"]),
        gacha_weight_line("중", COIN_GACHA_WEIGHTS["중"]),
        gacha_weight_line("상", COIN_GACHA_WEIGHTS["상"]),
        "",
        "조각 가챠",
        "조각가챠: F 50% / 조각 50%",
        "",
        "조각형 등급 분포",
        gacha_weight_line("하", PIECE_GACHA_WEIGHTS["하"]),
        gacha_weight_line("중", PIECE_GACHA_WEIGHTS["중"]),
        gacha_weight_line("상", PIECE_GACHA_WEIGHTS["상"]),
    ]
    return "\n".join(lines)


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
        completed.append(piece_item_name(info))

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


def apply_gacha_pity_point(cur, user_id, user_name):
    """
    코인형 가챠 F등급 보정 포인트를 같은 거래 안에서 처리합니다.
    """
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
    pity_points = int(cur.fetchone()["pity_points"] or 0)

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

        apply_money_change(
            cur,
            user_id,
            user_name,
            bonus_paid * GACHA_PITY_REWARD,
            f"코인형 가챠 행운포인트 {bonus_paid * 10}점 보상",
            None,
            "가챠시스템"
        )

    return pity_points, bonus_paid


def add_gacha_pity_point(user_id, user_name):
    """
    코인형 가챠 F등급 보정:
    F등급 1회 = 행운포인트 1
    10포인트 달성 시 5코인 자동 지급 후 10포인트 차감.
    """
    conn = db()
    cur = conn.cursor()
    try:
        result = apply_gacha_pity_point(cur, user_id, user_name)
        conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        log_error("GACHA_PITY_ERROR", e)
        return get_gacha_pity_point(user_id), 0
    finally:
        conn.close()


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


def gacha_grade(gacha_type, tier, coin_weights=None):
    if gacha_type == "coin":
        return weighted_pick(coin_weights or COIN_GACHA_WEIGHTS[tier])

    return weighted_pick(PIECE_GACHA_WEIGHTS[tier])


def random_piece_by_group(group=None):
    return weighted_pick([(60, "iron"), (30, "silver"), (10, "gold")])


def coin_prize_for(tier, grade):
    prize_table = {
        "하": {
            "F": [0, 2, 5],
            "E": [10],
            "D": [12, 15],
            "C": [18],
            "B": [20],
        },
        "중": {
            "F": [0, 10, 20],
            "E": [30],
            "D": [35, 40],
            "C": [45, 50],
            "B": [60],
            "A": [60],
        },
        "상": {
            "F": [0, 20, 30, 40],
            "E": [50],
            "D": [60, 70],
            "C": [80, 90],
            "B": [100],
            "A": [100],
            "S": [100],
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
            "F": None,
            "E": ("low", 3),
            "D": ("mid", 2),
            "C": ("high", 2),
            "B": ("high", 5),
            "A": ("all", 10),
        }
    else:
        table = {
            "F": None,
            "E": ("mid", 5),
            "D": ("high", 5),
            "C": ("high", 10),
            "B": ("all", 15),
            "A": ("all", 25),
            "S": ("all", 50),
        }

    value = table[grade]
    if value is None:
        return None

    group, amount = value
    piece_key = random_piece_by_group(group)
    return piece_key, amount


def random_prize_kind(tier, grade):
    # 랜덤형은 코인/조각 혼합.
    # F는 낮은 등급이라 코인 소액 또는 꽝 위주.
    if grade == "F":
        return weighted_pick([(70, "coin"), (30, "piece")])
    return weighted_pick([(50, "coin"), (50, "piece")])


def get_weekly_gacha_count(user_id):
    """
    이번 주 가챠 사용 횟수 조회.
    gacha_week_range_for_today() 기준이라 KST 토요일 00:00에 자동 초기화됩니다.
    """
    week_start, week_end = gacha_week_range_for_today()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT count
    FROM gacha_weekly_counts
    WHERE week_start = ?
      AND user_id = ?
    """, (week_start, user_id))
    row = cur.fetchone()
    conn.close()
    return int(row["count"]) if row else 0


def add_weekly_gacha_count(user_id, user_name):
    """
    가챠 성공 이용 후 이번 주 사용 횟수 +1.
    """
    conn = db()
    cur = conn.cursor()
    try:
        count = apply_weekly_gacha_count(cur, user_id, user_name)
        conn.commit()
        return count
    except Exception as e:
        conn.rollback()
        log_error("GACHA_COUNT_ERROR", e)
        return get_weekly_gacha_count(user_id)
    finally:
        conn.close()


def apply_weekly_gacha_count(cur, user_id, user_name):
    """
    이번 주 가챠 이용 횟수를 같은 거래 안에서 1회 증가시킵니다.
    """
    week_start, week_end = gacha_week_range_for_today()
    cur.execute("""
    INSERT INTO gacha_weekly_counts (
        week_start, week_end, user_id, user_name, count, updated_at
    )
    VALUES (?, ?, ?, ?, 1, ?)
    ON CONFLICT(week_start, user_id)
    DO UPDATE SET
        week_end = excluded.week_end,
        user_name = excluded.user_name,
        count = count + 1,
        updated_at = excluded.updated_at
    """, (week_start, week_end, user_id, user_name, now_str()))
    cur.execute("""
    SELECT count
    FROM gacha_weekly_counts
    WHERE week_start = ?
      AND user_id = ?
    """, (week_start, user_id))
    row = cur.fetchone()
    return int(row["count"] or 0) if row else 0


def gacha_count_status_text(user_id):
    week_start, week_end = gacha_week_range_for_today()
    used = get_weekly_gacha_count(user_id)
    return (
        "🎰 주간 가챠 사용 현황\n\n"
        f"기간: {week_start} ~ {week_end}\n"
        f"사용: {used}회\n"
        "남은 횟수: 제한 없음\n\n"
        "※ 매주 토요일 00:00(KST)에 자동 초기화됩니다."
    )


def run_gacha(user_id, user_name, tier, coin_weights=None, log_command=None, bypass_weekly_limit=False):
    if tier not in GACHA_COSTS:
        return False, "사용법\n\n/가챠 하\n/가챠 중\n/가챠 상"

    gacha_type = "coin"
    cost = GACHA_COSTS[tier]
    used_count = get_weekly_gacha_count(user_id)

    log_label = log_command or f"{tier} 가챠"
    grade = gacha_grade(gacha_type, tier, coin_weights=coin_weights)
    prize = coin_prize_for(tier, grade)
    pity_points = None
    bonus_paid = 0
    weekly_used_after = used_count
    final_balance = 0

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(balance, 0) AS balance FROM currency WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        balance = int(row["balance"] or 0) if row else 0
        if balance < cost:
            return False, (
                f"코인이 부족합니다.\n\n"
                f"필요: {coin_text(cost)}\n"
                f"보유: {coin_text(balance)}"
            )

        final_balance = apply_money_change(cur, user_id, user_name, -cost, f"{log_label} 이용", None, "가챠시스템")

        weekly_used_after = apply_weekly_gacha_count(cur, user_id, user_name)

        if prize > 0:
            final_balance = apply_money_change(
                cur,
                user_id,
                user_name,
                prize,
                f"{log_label} {grade}등급 코인 보상",
                None,
                "가챠시스템"
            )

        if grade == "F":
            pity_points, bonus_paid = apply_gacha_pity_point(cur, user_id, user_name)
            if bonus_paid > 0:
                final_balance += bonus_paid * GACHA_PITY_REWARD

        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("RUN_GACHA_ERROR", e)
        return False, "🎰 가챠 처리 중 문제가 생겼어요. 최근오류를 확인해 주세요."
    finally:
        conn.close()

    lines = [
        f"🎰 {tier}급 가챠 결과",
        "",
        f"타입: {GACHA_TYPE_LABELS[gacha_type]}",
        f"등급: {grade}",
        "",
    ]

    if prize > 0:
        lines.append(f"획득: 💰{coin_text(prize)}")
    else:
        lines.append("획득: 꽝")
        lines.append("다음 기회에...")

    if grade == "F":
        lines.append("")
        lines.append("🎁 행운포인트 +1")
        lines.append(f"현재 행운포인트: {pity_points or 0} / 10")

        if bonus_paid > 0:
            lines.append("")
            lines.append(f"🎉 행운포인트 보상 +{coin_text(bonus_paid * GACHA_PITY_REWARD)}")

    lines.append("")
    lines.append(f"이번 주 가챠: {weekly_used_after}회")
    lines.append(f"현재 잔액: {coin_text(final_balance)}")

    return True, "\n".join(lines)


def run_kimmeat_sang_gacha(user_id, user_name):
    return run_gacha(
        user_id,
        user_name,
        "상",
        coin_weights=KIMMEAT_SANG_GACHA_WEIGHTS,
        log_command="/가챠상",
        bypass_weekly_limit=True
    )


def gacha_system_text():
    return (
        "🎰 가챠 시스템 🎰\n\n"
        "운영시간\n"
        "제한 없음\n\n"
        "※ 가챠는 운영방에서 운영진만 이용할 수 있습니다.\n"
        "※ 주간 이용 제한은 없습니다.\n"
        "※ 상/중/하/조각 가챠 횟수는 기록만 표시됩니다.\n\n"
        "━━━━━━━━━━\n"
        "💰 코인 가챠\n"
        "━━━━━━━━━━\n\n"
        "/가챠 하 : 1코인\n"
        "/가챠 중 : 3코인\n"
        "/가챠 상 : 5코인\n\n"
        "결과 범위: 0배 ~ 2배\n"
        "결과에 따라 코인이 줄거나 늘어날 수 있습니다.\n\n"
        f"{gacha_probability_text()}\n\n"
        "━━━━━━━━━━\n"
        "🧩 조각 가챠\n"
        "━━━━━━━━━━\n\n"
        "/가챠 조각 : 1코인\n"
        "획득: 철 / 은 / 금 조각 또는 꽝\n\n"
        "━━━━━━━━━━\n"
        "🔨 대장장이\n"
        "━━━━━━━━━━\n\n"
        "철 조각 10개 → 0.5코인\n"
        "은 조각 10개 → 1코인\n"
        "금 조각 10개 → 2코인\n\n"
        "확인: /가챠 조각확인\n"
        "교환: /가챠 대장장이\n"
        "횟수: /가챠 횟수"
    )


# =========================
# 히든 미션
# =========================

def hidden_reward_message(title, reason, user_name, reward):
    return (
        f"{title}\n\n"
        f"{reason}\n"
        f"달성자: {user_name}\n"
        f"보상: 💰{coin_text(reward)}"
    )


def achievement_message(achievement_name, user_name, reward):
    return (
        "🏆 업적 달성!\n\n"
        f"{achievement_name}\n"
        f"달성자: {user_name}\n"
        f"보상: 💰{coin_text(reward)}"
    )


def grant_hidden_reward_once(date_str, mission_key, user_id, user_name, reward, reason, meta=""):
    """
    같은 날짜 + 같은 미션키는 1번만 지급.
    선착순/행운번호 보상에 사용.
    """
    conn = db()
    cur = conn.cursor()

    try:
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
        if inserted:
            apply_money_change(
                cur,
                user_id,
                user_name,
                reward,
                reason,
                None,
                "히든이벤트"
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("GRANT_HIDDEN_REWARD_ERROR", e)
        inserted = 0
    finally:
        conn.close()

    if inserted:
        return hidden_reward_message("🎉 히든 미션 달성!", reason, user_name, reward)

    return None




def get_system_flag(key, default=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM system_flags WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_system_flag(key, value):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO system_flags (key, value)
    VALUES (?, ?)
    ON CONFLICT(key)
    DO UPDATE SET value = excluded.value
    """, (key, str(value)))
    conn.commit()
    conn.close()


def daily_jackpot_mission_key(source_id, seq):
    """당일 + 방 + 순번 기준으로 잭팟 중복 지급을 막기 위한 키."""
    safe_source = str(source_id).replace(":", "_")
    return f"daily_chat_jackpot_{safe_source}_{seq}"


def is_bot_jackpot_user(user_id, user_name=""):
    """
    봇이 잭팟 순번을 밟았는지 판단.
    Railway Variables에 BOT_USER_ID를 넣으면 가장 정확합니다.
    LINE 봇이 직접 보낸 push 메시지는 보통 webhook으로 다시 들어오지 않지만,
    혹시 들어오는 환경이면 이 값으로 다음 사람 지급 처리가 됩니다.
    """
    if not user_id:
        return True
    if BOT_USER_ID and str(user_id).strip() == BOT_USER_ID:
        return True
    return False


def get_daily_lucky_number(date_str):
    """
    매일 1~10000 사이 랜덤 잭팟 번호 생성.
    date_str가 바뀌면 새로 생성되므로 KST 자정 기준 자동 초기화됩니다.
    고정 잭팟 번호 777 / 7777 / 10000과는 겹치지 않게 합니다.
    """
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
        return int(row["lucky_number"])

    lucky_number = random.randint(1, 10000)
    while lucky_number in (777, 7777, 10000):
        lucky_number = random.randint(1, 10000)

    cur.execute("""
    INSERT INTO daily_lucky_numbers (date, lucky_number, created_at)
    VALUES (?, ?, ?)
    """, (date_str, lucky_number, now_str()))

    conn.commit()
    conn.close()

    return lucky_number


def get_today_chat_log_sequence(source_id, date_str):
    """
    당일 로그상 순번.
    반드시 save_chat_log() 호출 후 실행해야 현재 메시지가 포함됩니다.
    """
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
    return int(row["total_logs"] or 0) if row else 0


def grant_daily_chat_jackpot(date_str, source_id, seq, user_id, user_name, reward, reason, meta=""):
    """
    당일 + 방 + 순번별 1회만 보상 지급.
    hidden_rewards에 저장하고, 지급 성공 시 코인 지급 + 방 알림.
    """
    mission_key = daily_jackpot_mission_key(source_id, seq)

    conn = db()
    cur = conn.cursor()

    try:
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
            meta or f"source_id={source_id};seq={seq}",
            now_str()
        ))

        inserted = cur.rowcount
        if inserted:
            apply_money_change(
                cur,
                user_id,
                user_name,
                reward,
                reason,
                None,
                "채팅잭팟"
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("GRANT_DAILY_CHAT_JACKPOT_ERROR", e)
        inserted = 0
    finally:
        conn.close()

    if not inserted:
        return False

    return hidden_reward_message("🎉 히든 보상 달성!", reason, user_name, reward)


def set_pending_daily_jackpot(date_str, source_id, seq, reward, reason):
    """
    봇이 잭팟 순번을 밟은 경우 다음 일반 유저에게 넘기기 위한 대기 저장.
    날짜가 바뀌면 key가 달라지므로 자동 초기화 효과가 있습니다.
    """
    prefix = f"pending_daily_chat_jackpot:{date_str}:{source_id}"
    set_system_flag(f"{prefix}:seq", seq)
    set_system_flag(f"{prefix}:reward", reward)
    set_system_flag(f"{prefix}:reason", reason)


def pop_pending_daily_jackpot(date_str, source_id):
    prefix = f"pending_daily_chat_jackpot:{date_str}:{source_id}"
    seq = get_system_flag(f"{prefix}:seq", "")
    reward = get_system_flag(f"{prefix}:reward", "")
    reason = get_system_flag(f"{prefix}:reason", "")

    if not seq or not reward or not reason:
        return None

    set_system_flag(f"{prefix}:seq", "")
    set_system_flag(f"{prefix}:reward", "")
    set_system_flag(f"{prefix}:reason", "")

    return int(seq), int(reward), reason


def check_daily_chat_jackpot_rewards(date_str, source_id, user_id, user_name):
    """
    당일 chat_logs 순번 기준 채팅 보상.

    지급 목록:
    - 777번째 채팅: 1코인
    - 7777번째 채팅: 2코인
    - 10000번째 채팅: 3코인
    - 매일 랜덤 1~10000번째 채팅: 2코인

    봇이 해당 순번이면 바로 지급하지 않고 다음 일반 유저에게 지급합니다.
    """
    if source_id != COUNT_SOURCE_ID:
        return []

    paid = []

    # 봇이 밟은 잭팟이 있으면 다음 일반 유저에게 지급
    if not is_bot_jackpot_user(user_id, user_name):
        pending = pop_pending_daily_jackpot(date_str, source_id)
        if pending:
            pending_seq, pending_reward, pending_reason = pending
            msg = grant_daily_chat_jackpot(
                date_str,
                source_id,
                pending_seq,
                user_id,
                user_name,
                pending_reward,
                f"{pending_reason} / 봇 순번으로 다음 채팅자 지급",
                f"source_id={source_id};seq={pending_seq};pending_to_next=1"
            )
            if msg:
                paid.append(msg)

    seq = get_today_chat_log_sequence(source_id, date_str)
    lucky_number = get_daily_lucky_number(date_str)

    targets = [
        (777, 10, "🎰 당일 777번째 채팅 잭팟"),
        (7777, 20, "🎰 당일 7777번째 채팅 메가잭팟"),
        (10000, 30, "🎰 당일 10000번째 채팅 슈퍼잭팟"),
        (lucky_number, 20, f"🎊 당일 랜덤 채팅 잭팟: {lucky_number}번째 채팅"),
    ]

    for target_seq, reward, reason in targets:
        if seq != target_seq:
            continue

        if is_bot_jackpot_user(user_id, user_name):
            set_pending_daily_jackpot(date_str, source_id, target_seq, reward, reason)
            continue

        msg = grant_daily_chat_jackpot(
            date_str,
            source_id,
            target_seq,
            user_id,
            user_name,
            reward,
            reason,
            f"source_id={source_id};seq={seq};lucky_number={lucky_number}"
        )
        if msg:
            paid.append(msg)

    return paid


# 구버전 함수명 호환용: 다른 곳에서 호출해도 당일 기준으로 동작하게 유지
# 단, date_str 없이 호출되는 구버전 형태라 today()를 사용합니다.
def check_chat_jackpot_rewards(source_id, user_id, user_name):
    return check_daily_chat_jackpot_rewards(today(), source_id, user_id, user_name)


def chat_jackpot_status(date_str=None, source_id=None):
    date_str = date_str or today()
    source_id = source_id or COUNT_SOURCE_ID
    target = get_daily_lucky_number(date_str)
    total = get_today_chat_log_sequence(source_id, date_str)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT mission_key, user_name, reward, meta, created_at
    FROM hidden_rewards
    WHERE date = ?
      AND mission_key LIKE ?
    ORDER BY created_at ASC
    """, (date_str, f"daily_chat_jackpot_{str(source_id).replace(':', '_')}_%"))
    rows = cur.fetchall()
    conn.close()

    # 기존 반환 형태 유지: total, target, random_claimed, rows
    random_claimed = any(str(target) in (row["meta"] or "") or row["mission_key"].endswith(f"_{target}") for row in rows)
    return total, target, random_claimed, rows


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
        "🎯 당일 첫 1000마디 달성",
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
        "👻 사이버망령 당일 첫 2000마디 달성",
        f"count={count}"
    )


def attendance_streak_days(user_id, date_str):
    """
    date_str 기준 출석일차 계산.
    중간에 /출석을 놓쳐도 첫 출석일 기준으로 일차는 계속 진행됩니다.
    """
    base = datetime.strptime(date_str, "%Y-%m-%d").date()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT MIN(date) AS first_date
    FROM attendance
    WHERE user_id = ?
      AND date <= ?
    """, (user_id, date_str))
    row = cur.fetchone()
    conn.close()

    if not row or not row["first_date"]:
        return 0

    first_date = datetime.strptime(row["first_date"], "%Y-%m-%d").date()
    if first_date > base:
        return 0

    return (base - first_date).days + 1


def mark_legacy_attendance_streak_reward_claimed(user_id, user_name, streak_days, reward):
    legacy_key = f"attendance_streak_{streak_days}_{user_id}"
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT date, created_at
    FROM hidden_rewards
    WHERE user_id = ?
      AND mission_key = ?
    ORDER BY date ASC
    LIMIT 1
    """, (user_id, legacy_key))
    row = cur.fetchone()

    if not row:
        conn.close()
        return False

    cur.execute("""
    INSERT OR IGNORE INTO attendance_streak_rewards (
        user_id, user_name, streak_days, reward, achieved_date, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        user_name,
        streak_days,
        reward,
        row["date"],
        row["created_at"] or now_str()
    ))
    inserted = cur.rowcount > 0
    conn.commit()
    conn.close()

    if inserted:
        print(
            "ATTENDANCE_STREAK_REWARD_LEGACY_CLAIMED:",
            user_id,
            user_name,
            streak_days
        )
    return True


def grant_attendance_streak_reward_once(date_str, user_id, user_name, streak_days, reward, current_streak):
    """
    출석일수 보상은 유저별/단계별로 한 번만 지급합니다.
    """
    if mark_legacy_attendance_streak_reward_claimed(user_id, user_name, streak_days, reward):
        return False

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT OR IGNORE INTO attendance_streak_rewards (
            user_id, user_name, streak_days, reward, achieved_date, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            user_name,
            streak_days,
            reward,
            date_str,
            now_str()
        ))
        inserted = cur.rowcount > 0
        if inserted:
            apply_money_change(
                cur,
                user_id,
                user_name,
                reward,
                f"출석일수 보상: {streak_days}일차 출석",
                None,
                "출석시스템"
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("ATTENDANCE_STREAK_REWARD_ERROR", e)
        inserted = False
    finally:
        conn.close()

    if not inserted:
        return False

    print(
        "ATTENDANCE_STREAK_REWARD:",
        user_id,
        user_name,
        streak_days,
        reward,
        f"current_streak={current_streak}"
    )
    return True


def check_attendance_streak_reward(date_str, user_id, user_name):
    """
    출석일수 보상:
    7일차 2코인, 14일차 3코인, 21일차 4코인처럼
    7일 단위마다 1코인씩 증가하며 각 구간별 1회 지급.
    """
    streak = attendance_streak_days(user_id, date_str)

    paid = []

    for required_days in range(ATTENDANCE_STREAK_INTERVAL, streak + 1, ATTENDANCE_STREAK_INTERVAL):
        reward = ATTENDANCE_STREAK_BASE_REWARD + ((required_days // ATTENDANCE_STREAK_INTERVAL) - 1) * 10
        ok = grant_attendance_streak_reward_once(
            date_str,
            user_id,
            user_name,
            required_days,
            reward,
            streak
        )
        if ok:
            paid.append((required_days, reward))

    return streak, paid


def check_lucky_log_rewards(date_str, source_id, user_id, user_name):
    """구버전 함수명 호환용. 실제 지급은 check_daily_chat_jackpot_rewards에서 처리합니다."""
    return check_daily_chat_jackpot_rewards(date_str, source_id, user_id, user_name)


def check_lucky_guy_reward(date_str, source_id, user_id, user_name):
    """구버전 함수명 호환용. 실제 지급은 check_daily_chat_jackpot_rewards에서 처리합니다."""
    paid = check_daily_chat_jackpot_rewards(date_str, source_id, user_id, user_name)
    return bool(paid)


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


def gacha_week_range_for_today():
    """
    가챠 전용 주차.
    KST 기준 매주 토요일 00:00에 새 주차로 자동 초기화됩니다.
    기간: 토요일 ~ 다음 주 금요일
    """
    now = datetime.now(KST).date()
    # Python weekday(): 월=0, 토=5
    days_since_saturday = (now.weekday() - 5) % 7
    start = now - timedelta(days=days_since_saturday)
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
        return 20   # 2코인
    if rank == 2:
        return 10   # 1코인
    if rank == 3:
        return 5    # 0.5코인
    if rank >= 4:
        return 2    # 0.2코인
    return 0


def settle_weekly_rewards(source_id, week_start, week_end):
    rows = weekly_ranking_rows(source_id, week_start, week_end, limit=10)
    paid = []

    conn = db()
    cur = conn.cursor()

    try:
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
                apply_money_change(
                    cur,
                    row["user_id"],
                    row["user_name"],
                    reward,
                    f"주간 마디수 랭킹 보상 {week_start}~{week_end} {idx}위",
                    None,
                    "주간정산"
                )
                paid.append({
                    "rank": idx,
                    "user_id": row["user_id"],
                    "user_name": row["user_name"],
                    "count": row["total_count"],
                    "reward": reward,
                })

        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("SETTLE_WEEKLY_REWARDS_ERROR", e)
        paid = []
    finally:
        conn.close()

    return paid


# =========================
# S.N.S 럭키드로우
# =========================
EVENT_TICKET_PRICE = 10          # 럭키드로우 1장 = 1코인
EVENT_BASE_PRIZE = 50            # 기본 부스팅 5코인
EVENT_PAYOUT_RATE = 0.9          # 럭키드로우 판매액 90% 지급


def event_week_key():
    return week_range_for_today()


def is_saturday_draw_time():
    now = datetime.now(KST)
    return now.weekday() == 5 and now.hour >= 21


def buy_lucky_draw_ticket(user_id, user_name):
    week_start, week_end = event_week_key()

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM sns_lucky_draw_results WHERE week_start = ?", (week_start,))
        if cur.fetchone():
            return False, "이번 주 S.N.S 럭키드로우는 이미 추첨 완료되었습니다."

        cur.execute("SELECT COALESCE(balance, 0) AS balance FROM currency WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        balance = int(row["balance"] or 0) if row else 0
        if balance < EVENT_TICKET_PRICE:
            return False, f"코인이 부족합니다.\n\n필요: {coin_text(EVENT_TICKET_PRICE)}\n보유: {coin_text(balance)}"

        cur.execute("""
        INSERT OR IGNORE INTO sns_lucky_draw_entries (week_start, week_end, user_id, user_name, tickets, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """, (week_start, week_end, user_id, user_name, now_str()))
        if cur.rowcount == 0:
            return False, "이미 이번 주 S.N.S 럭키드로우에 참여했습니다.\n구매 제한: 1인 1장"

        apply_money_change(cur, user_id, user_name, -EVENT_TICKET_PRICE, "S.N.S 럭키드로우 티켓 구매", None, "S.N.S이벤트")
        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("BUY_LUCKY_DRAW_TICKET_ERROR", e)
        return False, "🎟️ 럭키드로우 구매 처리 중 문제가 생겼어요. 최근오류를 확인해 주세요."
    finally:
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
        f"현재 예상 당첨금: {coin_text(prize)}",
        "추첨/발표: 매주 토요일 21:00 자동",
        "",
        "구매: /럭키드로우구매",
    ]

    if rows:
        lines.append("")
        lines.append("참여자 목록")
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']}")

    return "\n".join(lines)


def settle_lucky_draw(settled_by="자동추첨"):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM sns_lucky_draw_results WHERE week_start = ?", (week_start,))
        if cur.fetchone():
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
            return False, "이번 주 S.N.S 럭키드로우 참여자가 없습니다."

        total_sales = len(rows) * EVENT_TICKET_PRICE
        payout_pool = EVENT_BASE_PRIZE + int(total_sales * EVENT_PAYOUT_RATE)
        burned = max(0, total_sales - int(total_sales * EVENT_PAYOUT_RATE))

        shuffled = list(rows)
        random.shuffle(shuffled)
        if len(shuffled) == 1:
            ranks = [(1, shuffled[0], payout_pool)]
        elif len(shuffled) == 2:
            ranks = [
                (1, shuffled[0], int(round(payout_pool * 0.60))),
                (2, shuffled[1], payout_pool - int(round(payout_pool * 0.60))),
            ]
        else:
            p1 = int(round(payout_pool * 0.60))
            p2 = int(round(payout_pool * 0.25))
            p3 = payout_pool - p1 - p2
            ranks = [(1, shuffled[0], p1), (2, shuffled[1], p2), (3, shuffled[2], p3)]

        main_winner = ranks[0][1]
        cur.execute("""
        INSERT INTO sns_lucky_draw_results (
            week_start, week_end, winner_user_id, winner_user_name,
            participants, total_sales, prize, burned, settled_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (week_start, week_end, main_winner["user_id"], main_winner["user_name"], len(rows), total_sales, payout_pool, burned, settled_by, now_str()))

        for rank, winner, prize in ranks:
            cur.execute("""
            INSERT INTO sns_lucky_draw_prizes (week_start, week_end, rank, winner_user_id, winner_user_name, prize, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (week_start, week_end, rank, winner["user_id"], winner["user_name"], prize, now_str()))
            apply_money_change(
                cur,
                winner["user_id"],
                winner["user_name"],
                prize,
                f"S.N.S 럭키드로우 {rank}등 {week_start}~{week_end}",
                None,
                settled_by
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("SETTLE_LUCKY_DRAW_ERROR", e)
        return False, "🎉 럭키드로우 정산 중 문제가 생겼어요. 최근오류를 확인해 주세요."
    finally:
        conn.close()

    lines = [
        "🎉 S.N.S 럭키드로우 추첨 결과", "",
        f"기간: {week_start} ~ {week_end}",
        f"참여자: {len(rows)}명",
        f"총 판매액: {coin_text(total_sales)}",
        f"기본 부스팅: {coin_text(EVENT_BASE_PRIZE)}",
        f"지급풀: {coin_text(payout_pool)}",
        f"소각: {coin_text(burned)}", "",
    ]
    for rank, winner, prize in ranks:
        lines.append(f"{rank}등 {winner['user_name']} - {coin_text(prize)}")
    return True, "\n".join(lines)


def lucky_draw_result_text():
    """최근 S.N.S 럭키드로우 추첨 결과를 조회합니다."""
    current_week_start, current_week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT week_start, week_end, participants, total_sales, prize, burned, settled_by, created_at
    FROM sns_lucky_draw_results
    WHERE week_start = ?
    """, (current_week_start,))
    row = cur.fetchone()
    if not row:
        cur.execute("""
        SELECT week_start, week_end, participants, total_sales, prize, burned, settled_by, created_at
        FROM sns_lucky_draw_results
        ORDER BY created_at DESC
        LIMIT 1
        """)
        row = cur.fetchone()
    if not row:
        conn.close()
        return "🎟️ S.N.S 럭키드로우 결과\n\n아직 추첨 결과가 없습니다.\n\n참여 현황: /럭키드로우현황\n구매: /럭키드로우구매"
    cur.execute("""
    SELECT rank, winner_user_name, prize
    FROM sns_lucky_draw_prizes
    WHERE week_start = ?
    ORDER BY rank ASC
    """, (row["week_start"],))
    prizes = cur.fetchall()
    conn.close()
    is_current = row["week_start"] == current_week_start
    title = "🎉 이번 주 S.N.S 럭키드로우 결과" if is_current else "🎉 최근 S.N.S 럭키드로우 결과"
    lines = [
        title, "",
        f"기간: {row['week_start']} ~ {row['week_end']}",
        f"참여자: {row['participants']}명",
        f"총 판매액: {coin_text(row['total_sales'])}",
        f"지급풀: {coin_text(row['prize'])}",
        f"소각: {coin_text(row['burned'])}", "",
    ]
    if prizes:
        for pr in prizes:
            lines.append(f"{pr['rank']}등 {pr['winner_user_name']} - {coin_text(pr['prize'])}")
    else:
        lines.append("당첨 상세 기록이 없습니다.")
    lines += ["", f"추첨: {row['settled_by'] or '자동추첨'}", f"추첨일: {row['created_at']}"]
    return "\n".join(lines)




def maybe_auto_lucky_draw():
    """토요일 21:00 이후 자동 럭키드로우 정산/발표.
    중복 실행은 sns_lucky_draw_results의 week_start PK로 방지합니다.
    """
    if not is_saturday_draw_time():
        return False

    ok, msg = settle_lucky_draw("토요일 21시 자동추첨")
    if not ok:
        return False

    print("[PUSH_DISABLED] SNS_LUCKY_AUTO_RESULT", msg)
    return True


def lucky_draw_auto_scheduler_loop():
    """Railway/Gunicorn 환경에서도 동작하도록 백그라운드에서 1분마다 확인합니다."""
    while True:
        try:
            maybe_auto_lucky_draw()
        except Exception as e:
            print("SNS_LUCKY_AUTO_SCHEDULER_ERROR:", e)
        time.sleep(60)


def start_lucky_draw_auto_scheduler():
    if os.getenv("DISABLE_LUCKY_DRAW_AUTO", "").strip() == "1":
        return
    thread = threading.Thread(target=lucky_draw_auto_scheduler_loop, daemon=True)
    thread.start()


# =========================
# 업적
# =========================
ACHIEVEMENT_CATALOG = [
    ("first_attendance", "✅ 첫 출석", "출석을 처음 완료", 2),
    ("first_gacha", "🎰 첫 가챠", "가챠를 처음 이용", 2),
    ("first_lucky", "🎟️ 첫 럭키드로우", "S.N.S 럭키드로우 첫 참여", 2),
    ("first_manitto", "🎭 첫 마니또", "마니또를 처음 성공", 5),
    ("truth_question_10", "난 니가 궁금해!", "진실게임 질문 10회 지목", 20),
    ("truth_answer_10", "척척박사", "진실게임 답변 10회 완료", 10),
    ("affinity_50", "💞 친밀한 시작", "한 상대와 누적 친밀도 50 달성", 2),
    ("affinity_100", "💗 가까운 사이", "한 상대와 누적 친밀도 100 달성", 5),
    ("affinity_300", "💖 단짝", "한 상대와 누적 친밀도 300 달성", 10),
    ("jagiya", "💕 자기야", "한 상대와 누적 친밀도 500 달성", 30),
    ("daily_500_chatter", "💬 수다왕", "하루 500마디 달성", 10),
    ("weekly_500_emperor", "👑 수다황제", "7일 연속 500마디 이상 달성", 50),
]

AFFINITY_ACHIEVEMENT_MILESTONES = [
    (50, "affinity_50", "💞 친밀한 시작", 2),
    (100, "affinity_100", "💗 가까운 사이", 5),
    (300, "affinity_300", "💖 단짝", 10),
]

EXCLUDED_ACHIEVEMENT_KEYS = {"".join(("boun", "ty_complete"))}


def grant_achievement_once(user_id, user_name, achievement_key, achievement_name, reward=0, meta=""):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT OR IGNORE INTO achievements (
            user_id, user_name, achievement_key, achievement_name, reward, meta, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, user_name, achievement_key, achievement_name, reward, meta, now_str()))
        inserted = cur.rowcount

        if inserted and reward > 0:
            apply_money_change(cur, user_id, user_name, reward, f"업적 보상: {achievement_name}", None, "업적시스템")

        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("ACHIEVEMENT_GRANT_ERROR", e)
        inserted = 0
    finally:
        conn.close()

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
    rows = [
        row for row in get_user_achievements(user_id)
        if row["achievement_key"] not in EXCLUDED_ACHIEVEMENT_KEYS
    ]
    owned = {row["achievement_key"] for row in rows}

    dynamic = []
    for key, info in PIECE_INFO.items():
        dynamic.append((f"blacksmith_{key}", f"🔨 대장장이: {piece_item_name(info)}", f"{info['label']} 최초 완성", 20))

    catalog = ACHIEVEMENT_CATALOG + dynamic
    lines = [
        "🎖 업적 현황",
        f"대상: {user_name}",
        "",
        f"완료: {len(rows)}개",
        "",
    ]
    catalog_keys = {key for key, _, _, _ in catalog}
    for key, name, desc, reward in catalog:
        mark = "✅" if key in owned else "⬜"
        lines.append(f"{mark} {name}")
        lines.append(f"   {desc} / 보상 {coin_text(reward)}")

    extra_rows = [row for row in rows if row["achievement_key"] not in catalog_keys]
    if extra_rows:
        lines += ["", "━━━━━━━━━━", "추가 달성 업적", "━━━━━━━━━━"]
        for row in extra_rows:
            lines.append(f"✅ {row['achievement_name']}")
            lines.append(f"   보상 {coin_text(row['reward'])}")

    return "\n".join(lines)




def count_500_madi_streak(user_id, date_str, source_id):
    """
    date_str 기준 오늘 포함 연속 500마디 이상 달성일 계산.
    counts 테이블의 일자별 마디수를 기준으로 합니다.
    """
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        base = datetime.now(KST).date()

    conn = db()
    cur = conn.cursor()

    streak = 0
    day = base

    while True:
        d = day.strftime("%Y-%m-%d")
        cur.execute("""
        SELECT COALESCE(count, 0) AS count
        FROM counts
        WHERE date = ?
          AND source_id = ?
          AND user_id = ?
        """, (d, source_id, user_id))
        row = cur.fetchone()

        if row and int(row["count"] or 0) >= 500:
            streak += 1
            day -= timedelta(days=1)
        else:
            break

    conn.close()
    return streak


def check_chatter_achievements(date_str, source_id, user_id, user_name):
    """
    마디수 기반 업적 자동 지급.
    - 💬 수다왕: 하루 500마디 달성, 최초 1회, 1코인
    - 👑 수다황제: 7일 연속 500마디 이상, 최초 1회, 5코인
    """
    if source_id != COUNT_SOURCE_ID:
        return []

    current_count = get_user_count(date_str, source_id, user_id)
    granted = []

    if current_count >= 500:
        if grant_achievement_once(
            user_id,
            user_name,
            "daily_500_chatter",
            "💬 수다왕",
            10,
            f"date={date_str};count={current_count}"
        ):
            granted.append(("💬 수다왕", 10))

        streak = count_500_madi_streak(user_id, date_str, source_id)
        if streak >= 7:
            if grant_achievement_once(
                user_id,
                user_name,
                "weekly_500_emperor",
                "👑 수다황제",
                50,
                f"date={date_str};streak={streak};count={current_count}"
            ):
                granted.append(("👑 수다황제", 50))

    return granted


# =========================
# 인기인 / 설렘픽 / 케미 / 진실게임
# =========================
MENTION_SUFFIXES = (
    "님", "아", "야", "이", "가", "은", "는", "을", "를",
    "랑", "하고", "한테", "에게", "도", "만", "ㅋㅋ", "ㅎㅎ",
)

TRUTH_GAME_COST = 2       # 0.2코인
TRUTH_QUESTION_ACHIEVEMENT_REQUIRED = 10
TRUTH_QUESTION_ACHIEVEMENT_REWARD = 20  # 2코인
TRUTH_ANSWER_ACHIEVEMENT_REQUIRED = 10
TRUTH_ANSWER_ACHIEVEMENT_REWARD = 10    # 1코인
TRUTH_GAME_DIFFICULTIES = ("순한맛", "썸맛", "매운맛", "위험맛")
TRUTH_GAME_QUESTIONS = [
    ("순한맛", "요즘 공창에서 제일 반가운 사람은?"),
    ("순한맛", "더 친해지고 싶은 사람은?"),
    ("순한맛", "하루 종일 갠라해도 안 질릴 것 같은 사람은?"),
    ("순한맛", "벙 가면 제일 먼저 찾게 될 것 같은 사람은?"),
    ("순한맛", "최근 가장 인상이 좋아진 사람은?"),
    ("순한맛", "공창 분위기를 제일 좋게 만드는 사람은?"),
    ("순한맛", "가장 많이 웃게 해준 사람은?"),
    ("순한맛", "지금 가장 궁금한 사람은?"),
    ("순한맛", "처음엔 의외였는데 점점 괜찮아 보인 사람은?"),
    ("순한맛", "같이 술 한잔 해보고 싶은 사람은?"),
    ("순한맛", "이성에게 들었던 칭찬 중 가장 심쿵했던 말은 무엇인가요?"),
    ("순한맛", "내가 생각하는 나의 가장 매력적인 신체 부위는 어디인가요?"),
    ("순한맛", "상대를 볼 때 내가 생각하는 최고의 미덕은? (ex. 철저한 비밀 유지, 깔끔한 뒤끝, 완벽한 속궁합 등)"),
    ("순한맛", '상대에게서 "이것만큼은 절대 용납 못 한다" 하는 나만의 칼 차단 기준은 무엇인가요?'),

    ("썸맛", "요즘 가장 눈이 가는 사람은?"),
    ("썸맛", "DM이 오면 은근 반가울 것 같은 사람은?"),
    ("썸맛", "최근 가장 신경 쓰이는 사람은?"),
    ("썸맛", "공창에서 안 보이면 아쉬울 것 같은 사람은?"),
    ("썸맛", "단둘이 더 이야기해보고 싶은 사람은?"),
    ("썸맛", "처음보다 훨씬 매력적으로 보이는 사람은?"),
    ("썸맛", "벙에서 옆자리에 앉고 싶은 사람은?"),
    ("썸맛", "연락이 오면 가장 먼저 확인할 것 같은 사람은?"),
    ("썸맛", "지금 가장 플러팅 받아보고 싶은 사람은?"),
    ("썸맛", "같이 밤새 이야기해도 재밌을 것 같은 사람은?"),
    ("썸맛", "방 멤버 중 같이 여행 가보고 싶은 사람?(이성)"),
    ("썸맛", "한 번쯤 더 알아가고 싶다고 느낀 사람은?"),
    ("썸맛", '이성이 나한테 하면 "어? 나한테 관심 있나?" 하고 착각하게 만드는 행동은?'),
    ("썸맛", "지금 이 방에서 본캐(현생)와 부캐(밤의 세계)의 갭 차이가 가장 심할 것 같은 반전 매력의 사람은 누구?"),
    ("썸맛", "대화방에서 어떤 닉네임이 말할 때 자꾸 신경 쓰이거나, 왠지 나랑 밤 코드가 잘 맞을 것 같다는 느낌이 드나요?"),
    ("썸맛", '상대방이 나에게 던진 대화나 텍스트 중 "어? 이거 나 꼬시는 건가?" 하고 본능적으로 신호가 왔던 순간은?'),
    ("썸맛", "오직 밤을 위한 파트너를 구할 때, 나는 ‘내가 먼저 적극적으로 들이댄다’ vs ‘상대가 제안해 오도록 유도한다?’"),
    ("썸맛", '벙 약속을 잡기 전, "이 정도 수위의 대화나 사진 교환까지는 끝나야 만난다" 하는 마지노선은?'),
    ("썸맛", "내가 마음에 드는 사람에게만 은밀하게 흘리는 나만의 온라인 플러팅 방식이나 멘트가 있다면?"),
    ("썸맛", "상대방이 엄청난 테크니션(밤 기술자)인데 외모가 내 취향이 아님 vs 외모는 역대급 내 이상형인데 밤 기술이 완전 뚝딱이, 나의 선택은?"),
    ("썸맛", "지금 이 순간 같이 드라이브 가고 싶은 사람은?"),
    ("썸맛", "솔직히 한 번쯤 갠라를 고민해본 사람은?"),
    ("썸맛", "단둘이 술 마시면 재밌을 것 같은 사람은?"),
    ("썸맛", "외모를 떠나서 분위기가 끌리는 사람은?"),
    ("썸맛", "벙에서 가장 먼저 눈에 들어올 것 같은 사람은?"),
    ("썸맛", "요즘 묘하게 궁금한 사람은?"),
    ("썸맛", "최근 가장 기억에 남는 사람은?"),
    ("썸맛", "최근 가장 의식하게 된 사람은?"),
    ("썸맛", "솔직히 갠라 오면 거절 안 할 것 같은 사람은?"),
    ("썸맛", "지금 가장 연락 오길 기다리는 사람은?"),
    ("썸맛", "최근 가장 심쿵했던 사람은?"),

    ("매운맛", "첫 만남에 스킨십은 여기까지 가능하다 하는 나만의 마지노선은?"),
    ("매운맛", "최근 가장 설렌 순간을 만든 사람은?"),
    ("매운맛", "지금 가장 보고 싶은 사람은?"),
    ("매운맛", "공창에서 가장 매력 있다고 생각하는 사람은?"),
    ("매운맛", "은근 질투난 적 있는 사람은?"),
    ("매운맛", "공창에서 가장 플러팅 잘한다고 생각하는 사람은?"),
    ("매운맛", "방에서 가장 위험한 매력을 가진 사람은?"),
    ("매운맛", '지금 당장 누군가 나에게 100만 원을 주면서 "여기서 가장 마음에 드는 사람 번호 따와"라고 한다면, 망설임 없이 다가갈 대상은?'),
    ("매운맛", "내가 받아본 섹스어필 중 ‘이건 치트키였다‘싶었던 스킬이나 상황은?"),
    ("매운맛", "상대에게 해 본 섹스어필 중 ‘이건 치트키였다‘싶었던 스킬이나 상황은?"),
    ("매운맛", "만약 오늘 단둘이 섹벙을 나간다면 누구를 고를 건가요?"),
    ("매운맛", "성격 다 제외하고 오직 목소리나 말투만 들었을 때 밤에 가장 섹시할 것 같은 사람은 누구인가요?"),
    ("매운맛", "불을 켜고 하는 것을 선호하나요, 아니면 완전히 끄고(혹은 무드등만) 하는 것을 선호하나요?"),
    ("매운맛", "꿈(길몽/흉몽 제외) 중에서 이성이 나와서 했던 가장 야릇하거나 수위 높았던 꿈의 내용은?"),
    ("매운맛", "데이트 도중 이성의 **어떤 은밀한 터치(ex. 은근슬쩍 허벅지 쓸기, 손바닥 간지럽히기 등)**에 가장 쉽게 무너지나요?"),
    ("매운맛", '상대의 은밀한 페티시나 독특한 밤의 취향 중 "이것까지는 기분 좋게 맞춰줄 수 있다" 하는 것은?'),
    ("매운맛", "키스하거나 격하게 스킨십할 때, 내 손은 보통 상대방의 어디에 가 있나요?"),
    ("매운맛", "섹스 중, 가장 팍 식게 만드는 최악의 행동은?"),
    ("매운맛", "이성을 볼 때 가장 먼저 은밀하게 눈이 가는 신체 부위는?"),
    ("매운맛", "나는 '낮이밤이', '낮이밤져', '낮져밤이', '낮져밤져' 중 어디에 해당하나요?"),

    ("위험맛", "오늘 하루 같이 보내라면 누구를 고를 건가요?"),
    ("위험맛", "방에서 쓰리썸 한다면 함께 하고 싶은 사람 두명은?"),
    ("위험맛", "지금 바로 옆에 있는 사람과 섹스를 해야만 한다면, 옆에 누가 있으면 좋겠나요? (방멤버한정)"),
    ("위험맛", "본인이 해본 가장 자극적인 섹스 장소는?"),
    ("위험맛", "해본 체위 중 가장 난이도 높았던 체위는?"),
    ("위험맛", "지금 이 순간 애무를 당한다면 고를 신체부위 한 군데?"),
    ("위험맛", "만약 내 파트너에게 내 친구를 소개시켜줘야한다면, 방 멤버중 절대 소개 안시켜줄 것 같은 사람은?"),
    ("위험맛", "‘섹스할 때 잘 맞을 것 같다’고 생각되는 사람은?"),
    ("위험맛", "‘이 사람은 의외로 침대 위에서 되게 리드미컬하고 화끈할 것 같다’ 싶은 반전 이미지는 누구?"),
    ("위험맛", "여기에 키스(또는 애무) 받는 게 가장 짜릿하다 하는 나만의 성감대는 어디?"),
    ("위험맛", "소리를 자연스럽게 내는 편인가요, 아니면 참으려고 노력하는 편인가요?"),
    ("위험맛", "내가 생각하는 나의 밤 기술(테크닉) 점수는 10점 만점에 몇 점인가요?"),
]


def medal_icon(rank):
    if rank == 1:
        return "🥇"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return f"{rank}."


def mentioned_target_in_text(target_name, text_value):
    target = normalize_mention_name(target_name)
    if len(target) < 2:
        return False

    if len(target) <= 2:
        for token in nickname_tokens(text_value):
            if token == target:
                return True
            if token.startswith(target):
                suffix = token[len(target):]
                if any(suffix.startswith(item) for item in MENTION_SUFFIXES):
                    return True
        return False

    return target in normalize_match_text(text_value)


def process_mentions(date_str, source_id, sender_user_id, sender_user_name, text_value):
    try:
        text_value = str(text_value or "").strip()
        if not sender_user_id or not text_value:
            return 0
        if text_value.startswith("/"):
            return 0
        if is_bot_jackpot_user(sender_user_id, sender_user_name):
            return 0

        week_start, _ = event_week_key()
        targets = []
        seen = set()
        for row in active_user_rows_for_matching():
            target_user_id = row["user_id"]
            if target_user_id == sender_user_id or target_user_id in seen:
                continue
            if mentioned_target_in_text(row["user_name"], text_value):
                targets.append(row)
                seen.add(target_user_id)

        if not targets:
            return 0

        conn = db()
        cur = conn.cursor()
        for row in targets:
            cur.execute("""
            INSERT INTO mention_logs (
                date, week_start, source_id,
                sender_user_id, sender_user_name,
                target_user_id, target_user_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str, week_start, source_id,
                sender_user_id, sender_user_name,
                row["user_id"], row["user_name"], now_str()
            ))
        conn.commit()
        conn.close()
        return len(targets)
    except Exception as e:
        print("PROCESS_MENTIONS_ERROR:", repr(e))
        return 0


def mention_period_filter(period, date_str):
    if period == "weekly":
        week_start, week_end = event_week_key()
        return "m.week_start = ?", [week_start], f"기간: {week_start} ~ {week_end}"
    return "m.date = ?", [date_str], f"날짜: {date_str}"


def popular_mentions_text(date_str, source_id, period="daily"):
    try:
        where_sql, where_params, period_line = mention_period_filter(period, date_str)
        title = "👑 이번 주 인기인" if period == "weekly" else "👑 오늘의 인기인"

        conn = db()
        cur = conn.cursor()
        cur.execute(f"""
        SELECT
            m.target_user_id,
            u.user_name AS target_user_name,
            COUNT(*) AS mention_count
        FROM mention_logs m
        JOIN users u
          ON u.user_id = m.target_user_id
        LEFT JOIN deleted_users d
          ON d.original_user_id = m.target_user_id
        WHERE {where_sql}
          AND m.source_id = ?
          AND COALESCE(u.is_active, 1) = 1
          AND d.original_user_id IS NULL
        GROUP BY m.target_user_id
        ORDER BY mention_count DESC, target_user_name ASC
        LIMIT 10
        """, (*where_params, source_id))
        rows = cur.fetchall()

        cur.execute(f"""
        SELECT
            su.user_name AS sender_user_name,
            tu.user_name AS target_user_name,
            COUNT(*) AS mention_count
        FROM mention_logs m
        JOIN users su
          ON su.user_id = m.sender_user_id
        JOIN users tu
          ON tu.user_id = m.target_user_id
        LEFT JOIN deleted_users sd
          ON sd.original_user_id = m.sender_user_id
        LEFT JOIN deleted_users td
          ON td.original_user_id = m.target_user_id
        WHERE {where_sql}
          AND m.source_id = ?
          AND COALESCE(su.is_active, 1) = 1
          AND COALESCE(tu.is_active, 1) = 1
          AND sd.original_user_id IS NULL
          AND td.original_user_id IS NULL
        GROUP BY m.sender_user_id, m.target_user_id
        ORDER BY mention_count DESC, sender_user_name ASC, target_user_name ASC
        LIMIT 1
        """, (*where_params, source_id))
        combo = cur.fetchone()
        conn.close()

        lines = [title, period_line, ""]
        if not rows:
            lines.append("아직 언급 기록이 없습니다.")
            return "\n".join(lines)

        for i, row in enumerate(rows, 1):
            lines.append(f"{medal_icon(i)} {display_nickname(row['target_user_name'])} {row['mention_count']}회")

        lines += ["", "오늘 가장 많이 언급한 조합:" if period != "weekly" else "이번 주 가장 많이 언급한 조합:"]
        if combo:
            lines.append(
                f"{display_nickname(combo['sender_user_name'])} → "
                f"{display_nickname(combo['target_user_name'])} {combo['mention_count']}회"
            )
        else:
            lines.append("-")
        return "\n".join(lines)
    except Exception as e:
        print("POPULAR_MENTIONS_TEXT_ERROR:", repr(e))
        return "👑 인기인 조회 중 오류가 발생했습니다."


def mention_ranking_text(keyword, date_str, source_id):
    try:
        target, err = resolve_active_user_by_nickname(keyword, purpose="유저")
        if err:
            return "👑 언급랭킹 조회 실패\n\n" + err

        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT
            u.user_name AS target_user_name,
            COUNT(*) AS mention_count
        FROM mention_logs m
        JOIN users u
          ON u.user_id = m.target_user_id
        LEFT JOIN deleted_users d
          ON d.original_user_id = m.target_user_id
        WHERE m.date = ?
          AND m.source_id = ?
          AND m.sender_user_id = ?
          AND COALESCE(u.is_active, 1) = 1
          AND d.original_user_id IS NULL
        GROUP BY m.target_user_id
        ORDER BY mention_count DESC, target_user_name ASC
        LIMIT 10
        """, (date_str, source_id, target["user_id"]))
        rows = cur.fetchall()
        conn.close()

        lines = [
            "👑 언급랭킹",
            f"대상: {display_nickname(target['user_name'])}",
            f"날짜: {date_str}",
            "",
        ]
        if not rows:
            lines.append("오늘 언급한 기록이 없습니다.")
        else:
            for i, row in enumerate(rows, 1):
                lines.append(f"{medal_icon(i)} {display_nickname(row['target_user_name'])} {row['mention_count']}회")
        return "\n".join(lines)
    except Exception as e:
        print("MENTION_RANKING_TEXT_ERROR:", repr(e))
        return "👑 언급랭킹 조회 중 오류가 발생했습니다."


HEART_PICK_REWARDS = {
    1: 20,  # 2코인
    2: 10,  # 1코인
    3: 5,   # 0.5코인
}


def heart_pick_reward_amount(rank):
    return HEART_PICK_REWARDS.get(int(rank), 0)


def heart_pick(sender_user_id, sender_user_name, target_keyword, announce_public=False):
    try:
        if not sender_user_id:
            return "💘 설렘픽 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        target_keyword = str(target_keyword or "").strip()
        if not target_keyword:
            return "💘 설렘픽 안내\n\n사용법: /설렘 닉네임"

        target, err = resolve_active_user_by_nickname(target_keyword, purpose="대상")
        if err:
            return "💘 설렘픽 안내\n\n" + err
        if target["user_id"] == sender_user_id:
            return "💘 설렘픽 안내\n\n자기 자신에게는 투표할 수 없어요."

        ok_gender, gender_err = opposite_gender_check(sender_user_id, sender_user_name, target)
        if not ok_gender:
            return "💘 설렘픽 안내\n\n" + gender_err

        date_str = today()
        week_start, _ = event_week_key()
        conn = db()
        cur = conn.cursor()

        cur.execute("""
        SELECT id
        FROM heart_picks
        WHERE date = ? AND sender_user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """, (date_str, sender_user_id))
        if cur.fetchone():
            conn.close()
            return "💘 설렘픽 안내\n\n설렘픽은 하루에 한 번만 가능해요."

        cur.execute("""
        INSERT INTO heart_picks (
            date, week_start,
            sender_user_id, sender_user_name,
            target_user_id, target_user_name,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str, week_start,
            sender_user_id, sender_user_name,
            target["user_id"], target["user_name"],
            now_str()
        ))
        conn.commit()
        conn.close()

        public_message = "누군가가 설렘픽 투표를 했습니다."
        if not announce_public:
            return public_message

        ok = queue_public_announcement(COUNT_SOURCE_ID, public_message, "heart_pick")
        if ok:
            return (
                "💘 설렘픽 완료\n\n"
                "투표가 기록되었습니다.\n"
                "다음 공창 메시지에 익명 알림이 표시됩니다."
            )

        return (
            "💘 설렘픽 완료\n\n"
            "투표는 기록되었어요.\n"
            "다만 공창 알림 예약은 잠시 실패했어요."
        )
    except Exception as e:
        print("HEART_PICK_ERROR:", repr(e))
        return "💘 설렘픽 처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def heart_pick_status_text(user_id, user_name):
    try:
        if not user_id:
            return "💘 설렘픽 현황 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        date_str = today()
        week_start, week_end = event_week_key()
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT target_user_name, created_at
        FROM heart_picks
        WHERE date = ? AND sender_user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """, (date_str, user_id))
        sent = cur.fetchone()

        cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM heart_picks
        WHERE week_start = ? AND target_user_id = ?
        """, (week_start, user_id))
        received = cur.fetchone()
        conn.close()

        lines = [
            "💘 설렘픽 현황",
            f"대상: {user_name}",
            f"기간: {week_start} ~ {week_end}",
            "",
        ]
        if sent:
            lines.append(f"오늘 투표: 완료 ({display_nickname(sent['target_user_name'])}님)")
        else:
            lines.append("오늘 투표: 아직 사용하지 않음")
        lines.append(f"이번 주 받은 설렘픽: {int(received['cnt'] or 0) if received else 0}표")
        return "\n".join(lines)
    except Exception as e:
        print("HEART_PICK_STATUS_ERROR:", repr(e))
        return "💘 설렘픽 현황을 불러오는 중 문제가 생겼어요."


def heart_pick_ranking_rows(week_start=None, limit=10):
    week_start = week_start or event_week_key()[0]
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        p.target_user_id AS user_id,
        tu.user_name,
        COUNT(*) AS pick_count
    FROM heart_picks p
    JOIN users tu
      ON tu.user_id = p.target_user_id
    JOIN users su
      ON su.user_id = p.sender_user_id
    LEFT JOIN deleted_users td
      ON td.original_user_id = p.target_user_id
    LEFT JOIN deleted_users sd
      ON sd.original_user_id = p.sender_user_id
    WHERE p.week_start = ?
      AND COALESCE(tu.is_active, 1) = 1
      AND COALESCE(su.is_active, 1) = 1
      AND td.original_user_id IS NULL
      AND sd.original_user_id IS NULL
    GROUP BY p.target_user_id
    ORDER BY pick_count DESC, tu.user_name ASC
    LIMIT ?
    """, (week_start, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def heart_pick_ranking_text():
    try:
        week_start, week_end = event_week_key()
        rows = heart_pick_ranking_rows(week_start, limit=10)

        lines = [
            "💘 이번주 설렘픽 랭킹",
            f"기간: {week_start} ~ {week_end}",
            "보상: 1등 2코인 / 2등 1코인 / 3등 0.5코인",
            "",
        ]
        if not rows:
            lines.append("아직 설렘픽 기록이 없습니다.")
        else:
            for i, row in enumerate(rows, 1):
                reward = heart_pick_reward_amount(i)
                reward_text = f" / 보상 {coin_text(reward)}" if reward > 0 else ""
                lines.append(f"{medal_icon(i)} {display_nickname(row['user_name'])} {row['pick_count']}표{reward_text}")
        return "\n".join(lines)
    except Exception as e:
        print("HEART_PICK_RANKING_ERROR:", repr(e))
        return "💘 설렘픽 랭킹 조회 중 오류가 발생했습니다."


def settle_heart_pick_rewards(week_start=None, week_end=None):
    if not week_start or not week_end:
        week_start, week_end = event_week_key()
    rows = heart_pick_ranking_rows(week_start, limit=3)
    paid = []

    conn = db()
    cur = conn.cursor()
    try:
        for idx, row in enumerate(rows, 1):
            reward = heart_pick_reward_amount(idx)
            if reward <= 0:
                continue
            cur.execute("""
            INSERT OR IGNORE INTO heart_pick_rewards (
                week_start, week_end, user_id, user_name,
                rank, pick_count, reward, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                week_start,
                week_end,
                row["user_id"],
                row["user_name"],
                idx,
                row["pick_count"],
                reward,
                now_str()
            ))
            if cur.rowcount > 0:
                apply_money_change(
                    cur,
                    row["user_id"],
                    row["user_name"],
                    reward,
                    f"설렘픽 랭킹 보상 {week_start}~{week_end} {idx}위",
                    None,
                    "설렘픽"
                )
                paid.append({
                    "rank": idx,
                    "user_id": row["user_id"],
                    "user_name": row["user_name"],
                    "pick_count": row["pick_count"],
                    "reward": reward,
                })
        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("SETTLE_HEART_PICK_REWARDS_ERROR", e)
        paid = []
    finally:
        conn.close()

    return paid


def heart_pick_settlement_text():
    try:
        week_start, week_end = event_week_key()
        paid = settle_heart_pick_rewards(week_start, week_end)
        if not paid:
            return (
                "💘 설렘픽 정산\n\n"
                f"기간: {week_start} ~ {week_end}\n"
                "새로 지급할 설렘픽 보상이 없습니다.\n"
                "이미 정산했거나 랭킹 데이터가 없습니다."
            )

        lines = ["💘 설렘픽 정산 완료", f"기간: {week_start} ~ {week_end}", ""]
        for item in paid:
            lines.append(
                f"{item['rank']}위 {display_nickname(item['user_name'])} "
                f"{item['pick_count']}표 / {coin_text(item['reward'])}"
            )
        return "\n".join(lines)
    except Exception as e:
        print("HEART_PICK_SETTLEMENT_ERROR:", repr(e))
        return "💘 설렘픽 정산 중 오류가 발생했습니다."


def reset_today_heart_picks(date_str=None):
    date_str = date_str or today()
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM heart_picks WHERE date = ?", (date_str,))
        deleted = cur.rowcount
        cur.execute("""
        DELETE FROM public_announcements
        WHERE category = 'heart_pick'
          AND delivered_at IS NULL
          AND substr(created_at, 1, 10) = ?
        """, (date_str,))
        queued_deleted = cur.rowcount
        conn.commit()
        conn.close()
        return (
            "💘 설렘픽 초기화 완료\n\n"
            f"기준일: {date_str}\n"
            f"삭제된 투표: {deleted}건\n"
            f"삭제된 대기 알림: {queued_deleted}건\n\n"
            "오늘 설렘픽 횟수가 초기화되었습니다."
        )
    except Exception as e:
        print("HEART_PICK_RESET_ERROR:", repr(e))
        return "💘 설렘픽 초기화 중 오류가 발생했습니다."


def chemistry_pair_key(user_a, user_b):
    a, b = sorted([str(user_a), str(user_b)])
    return f"{a}:{b}"


CHEMISTRY_FIRST_MATCH_REWARD = 10   # 1코인
CHEMISTRY_REPEAT_MATCH_REWARD = 2   # 0.2코인


def chemistry_reward_amount_for_user(cur, user_id):
    cur.execute("""
    SELECT COUNT(*) AS cnt
    FROM chemistry_rewards
    WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    return CHEMISTRY_FIRST_MATCH_REWARD if int(row["cnt"] or 0) == 0 else CHEMISTRY_REPEAT_MATCH_REWARD


def grant_chemistry_match_rewards(date_str, week_start, sender, target):
    """
    케미 매칭 성사 시 양쪽 모두에게 보상 지급.
    각 유저 기준 최초 매칭은 1코인, 이후 매칭은 0.2코인입니다.
    """
    paid = []
    conn = None
    try:
        conn = db()
        cur = conn.cursor()
        pairs = [
            (sender["user_id"], sender["user_name"], target["user_id"], target["user_name"]),
            (target["user_id"], target["user_name"], sender["user_id"], sender["user_name"]),
        ]

        for user_id, user_name, matched_user_id, matched_user_name in pairs:
            reward = chemistry_reward_amount_for_user(cur, user_id)
            cur.execute("""
            INSERT OR IGNORE INTO chemistry_rewards (
                date, week_start,
                user_id, user_name,
                matched_user_id, matched_user_name,
                reward, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str, week_start,
                user_id, user_name,
                matched_user_id, matched_user_name,
                reward, now_str()
            ))
            if cur.rowcount > 0:
                apply_money_change(
                    cur,
                    user_id,
                    user_name,
                    reward,
                    f"케미 매칭 보상 {date_str}",
                    None,
                    "케미"
                )
                paid.append({
                    "user_id": user_id,
                    "user_name": user_name,
                    "reward": reward,
                })

        conn.commit()
    except Exception as e:
        print("CHEMISTRY_REWARD_RECORD_ERROR:", repr(e))
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        paid = []
    finally:
        if conn:
            conn.close()

    return paid


def chemistry_reward_line_for_user(paid, user_id):
    for item in paid:
        if item["user_id"] == user_id:
            return f"\n보상: +{coin_text(item['reward'])}"
    return "\n보상: 이미 지급 완료"


def chemistry_signal(sender_user_id, sender_user_name, target_keyword, announce_public=False):
    try:
        if not sender_user_id:
            return "💞 케미 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        target_keyword = str(target_keyword or "").strip()
        if not target_keyword:
            return "💞 케미 안내\n\n사용법: /케미 닉네임"

        target, err = resolve_active_user_by_nickname(target_keyword, purpose="대상")
        if err:
            return "💞 케미 안내\n\n" + err
        if target["user_id"] == sender_user_id:
            return "💞 케미 안내\n\n자기 자신에게는 케미를 보낼 수 없어요."

        ok_gender, gender_err = opposite_gender_check(sender_user_id, sender_user_name, target)
        if not ok_gender:
            return "💞 케미 안내\n\n" + gender_err

        date_str = today()
        week_start, _ = event_week_key()
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT id
        FROM chemistry_signals
        WHERE date = ? AND sender_user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """, (date_str, sender_user_id))
        if cur.fetchone():
            conn.close()
            return "💞 케미 안내\n\n케미는 하루에 한 번만 가능해요."

        cur.execute("""
        INSERT INTO chemistry_signals (
            date, week_start,
            sender_user_id, sender_user_name,
            target_user_id, target_user_name,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str, week_start,
            sender_user_id, sender_user_name,
            target["user_id"], target["user_name"],
            now_str()
        ))

        cur.execute("""
        SELECT id
        FROM chemistry_signals
        WHERE date = ?
          AND sender_user_id = ?
          AND target_user_id = ?
        LIMIT 1
        """, (date_str, target["user_id"], sender_user_id))
        mutual = cur.fetchone() is not None
        conn.commit()
        conn.close()

        if not mutual:
            return (
                "💞 케미 완료\n\n"
                "케미가 기록되었습니다.\n"
                "상대도 오늘 케미를 보낸 경우에만 매칭 알림이 표시됩니다."
            )

        sender_info = {"user_id": sender_user_id, "user_name": sender_user_name}
        reward_paid = grant_chemistry_match_rewards(date_str, week_start, sender_info, target)
        reward_line = chemistry_reward_line_for_user(reward_paid, sender_user_id)

        flag_key = f"chemistry_mutual_announced:{date_str}:{chemistry_pair_key(sender_user_id, target['user_id'])}"
        if get_system_flag(flag_key):
            return (
                "💞 케미 매칭 성공\n\n"
                "당신이 원하는 케미가 이루어졌습니다.\n"
                "오늘 이미 공개 알림이 올라간 조합입니다."
                f"{reward_line}"
            )

        public_message = "💞 케미 매칭 성사!\n\n누군가의 케미 매칭이 성사되었습니다."
        if announce_public and queue_public_announcement(COUNT_SOURCE_ID, public_message, "chemistry_mutual"):
            set_system_flag(flag_key, "1")
            return (
                "💞 케미 매칭 성공\n\n"
                "당신이 원하는 케미가 이루어졌습니다.\n"
                "공개창에는 익명 매칭 알림만 표시됩니다."
                f"{reward_line}"
            )

        return (
            "💞 케미 매칭 성공\n\n"
            "당신이 원하는 케미가 이루어졌습니다.\n"
            "다만 공개창 알림 예약은 잠시 실패했어요."
            f"{reward_line}"
        )
    except Exception as e:
        print("CHEMISTRY_SIGNAL_ERROR:", repr(e))
        return "💞 케미 처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def mutual_chemistry_report_text():
    try:
        date_str = today()
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT
            a.sender_user_id AS user_a,
            au.user_name AS user_a_name,
            a.target_user_id AS user_b,
            bu.user_name AS user_b_name,
            MAX(a.created_at) AS user_a_at,
            MAX(b.created_at) AS user_b_at
        FROM chemistry_signals a
        JOIN chemistry_signals b
          ON b.week_start = a.week_start
         AND b.sender_user_id = a.target_user_id
         AND b.target_user_id = a.sender_user_id
        JOIN users au
          ON au.user_id = a.sender_user_id
        JOIN users bu
          ON bu.user_id = a.target_user_id
        LEFT JOIN deleted_users ad
          ON ad.original_user_id = a.sender_user_id
        LEFT JOIN deleted_users bd
          ON bd.original_user_id = a.target_user_id
        WHERE a.date = ?
          AND b.date = a.date
          AND a.sender_user_id < a.target_user_id
          AND COALESCE(au.is_active, 1) = 1
          AND COALESCE(bu.is_active, 1) = 1
          AND ad.original_user_id IS NULL
          AND bd.original_user_id IS NULL
        GROUP BY a.sender_user_id, a.target_user_id
        ORDER BY user_a_at DESC, user_b_at DESC
        """, (date_str,))
        rows = cur.fetchall()
        conn.close()

        lines = ["💞 오늘 쌍방 케미 확인", f"기준일: {date_str}", ""]
        if not rows:
            lines.append("오늘 쌍방 케미가 없습니다.")
        else:
            for i, row in enumerate(rows, 1):
                lines.append(
                    f"{i}. {display_nickname(row['user_a_name'])} ↔ "
                    f"{display_nickname(row['user_b_name'])}"
                )
        return "\n".join(lines)
    except Exception as e:
        print("MUTUAL_CHEMISTRY_REPORT_ERROR:", repr(e))
        return "💞 쌍방 케미 확인 중 오류가 발생했습니다."


def personal_chemistry_check_text(user_id):
    try:
        if not user_id:
            return "💞 케미확인 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        date_str = today()
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT target_user_id, created_at
        FROM chemistry_signals
        WHERE date = ?
          AND sender_user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """, (date_str, user_id))
        sent = cur.fetchone()

        cur.execute("""
        SELECT COUNT(DISTINCT sender_user_id) AS cnt
        FROM chemistry_signals
        WHERE date = ?
          AND target_user_id = ?
          AND sender_user_id != ?
        """, (date_str, user_id, user_id))
        received = cur.fetchone()
        received_count = int(received["cnt"] or 0) if received else 0

        cur.execute("""
        SELECT reward
        FROM chemistry_rewards
        WHERE date = ?
          AND user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """, (date_str, user_id))
        reward_row = cur.fetchone()
        reward_line = f"오늘 케미 보상: {coin_text(reward_row['reward'])}\n" if reward_row else ""

        if not sent:
            conn.close()
            return (
                "💞 케미확인\n\n"
                f"기준일: {date_str}\n"
                "내가 보낸 케미: 아직 참여하지 않음\n"
                f"나에게 온 케미 요청: {received_count}명\n\n"
                f"{reward_line}"
                "꽃봇 1:1에서 /케미 닉네임 을 입력해 참여할 수 있습니다."
            )

        cur.execute("""
        SELECT id
        FROM chemistry_signals
        WHERE date = ?
          AND sender_user_id = ?
          AND target_user_id = ?
        LIMIT 1
        """, (date_str, sent["target_user_id"], user_id))
        matched = cur.fetchone() is not None
        conn.close()

        if matched:
            return (
                "💞 케미확인\n\n"
                f"기준일: {date_str}\n"
                "내가 보낸 케미: 매칭 성공\n"
                f"나에게 온 케미 요청: {received_count}명\n\n"
                f"{reward_line}"
                "당신이 원하는 케미가 이루어졌습니다."
            )

        return (
            "💞 케미확인\n\n"
            f"기준일: {date_str}\n"
            "내가 보낸 케미: 아직 미성공\n"
            f"나에게 온 케미 요청: {received_count}명\n\n"
            f"{reward_line}"
            "상대도 오늘 같은 케미를 보내면 매칭됩니다."
        )
    except Exception as e:
        print("PERSONAL_CHEMISTRY_CHECK_ERROR:", repr(e))
        return "💞 케미확인을 불러오는 중 문제가 생겼어요."


def get_pending_truth_game(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT *
    FROM truth_game_sessions
    WHERE user_id = ? AND status = 'pending'
    ORDER BY id DESC
    LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_pending_truth_game_by_requester(requester_user_id, target_user_id=None):
    conn = db()
    cur = conn.cursor()
    if target_user_id:
        cur.execute("""
        SELECT *
        FROM truth_game_sessions
        WHERE requester_user_id = ?
          AND user_id = ?
          AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """, (requester_user_id, target_user_id))
    else:
        cur.execute("""
        SELECT *
        FROM truth_game_sessions
        WHERE requester_user_id = ?
          AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """, (requester_user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def truth_game_question_count(user_id):
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM truth_game_sessions
        WHERE requester_user_id = ?
        """, (user_id,))
        row = cur.fetchone()
        conn.close()
        return int(row["cnt"] or 0) if row else 0
    except Exception as e:
        print("TRUTH_GAME_QUESTION_COUNT_ERROR:", repr(e))
        return 0


def truth_game_answer_count(user_id):
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM truth_game_sessions
        WHERE user_id = ?
          AND status = 'answered'
        """, (user_id,))
        row = cur.fetchone()
        conn.close()
        return int(row["cnt"] or 0) if row else 0
    except Exception as e:
        print("TRUTH_GAME_ANSWER_COUNT_ERROR:", repr(e))
        return 0


def grant_truth_question_achievement_if_ready(user_id, user_name):
    try:
        count = truth_game_question_count(user_id)
        if count < TRUTH_QUESTION_ACHIEVEMENT_REQUIRED:
            return ""
        achievement_name = "난 니가 궁금해!"
        if grant_achievement_once(
            user_id,
            user_name,
            "truth_question_10",
            achievement_name,
            TRUTH_QUESTION_ACHIEVEMENT_REWARD,
            f"truth_questions={count}",
        ):
            return achievement_message(achievement_name, user_name, TRUTH_QUESTION_ACHIEVEMENT_REWARD)
        return ""
    except Exception as e:
        print("TRUTH_GAME_QUESTION_ACHIEVEMENT_ERROR:", repr(e))
        return ""


def grant_truth_answer_achievement_if_ready(user_id, user_name):
    try:
        count = truth_game_answer_count(user_id)
        if count < TRUTH_ANSWER_ACHIEVEMENT_REQUIRED:
            return ""
        achievement_name = "척척박사"
        if grant_achievement_once(
            user_id,
            user_name,
            "truth_answer_10",
            achievement_name,
            TRUTH_ANSWER_ACHIEVEMENT_REWARD,
            f"truth_answers={count}",
        ):
            return achievement_message(achievement_name, user_name, TRUTH_ANSWER_ACHIEVEMENT_REWARD)
        return ""
    except Exception as e:
        print("TRUTH_GAME_ANSWER_ACHIEVEMENT_ERROR:", repr(e))
        return ""


def truth_game_setup_text():
    return (
        "🎭 진실게임 가이드\n\n"
        "난이도와 상대를 지목하면 질문이 하나 뽑혀요.\n\n"
        "사용법\n"
        "/진실 난이도 닉네임\n"
        "예: /진실 썸맛 미트\n\n"
        "진행\n"
        f"- 질문 비용: {coin_text(TRUTH_GAME_COST)}\n"
        f"- 답변 완료: 답변자에게 {coin_text(TRUTH_GAME_COST)} 지급\n"
        f"- 패스: 질문자 {coin_text(TRUTH_GAME_COST)} 환급 / 패스한 사람 {coin_text(TRUTH_GAME_COST)} 차감\n"
        "- 취소: 질문자는 /진실 취소 로 취소 가능\n\n"
        "참고\n"
        "- 자기 자신은 지목할 수 없어요.\n"
        "- 이미 받았던 질문은 같은 난이도에서 다시 나오지 않아요.\n"
        "- 다시 처음부터 받고 싶으면 /진실 초기화\n\n"
        "답변 명령어\n"
        "/답변 내용\n"
        "/패스\n\n"
        f"난이도: {', '.join(TRUTH_GAME_DIFFICULTIES)}"
    )


def parse_truth_game_args(raw_args):
    raw_args = str(raw_args or "").strip()
    if not raw_args:
        return None, None, truth_game_setup_text()

    parts = raw_args.split()
    difficulty = None

    if parts and parts[0] in TRUTH_GAME_DIFFICULTIES:
        difficulty = parts.pop(0)
    elif parts and parts[-1] in TRUTH_GAME_DIFFICULTIES:
        difficulty = parts.pop()

    if not difficulty:
        return None, None, (
            "🎭 진실게임 설정\n\n"
            "난이도를 먼저 선택해주세요.\n\n"
            "사용법\n"
            "/진실 난이도 닉네임\n\n"
            f"난이도: {', '.join(TRUTH_GAME_DIFFICULTIES)}"
        )

    target_keyword = " ".join(parts).strip()
    if not target_keyword:
        return None, None, (
            "🎭 진실게임 설정\n\n"
            "상대를 지목해주세요.\n\n"
            f"예시: /진실 {difficulty} 미트"
        )

    return target_keyword, difficulty, None


def truth_game_seen_question_keys(user_id, difficulty=None):
    try:
        if not user_id:
            return set()

        difficulty = str(difficulty or "").strip()
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT reset_at, reset_after_session_id
        FROM truth_game_resets
        WHERE user_id = ?
        """, (user_id,))
        reset_row = cur.fetchone()
        reset_at = reset_row["reset_at"] if reset_row else None
        reset_after_session_id = int(reset_row["reset_after_session_id"] or 0) if reset_row else 0

        where_parts = ["requester_user_id = ?"]
        params = [user_id]
        if reset_after_session_id > 0:
            where_parts.append("id > ?")
            params.append(reset_after_session_id)
        elif reset_at:
            where_parts.append("created_at > ?")
            params.append(reset_at)
        if difficulty:
            where_parts.append("category = ?")
            params.append(difficulty)

        cur.execute(f"""
        SELECT category, question
        FROM truth_game_sessions
        WHERE {' AND '.join(where_parts)}
        """, params)
        rows = cur.fetchall()
        conn.close()
        return {(row["category"], row["question"]) for row in rows}
    except Exception as e:
        print("TRUTH_GAME_SEEN_QUESTION_ERROR:", repr(e))
        return set()


def truth_game_question_pool(difficulty=None, exclude_seen_for_user=None):
    difficulty = str(difficulty or "").strip()
    pool = [
        item for item in TRUTH_GAME_QUESTIONS
        if not difficulty or item[0] == difficulty
    ]

    try:
        conn = db()
        cur = conn.cursor()
        if difficulty:
            cur.execute("""
            SELECT category, question
            FROM truth_game_questions
            WHERE is_active = 1
              AND category = ?
            ORDER BY id ASC
            """, (difficulty,))
        else:
            cur.execute("""
            SELECT category, question
            FROM truth_game_questions
            WHERE is_active = 1
            ORDER BY id ASC
            """)
        rows = cur.fetchall()
        conn.close()
        existing = {(category, question) for category, question in pool}
        for row in rows:
            item = (row["category"], row["question"])
            if item not in existing:
                pool.append(item)
                existing.add(item)
    except Exception as e:
        print("TRUTH_GAME_QUESTION_POOL_ERROR:", repr(e))

    if exclude_seen_for_user:
        seen = truth_game_seen_question_keys(exclude_seen_for_user, difficulty)
        if seen:
            pool = [item for item in pool if item not in seen]

    return pool


def truth_game_refund_exhausted_question(user_id, user_name, difficulty):
    try:
        conn = db()
        cur = conn.cursor()
        try:
            apply_money_change(
                cur,
                user_id,
                user_name,
                -TRUTH_GAME_COST,
                f"진실게임 질문 소진 확인: {difficulty}",
                None,
                "진실게임"
            )
            apply_money_change(
                cur,
                user_id,
                user_name,
                TRUTH_GAME_COST,
                f"진실게임 질문 소진 즉시 환급: {difficulty}",
                None,
                "진실게임"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as e:
        print("TRUTH_GAME_EXHAUSTED_REFUND_ERROR:", repr(e))


def truth_game_reset_user_questions(user_id, user_name):
    try:
        if not user_id:
            return "🎭 진실게임 초기화 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT COALESCE(MAX(id), 0) AS max_id
        FROM truth_game_sessions
        WHERE requester_user_id = ?
        """, (user_id,))
        reset_after_session_id = int(cur.fetchone()["max_id"] or 0)
        cur.execute("SELECT reset_count FROM truth_game_resets WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        reset_at = now_str()
        if row:
            cur.execute("""
            UPDATE truth_game_resets
            SET user_name = ?,
                reset_at = ?,
                reset_count = reset_count + 1,
                reset_after_session_id = ?
            WHERE user_id = ?
            """, (user_name, reset_at, reset_after_session_id, user_id))
            reset_count = int(row["reset_count"] or 0) + 1
        else:
            cur.execute("""
            INSERT INTO truth_game_resets (
                user_id, user_name, reset_at, reset_count, reset_after_session_id
            ) VALUES (?, ?, ?, 1, ?)
            """, (user_id, user_name, reset_at, reset_after_session_id))
            reset_count = 1
        conn.commit()
        conn.close()

        return (
            "🎭 진실게임 초기화 완료\n\n"
            "이제 이전에 받았던 질문도 다시 나올 수 있어요.\n"
            "기존 진실게임 기록은 삭제하지 않고 그대로 남겨뒀습니다.\n"
            f"초기화 횟수: {reset_count}회"
        )
    except Exception as e:
        print("TRUTH_GAME_RESET_ERROR:", repr(e))
        return "🎭 진실게임 초기화 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def add_truth_game_question(raw_args, staff_user_id, staff_user_name):
    try:
        raw_args = str(raw_args or "").strip()
        if not raw_args:
            return (
                "🎭 진실질문 추가 안내\n\n"
                "사용법: /진실질문추가 난이도 질문내용\n"
                f"난이도: {', '.join(TRUTH_GAME_DIFFICULTIES)}"
            )

        parts = raw_args.split(maxsplit=1)
        if len(parts) < 2:
            return (
                "🎭 진실질문 추가 안내\n\n"
                "난이도와 질문내용을 함께 입력해 주세요.\n"
                "예: /진실질문추가 썸맛 요즘 가장 눈이 가는 사람은?"
            )

        category, question = parts[0].strip(), parts[1].strip()
        if category not in TRUTH_GAME_DIFFICULTIES:
            return (
                "🎭 진실질문 추가 안내\n\n"
                "난이도를 확인해 주세요.\n"
                f"사용 가능: {', '.join(TRUTH_GAME_DIFFICULTIES)}"
            )
        if len(question) < 5:
            return "🎭 진실질문 추가 안내\n\n질문은 5글자 이상으로 입력해 주세요."
        if len(question) > 120:
            return "🎭 진실질문 추가 안내\n\n질문은 120글자 이하로 입력해 주세요."

        conn = db()
        cur = conn.cursor()
        cur.execute("""
        INSERT OR IGNORE INTO truth_game_questions (
            category, question, is_active, created_by, created_by_name, created_at
        ) VALUES (?, ?, 1, ?, ?, ?)
        """, (category, question, staff_user_id, staff_user_name, now_str()))
        inserted = cur.rowcount
        conn.commit()
        conn.close()

        if not inserted:
            return (
                "🎭 진실질문 추가 안내\n\n"
                "이미 등록된 질문입니다."
            )

        return (
            "🎭 진실질문 추가 완료\n\n"
            f"난이도: {category}\n"
            f"질문: {question}"
        )
    except Exception as e:
        print("TRUTH_GAME_QUESTION_ADD_ERROR:", repr(e))
        return "🎭 진실질문을 추가하는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def truth_game_start(user_id, user_name, target_keyword, difficulty=None):
    try:
        if not user_id:
            return "🎭 진실게임 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        target_keyword = str(target_keyword or "").strip()
        if not target_keyword:
            return truth_game_setup_text()

        difficulty = str(difficulty or "").strip()
        if difficulty and difficulty not in TRUTH_GAME_DIFFICULTIES:
            return (
                "🎭 진실게임 안내\n\n"
                "난이도를 확인해 주세요.\n"
                f"사용 가능: {', '.join(TRUTH_GAME_DIFFICULTIES)}"
            )

        target, err = resolve_active_user_by_nickname(target_keyword, purpose="대상")
        if err:
            return "🎭 진실게임 안내\n\n" + err
        if target["user_id"] == user_id:
            return "🎭 진실게임 안내\n\n자기 자신은 지목할 수 없어요."

        pending = get_pending_truth_game(target["user_id"])
        if pending:
            cancel_line = ""
            if pending["requester_user_id"] == user_id:
                cancel_line = "\n취소: /진실 취소"
            return (
                "🎭 진행 중인 진실게임이 있습니다.\n\n"
                f"대상: {pending['user_name']}\n"
                f"질문자: {pending['requester_user_name'] or '-'}\n"
                f"난이도: {pending['category']}\n"
                f"질문: {pending['question']}\n"
                f"{pending['user_name']}님이 먼저 답변하거나 패스해야 합니다."
                f"{cancel_line}"
            )

        if get_balance(user_id) < TRUTH_GAME_COST:
            return (
                "🎭 진실게임 안내\n\n"
                f"필요 코인: {coin_text(TRUTH_GAME_COST)}\n"
                f"현재 보유: {coin_text(get_balance(user_id))}"
            )

        all_question_pool = truth_game_question_pool(difficulty)
        if not all_question_pool:
            return "🎭 진실게임 안내\n\n지금 사용할 수 있는 질문이 없어요."

        question_pool = truth_game_question_pool(difficulty, exclude_seen_for_user=user_id)
        if not question_pool:
            truth_game_refund_exhausted_question(user_id, user_name, difficulty)
            return (
                "🎭 진실게임 안내\n\n"
                f"{difficulty} 질문은 전부 받아봤어요.\n"
                "새 질문은 향후 추가될 예정입니다.\n"
                f"코인은 바로 환급 처리했어요. 현재 보유: {coin_text(get_balance(user_id))}\n\n"
                "다시 처음부터 받고 싶다면 /진실 초기화 를 입력해 주세요."
            )
        category, question = random.choice(question_pool)

        conn = db()
        cur = conn.cursor()
        try:
            apply_money_change(cur, user_id, user_name, -TRUTH_GAME_COST, "진실게임 질문 뽑기", None, "진실게임")
            cur.execute("""
            INSERT INTO truth_game_sessions (
                user_id, user_name, requester_user_id, requester_user_name,
                question, category, status, cost, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                target["user_id"], target["user_name"],
                user_id, user_name,
                question, category, TRUTH_GAME_COST, now_str()
            ))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        target_name = display_nickname(target["user_name"])
        achievement_notice = grant_truth_question_achievement_if_ready(user_id, user_name)
        result_text = (
            "🎭 진실게임 시작\n\n"
            f"지목: {target_name}님\n"
            f"난이도: {category}\n"
            f"질문: {question}\n"
            f"질문 비용: -{coin_text(TRUTH_GAME_COST)}\n"
            f"현재 보유: {coin_text(get_balance(user_id))}\n\n"
            f"{target_name}님은 /답변 내용 으로 답변하거나 /패스 로 넘길 수 있습니다.\n"
            "질문자는 /진실 취소 로 취소할 수 있습니다."
        )
        if achievement_notice:
            result_text += "\n\n" + achievement_notice
        return result_text
    except Exception as e:
        print("TRUTH_GAME_START_ERROR:", repr(e))
        return "🎭 진실게임을 시작하는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def truth_game_answer(user_id, user_name, answer_text):
    try:
        if not user_id:
            return "🎭 진실게임 답변 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        answer_text = str(answer_text or "").strip()
        if not answer_text:
            return "🎭 진실게임 답변 안내\n\n사용법: /답변 내용"

        pending = get_pending_truth_game(user_id)
        if not pending:
            return "🎭 진실게임 답변 안내\n\n지금 진행 중인 질문이 없어요.\n질문 뽑기: /진실"

        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("""
            UPDATE truth_game_sessions
            SET status = 'answered',
                answered_at = ?,
                answer_text = ?,
                user_name = ?
            WHERE id = ? AND status = 'pending'
            """, (now_str(), answer_text, user_name, pending["id"]))
            changed = cur.rowcount
            if changed:
                apply_money_change(cur, user_id, user_name, TRUTH_GAME_COST, "진실게임 답변 보상", None, "진실게임")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if not changed:
            return "🎭 진실게임 답변 안내\n\n지금 진행 중인 질문이 없어요."

        achievement_notice = grant_truth_answer_achievement_if_ready(user_id, user_name)
        requester = pending["requester_user_name"] if "requester_user_name" in pending.keys() else None
        request_line = f"지목자: {requester}\n" if requester else ""
        result_text = (
            "🎭 진실게임 답변\n\n"
            f"{request_line}"
            f"질문: {pending['question']}\n\n"
            f"{user_name}님의 답변:\n"
            f"{answer_text}\n\n"
            f"답변 보상: {coin_text(TRUTH_GAME_COST)}"
        )
        if achievement_notice:
            result_text += "\n\n" + achievement_notice
        return result_text
    except Exception as e:
        print("TRUTH_GAME_ANSWER_ERROR:", repr(e))
        return "🎭 진실게임 답변 처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def truth_game_cancel(requester_user_id, requester_user_name, target_keyword=""):
    try:
        if not requester_user_id:
            return "🎭 진실게임 취소 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        target_keyword = str(target_keyword or "").strip()
        target_user_id = None
        if target_keyword:
            target, err = resolve_active_user_by_nickname(target_keyword, purpose="대상")
            if err:
                return "🎭 진실게임 취소 안내\n\n" + err
            target_user_id = target["user_id"]

        pending = get_pending_truth_game_by_requester(requester_user_id, target_user_id)
        if not pending:
            answer_pending = get_pending_truth_game(requester_user_id)
            if answer_pending:
                return (
                    "🎭 진실게임 취소 안내\n\n"
                    "이 질문은 내가 건 질문이 아니라 취소할 수 없어요.\n"
                    "답변하려면 /답변 내용, 넘기려면 /패스 를 사용해 주세요."
                )
            return (
                "🎭 진실게임 취소 안내\n\n"
                "내가 걸어둔 진행 중 질문이 없어요.\n"
                "특정 대상 질문을 취소하려면 /진실 취소 닉네임 으로 입력해 주세요."
            )

        refund = int(pending["cost"] or TRUTH_GAME_COST)
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("""
            UPDATE truth_game_sessions
            SET status = 'cancelled',
                answered_at = ?,
                answer_text = ?,
                requester_user_name = ?
            WHERE id = ? AND status = 'pending' AND requester_user_id = ?
            """, (
                now_str(),
                "질문자가 취소했습니다.",
                requester_user_name,
                pending["id"],
                requester_user_id,
            ))
            changed = cur.rowcount
            if changed and refund > 0:
                apply_money_change(
                    cur,
                    requester_user_id,
                    requester_user_name,
                    refund,
                    f"진실게임 질문 취소 환급: {pending['user_name']}",
                    None,
                    "진실게임"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if not changed:
            return "🎭 진실게임 취소 안내\n\n이미 처리된 질문이라 취소할 수 없어요."

        return (
            "🎭 진실게임 취소 완료\n\n"
            f"대상: {pending['user_name']}\n"
            f"난이도: {pending['category']}\n"
            f"질문: {pending['question']}\n"
            f"환급: {coin_text(refund)}"
        )
    except Exception as e:
        print("TRUTH_GAME_CANCEL_ERROR:", repr(e))
        return "🎭 진실게임 취소 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def truth_game_pass(user_id, user_name):
    try:
        if not user_id:
            return "🎭 진실게임 패스 안내\n\n사용자 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요."

        pending = get_pending_truth_game(user_id)
        if not pending:
            return "🎭 진실게임 패스 안내\n\n지금 진행 중인 질문이 없어요.\n질문 뽑기: /진실"

        requester = pending["requester_user_name"] if "requester_user_name" in pending.keys() else None
        requester_user_id = pending["requester_user_id"] if "requester_user_id" in pending.keys() else None
        refund = int(pending["cost"] or TRUTH_GAME_COST)
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute("""
            UPDATE truth_game_sessions
            SET status = 'passed',
                answered_at = ?,
                user_name = ?
            WHERE id = ? AND status = 'pending'
            """, (now_str(), user_name, pending["id"]))
            changed = cur.rowcount
            if changed and requester_user_id and requester:
                apply_money_change(
                    cur,
                    requester_user_id,
                    requester,
                    refund,
                    f"진실게임 패스 환급: {user_name}",
                    None,
                    "진실게임"
                )
            if changed:
                apply_money_change(
                    cur,
                    user_id,
                    user_name,
                    -TRUTH_GAME_COST,
                    f"진실게임 패스 비용: {requester or '질문자'}",
                    None,
                    "진실게임"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if not changed:
            return "🎭 진실게임 패스 안내\n\n지금 진행 중인 질문이 없어요."

        refund_line = ""
        if requester_user_id and requester:
            refund_line = f"\n질문자 환급: {coin_text(refund)}"
        pass_cost_line = f"\n패스 비용: -{coin_text(TRUTH_GAME_COST)}"
        request_line = f"질문자: {requester}\n" if requester else ""
        return (
            "🎭 진실게임 패스\n\n"
            f"{request_line}"
            f"{user_name}님이 질문을 패스했습니다."
            f"{refund_line}"
            f"{pass_cost_line}"
        )
    except Exception as e:
        print("TRUTH_GAME_PASS_ERROR:", repr(e))
        return "🎭 진실게임 패스 처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


def truth_game_list_text(limit=10):
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT
            t.user_name,
            t.requester_user_name,
            t.question,
            t.answer_text,
            t.answered_at
        FROM truth_game_sessions t
        JOIN users u
          ON u.user_id = t.user_id
        LEFT JOIN deleted_users d
          ON d.original_user_id = t.user_id
        WHERE t.status = 'answered'
          AND COALESCE(u.is_active, 1) = 1
          AND d.original_user_id IS NULL
        ORDER BY t.id DESC
        LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()

        lines = ["🎭 최근 진실게임", ""]
        if not rows:
            lines.append("아직 답변된 진실게임이 없습니다.")
        else:
            for i, row in enumerate(rows, 1):
                lines.append(f"{i}. {row['user_name']}")
                if row["requester_user_name"]:
                    lines.append(f"지목자: {row['requester_user_name']}")
                lines.append(f"Q. {row['question']}")
                lines.append(f"A. {row['answer_text']}")
                if row["answered_at"]:
                    lines.append(f"시간: {row['answered_at']}")
                lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        print("TRUTH_GAME_LIST_ERROR:", repr(e))
        return "🎭 진실게임 목록을 불러오는 중 문제가 생겼어요."


def truth_game_user_history_text(keyword, limit=20):
    try:
        keyword = str(keyword or "").strip()
        if not keyword:
            return "사용법: /진실기록 닉네임"

        target, err = resolve_active_user_by_nickname(keyword, purpose="대상")
        if err:
            return "🎭 진실기록 조회 실패\n\n" + err

        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT
            id,
            user_id,
            user_name,
            requester_user_id,
            requester_user_name,
            question,
            category,
            status,
            created_at,
            answered_at,
            answer_text
        FROM truth_game_sessions
        WHERE user_id = ?
           OR requester_user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """, (target["user_id"], target["user_id"], limit))
        rows = cur.fetchall()
        conn.close()

        target_name = target["user_name"]
        lines = [
            "🎭 진실기록",
            f"대상: {target_name}",
            "",
        ]

        if not rows:
            lines.append("진실게임 기록이 없습니다.")
            return "\n".join(lines)

        status_labels = {
            "pending": "진행중",
            "answered": "답변완료",
            "passed": "패스",
            "cancelled": "취소",
        }

        for i, row in enumerate(rows, 1):
            if row["user_id"] == target["user_id"]:
                role = "답변 대상"
                other_line = f"지목자: {row['requester_user_name'] or '-'}"
            else:
                role = "질문자"
                other_line = f"지목 대상: {row['user_name'] or '-'}"

            status = status_labels.get(row["status"], row["status"])
            lines.append(f"{i}. #{row['id']} {role} / {status}")
            lines.append(f"   {other_line}")
            lines.append(f"   난이도: {row['category']}")
            lines.append(f"   질문: {row['question']}")
            if row["status"] == "answered":
                lines.append(f"   답변: {row['answer_text'] or '-'}")
            elif row["status"] == "passed":
                lines.append("   답변: 패스")
            elif row["status"] == "cancelled":
                lines.append("   답변: 질문자 취소")
            else:
                lines.append("   답변: 아직 없음")
            lines.append(f"   생성: {row['created_at']}")
            if row["answered_at"]:
                lines.append(f"   처리: {row['answered_at']}")
            lines.append("")

        return "\n".join(lines).strip()
    except Exception as e:
        print("TRUTH_GAME_USER_HISTORY_ERROR:", repr(e))
        return "🎭 진실기록을 불러오는 중 문제가 생겼어요."


def grant_blacksmith_if_first(user_id, user_name, piece_key):
    info = PIECE_INFO.get(piece_key)
    if not info:
        return False
    return grant_achievement_once(
        user_id,
        user_name,
        f"blacksmith_{piece_key}",
        f"🔨 대장장이: {piece_item_name(info)}",
        20,
        f"piece_key={piece_key}"
    )


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
        ("attendance_streak_rewards", "user_id", "user_name"),
        ("danbung_attendance", "user_id", "user_name"),
        ("mission_claims", "user_id", "user_name"),
        ("hidden_rewards", "user_id", "user_name"),
        ("gacha_settings", "user_id", "user_name"),
        ("gacha_pity", "user_id", "user_name"),
        ("weekly_rewards", "user_id", "user_name"),
        ("sns_lucky_draw_entries", "user_id", "user_name"),
        ("achievements", "user_id", "user_name"),
        ("truth_game_sessions", "user_id", "user_name"),
        ("truth_game_sessions", "requester_user_id", "requester_user_name"),
        ("truth_game_resets", "user_id", "user_name"),
        ("chat_last_speakers", "user_id", "user_name"),
        ("mention_logs", "sender_user_id", "sender_user_name"),
        ("mention_logs", "target_user_id", "target_user_name"),
        ("anonymous_pokes", "sender_user_id", "sender_user_name"),
        ("anonymous_pokes", "target_user_id", "target_user_name"),
        ("heart_picks", "sender_user_id", "sender_user_name"),
        ("heart_picks", "target_user_id", "target_user_name"),
        ("heart_pick_rewards", "user_id", "user_name"),
        ("chemistry_signals", "sender_user_id", "sender_user_name"),
        ("chemistry_signals", "target_user_id", "target_user_name"),
        ("chemistry_rewards", "user_id", "user_name"),
        ("chemistry_rewards", "matched_user_id", "matched_user_name"),
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
        "revival_claims",
        "purchases",
        "attendance",
        "attendance_streak_rewards",
        "danbung_attendance",
        "mission_claims",
        "hidden_rewards",
        "gacha_settings",
        "gacha_pity",
        "gacha_pieces",
        "weekly_rewards",
        "sns_lucky_draw_entries",
        "achievements",
        "truth_game_sessions",
        "truth_game_resets",
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
            ("mention_logs", "sender_user_id"),
            ("mention_logs", "target_user_id"),
            ("anonymous_pokes", "sender_user_id"),
            ("anonymous_pokes", "target_user_id"),
            ("heart_picks", "sender_user_id"),
            ("heart_picks", "target_user_id"),
            ("heart_pick_rewards", "user_id"),
            ("chemistry_signals", "sender_user_id"),
            ("chemistry_signals", "target_user_id"),
            ("chemistry_rewards", "user_id"),
            ("chemistry_rewards", "matched_user_id"),
            ("truth_game_sessions", "requester_user_id"),
            ("affinity_scores", "user_a"),
            ("affinity_scores", "user_b"),
            ("affinity_cumulative_scores", "user_a"),
            ("affinity_cumulative_scores", "user_b"),
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



def jagiya_achievement_notice(user_name, other_name):
    return (
        "🏆 업적 달성!\n\n"
        "💕 자기야\n\n"
        f"{user_name}님과 {other_name}님이\n"
        "누적 친밀도 500을 달성했습니다.\n\n"
        "보상: 💰3코인"
    )

def process_affinity_message(source_id, user_id, user_name, text_value):
    if source_id != COUNT_SOURCE_ID or not user_id or not text_value:
        return None
    if str(text_value).startswith('/'):
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
    INSERT INTO affinity_cumulative_scores (user_a, user_b, user_a_name, user_b_name, total_score, updated_at)
    VALUES (?, ?, ?, ?, 1, ?)
    ON CONFLICT(user_a, user_b)
    DO UPDATE SET total_score = total_score + 1,
                  user_a_name = excluded.user_a_name,
                  user_b_name = excluded.user_b_name,
                  updated_at = excluded.updated_at
    """, (a, b, a_name, b_name, now_str()))

    cur.execute("""
    SELECT total_score
    FROM affinity_cumulative_scores
    WHERE user_a = ? AND user_b = ?
    """, (a, b))
    cumulative_row = cur.fetchone()
    cumulative_score = int(cumulative_row["total_score"] or 0) if cumulative_row else 0

    cur.execute("""
    INSERT INTO affinity_pair_cooldowns (source_id, week_start, user_a, user_b, last_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(source_id, week_start, user_a, user_b)
    DO UPDATE SET last_at = excluded.last_at
    """, (source_id, week_start, a, b, now_str()))

    conn.commit()
    conn.close()

    messages = []

    try:
        milestone_msg = grant_affinity_milestone_achievements_if_ready(
            user_id, user_name,
            last["user_id"], last["user_name"],
            cumulative_score
        )
        if milestone_msg:
            messages.append(milestone_msg)

        jagiya_msg = grant_jagiya_achievement_if_ready(
            user_id, user_name,
            last["user_id"], last["user_name"],
            cumulative_score
        )
        if jagiya_msg:
            messages.append(jagiya_msg)
    except Exception as e:
        print("AFFINITY_ACHIEVEMENT_ERROR:", repr(e))

    try:
        msg1 = complete_manitto_if_ready(user_id, user_name, last["user_id"])
        if msg1:
            messages.append(msg1)

        msg2 = complete_manitto_if_ready(last["user_id"], last["user_name"], user_id)
        if msg2:
            messages.append(msg2)
    except Exception as e:
        print("MANITTO_COMPLETE_CHECK_ERROR:", repr(e))

    if messages:
        return "\n".join(dict.fromkeys(messages))
    return None


def grant_affinity_milestone_achievements_if_ready(user_id_1, user_name_1, user_id_2, user_name_2, total_score):
    """
    한 상대와 누적 친밀도 단계 달성 시 양쪽에게 업적을 지급합니다.
    각 단계는 유저별 최초 1회만 지급됩니다.
    """
    total_score = int(total_score or 0)
    unlocked = []

    for required_score, key, title, reward in AFFINITY_ACHIEVEMENT_MILESTONES:
        if total_score < required_score:
            continue

        paid = []
        for owner_id, owner_name, partner_id, partner_name in [
            (user_id_1, user_name_1, user_id_2, user_name_2),
            (user_id_2, user_name_2, user_id_1, user_name_1),
        ]:
            meta = f"partner_id={partner_id};partner_name={partner_name};total_affinity={total_score}"
            if grant_achievement_once(owner_id, owner_name, key, title, reward, meta):
                paid.append(owner_name)

        if paid:
            unlocked.append((required_score, title, reward))

    if not unlocked:
        return None

    lines = [
        "🏆 업적 달성!",
        "",
        f"{user_name_1}님과 {user_name_2}님",
        f"누적 친밀도 {total_score}",
        "",
    ]
    for required_score, title, reward in unlocked:
        lines.append(f"{title} - {required_score} 달성 / 각 {coin_text(reward)}")
    return "\n".join(lines)



# =========================
# 표시 함수 호환 보정
# =========================
def weekly_gacha_count_text(user_id):
    return gacha_count_status_text(user_id)


def gacha_pity_text(user_id, user_name):
    point = get_gacha_pity_point(user_id)
    return (
        "🍀 행운포인트\n\n"
        f"{user_name}님\n"
        f"현재 포인트: {point} / 10\n\n"
        "코인가챠 F등급 획득 시 +1\n"
        f"10포인트 달성 시 {coin_text(GACHA_PITY_REWARD)} 자동 지급"
    )


def gacha_piece_text(user_id):
    rows = get_all_gacha_pieces(user_id)
    lines = ["🧩 조각 보유 현황", ""]
    if not rows:
        lines.append("보유 중인 조각이 없습니다.")
    else:
        piece_map = {row["piece_key"]: row["count"] for row in rows}
        for key, info in PIECE_INFO.items():
            count = int(piece_map.get(key, 0) or 0)
            lines.append(f"{info['label']} {count} / {info['need']}")
    return "\n".join(lines)



def add_simple_piece(user_id, user_name, piece_key, amount):
    if piece_key not in PIECE_INFO:
        piece_key = "iron"
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO gacha_pieces (user_id, piece_key, count, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id, piece_key)
    DO UPDATE SET count = count + excluded.count, updated_at = excluded.updated_at
    """, (user_id, piece_key, int(amount), now_str()))
    conn.commit()
    conn.close()


def run_piece_gacha(user_id, user_name):
    cost = 10
    result_kind = weighted_pick(PIECE_STANDALONE_GACHA_WEIGHTS)
    piece_key = random_piece_by_group() if result_kind == "piece" else None
    final_balance = 0

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(balance, 0) AS balance FROM currency WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        balance = int(row["balance"] or 0) if row else 0
        if balance < cost:
            return False, f"코인이 부족합니다.\n\n필요: {coin_text(cost)}\n보유: {coin_text(balance)}"

        final_balance = apply_money_change(cur, user_id, user_name, -cost, "조각가챠 이용", None, "가챠시스템")
        used_after = apply_weekly_gacha_count(cur, user_id, user_name)

        if piece_key:
            cur.execute("""
            INSERT INTO gacha_pieces (user_id, piece_key, count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, piece_key)
            DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
            """, (user_id, piece_key, now_str()))
            label = PIECE_INFO[piece_key]["label"]
            result = f"획득: {label} x1"
        else:
            result = "획득: 꽝"

        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("RUN_PIECE_GACHA_ERROR", e)
        return False, "🧩 조각가챠 처리 중 문제가 생겼어요. 최근오류를 확인해 주세요."
    finally:
        conn.close()

    return True, f"🧩 조각가챠 결과\n\n{result}\n\n이번 주 가챠: {used_after}회\n현재 잔액: {coin_text(final_balance)}"


def blacksmith_exchange(user_id, user_name):
    conn = db()
    cur = conn.cursor()
    paid = []
    final_balance = 0
    try:
        for key, info in PIECE_INFO.items():
            cur.execute("SELECT count FROM gacha_pieces WHERE user_id = ? AND piece_key = ?", (user_id, key))
            row = cur.fetchone()
            count = int(row["count"] or 0) if row else 0
            sets = count // int(info["need"])
            if sets <= 0:
                continue
            used = sets * int(info["need"])
            remain = count - used
            cur.execute("UPDATE gacha_pieces SET count = ?, updated_at = ? WHERE user_id = ? AND piece_key = ?", (remain, now_str(), user_id, key))
            reward = sets * int(info["reward"])
            paid.append((info["label"], sets, reward))

        if not paid:
            return "🔨 대장장이\n\n교환 가능한 조각이 없습니다.\n\n철/은/금 조각은 각 10개 단위로 교환됩니다."

        total = sum(x[2] for x in paid)
        final_balance = apply_money_change(cur, user_id, user_name, total, "대장장이 조각 교환", None, "대장장이")
        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("BLACKSMITH_EXCHANGE_ERROR", e)
        return "🔨 대장장이 처리 중 문제가 생겼어요. 최근오류를 확인해 주세요."
    finally:
        conn.close()

    lines = ["🔨 대장장이 교환 완료", ""]
    for label, sets, reward in paid:
        lines.append(f"{label} 10개 x{sets}세트 → {coin_text(reward)}")
    lines += ["", f"총 지급: {coin_text(total)}", f"현재 보유: {coin_text(final_balance)}"]
    return "\n".join(lines)


def migrate_old_pieces_to_iron():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, piece_key, count FROM gacha_pieces")
    rows = cur.fetchall()
    converted = 0
    for row in rows:
        key = row["piece_key"]
        if key in PIECE_INFO:
            continue
        count = int(row["count"] or 0)
        if count <= 0:
            cur.execute("DELETE FROM gacha_pieces WHERE user_id = ? AND piece_key = ?", (row["user_id"], key))
            continue
        cur.execute("""
        INSERT INTO gacha_pieces (user_id, piece_key, count, updated_at)
        VALUES (?, 'iron', ?, ?)
        ON CONFLICT(user_id, piece_key)
        DO UPDATE SET count = count + excluded.count, updated_at = excluded.updated_at
        """, (row["user_id"], count, now_str()))
        cur.execute("DELETE FROM gacha_pieces WHERE user_id = ? AND piece_key = ?", (row["user_id"], key))
        converted += count
    conn.commit()
    conn.close()
    return converted

def shop_text():
    rows = list_shop_items()
    lines = ["🛒 상점", ""]
    if not rows:
        lines.append("현재 판매 중인 상품이 없습니다.")
    else:
        for row in rows:
            desc = f"\n{row['description']}" if row["description"] else ""
            lines.append(f"{row['name']} - {coin_text(row['price'])}{desc}")
            lines.append("")
    lines += [
        "구매 방법",
        "/구매 상품명",
        "",
        "보유 확인",
        "/내보유",
    ]
    return "\n".join(lines)

# =========================
# 마니또 로직 v64
# =========================
def get_current_manitto(user_id):
    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT *
    FROM manitto_assignments
    WHERE week_start = ?
      AND hunter_user_id = ?
    """, (week_start, user_id))
    row = cur.fetchone()
    conn.close()
    return row


def pick_manitto_type():
    return "gold" if random.random() < MANITTO_GOLD_RATE else "normal"


def manitto_reward_range(manitto_type):
    if manitto_type == "gold":
        return MANITTO_GOLD_REWARD_MIN, MANITTO_GOLD_REWARD_MAX
    return MANITTO_NORMAL_REWARD_MIN, MANITTO_NORMAL_REWARD_MAX


def manitto_target_candidates(hunter_user_id, exclude_ids=None, strict=True):
    """
    마니또 대상 후보.
    strict=True:
      - 활성 유저
      - 본인 제외
      - 2코인 이상
      - 최근 7일 내 COUNT_SOURCE_ID 채팅 기록 존재
    strict=False:
      - 활성 유저 + 본인/제외대상 제외
    """
    exclude_ids = set(exclude_ids or [])
    exclude_ids.add(hunter_user_id)

    since_date = (datetime.now(KST) - timedelta(days=MANITTO_ACTIVE_DAYS)).strftime("%Y-%m-%d")

    conn = db()
    cur = conn.cursor()

    params = []
    exclude_sql = ""
    if exclude_ids:
        placeholders = ",".join("?" for _ in exclude_ids)
        exclude_sql = f" AND u.user_id NOT IN ({placeholders})"
        params.extend(list(exclude_ids))

    if strict:
        sql = f"""
        SELECT
            u.user_id,
            u.user_name,
            COALESCE(c.balance, 0) AS balance,
            COALESCE(SUM(cnt.count), 0) AS recent_count
        FROM users u
        LEFT JOIN currency c ON c.user_id = u.user_id
        LEFT JOIN counts cnt
          ON cnt.user_id = u.user_id
         AND cnt.source_id = ?
         AND cnt.date >= ?
        WHERE COALESCE(u.is_active, 1) = 1
          {exclude_sql}
        GROUP BY u.user_id
        HAVING balance >= ?
           AND recent_count > 0
        ORDER BY RANDOM()
        """
        cur.execute(sql, [COUNT_SOURCE_ID, since_date] + params + [MANITTO_MIN_TARGET_BALANCE])
    else:
        sql = f"""
        SELECT
            u.user_id,
            u.user_name,
            COALESCE(c.balance, 0) AS balance,
            0 AS recent_count
        FROM users u
        LEFT JOIN currency c ON c.user_id = u.user_id
        WHERE COALESCE(u.is_active, 1) = 1
          {exclude_sql}
        ORDER BY RANDOM()
        """
        cur.execute(sql, params)

    rows = cur.fetchall()
    conn.close()
    return rows


def manitto_target_pick(hunter_user_id, exclude_ids=None):
    """
    1순위: 2코인 이상 + 최근 7일 활동
    후보 부족 시 전체 활성유저로 완화
    """
    rows = manitto_target_candidates(hunter_user_id, exclude_ids, strict=True)
    if rows:
        return random.choice(rows)

    rows = manitto_target_candidates(hunter_user_id, exclude_ids, strict=False)
    if rows:
        return random.choice(rows)

    return None


def assign_manitto_if_missing(user_id, user_name):
    current = get_current_manitto(user_id)
    if current:
        return current

    week_start, week_end = event_week_key()
    target = manitto_target_pick(user_id)

    if not target:
        return None

    manitto_type = pick_manitto_type()
    required_score, reward_min, reward_max = calculate_manitto_goal_and_rewards(user_id, target["user_id"], manitto_type)
    reward = random.randint(reward_min, reward_max)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO manitto_assignments (
        week_start, week_end,
        hunter_user_id, hunter_user_name,
        target_user_id, target_user_name,
        required_score, reward_min, reward_max, reward,
        manitto_type, completed, reroll_count, previous_target_ids,
        created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
    """, (
        week_start, week_end,
        user_id, user_name,
        target["user_id"], target["user_name"],
        required_score,
        reward_min,
        reward_max,
        reward,
        manitto_type,
        target["user_id"],
        now_str(),
        now_str()
    ))
    conn.commit()
    conn.close()

    return get_current_manitto(user_id)


def get_pair_weekly_affinity(user_a, user_b):
    week_start, week_end = event_week_key()
    a, b = pair_key(user_a, user_b)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT score
    FROM affinity_scores
    WHERE week_start = ?
      AND user_a = ?
      AND user_b = ?
    """, (week_start, a, b))
    row = cur.fetchone()
    conn.close()

    return int(row["score"] or 0) if row else 0


def manitto_status_text(user_id, user_name):
    row = assign_manitto_if_missing(user_id, user_name)
    if not row:
        return "🎭 마니또를 배정할 대상이 부족합니다."

    progress = get_pair_weekly_affinity(user_id, row["target_user_id"])
    completed = int(row["completed"] or 0) == 1
    reroll_count = int(row["reroll_count"] or 0) if "reroll_count" in row.keys() else 0
    manitto_type = row["manitto_type"] or "normal"

    if manitto_type == "gold":
        title = "👑 황금 마니또"
        reward_line = "❓ 고급 랜덤 보상"
        extra = "\n황금 마니또는 일반 마니또보다 높은 보상을 지급합니다."
    else:
        title = "🎭 이번 주 마니또"
        reward_line = "❓ 랜덤 코인"
        extra = ""

    if completed:
        paid_reward = manitto_completed_reward_amount(row, user_id)
        reward_text = coin_text(paid_reward) if paid_reward > 0 else "기록 확인 필요"
        return (
            f"{title}\n\n"
            "✅ 미션 성공\n\n"
            f"대상\n{row['target_user_name']}\n\n"
            f"달성 친밀도\n{int(row['required_score'] or MANITTO_REQUIRED_SCORE)} / {int(row['required_score'] or MANITTO_REQUIRED_SCORE)}\n\n"
            f"🎁 받은 보상\n{reward_text}\n\n"
            "보상은 이미 지급 완료되었습니다.\n\n"
            "축하합니다 😊"
        )

    required_score = int(row["required_score"] or MANITTO_REQUIRED_SCORE)
    near = "\n\n🔥 거의 달성했습니다!" if progress >= required_score - 2 else ""

    return (
        f"{title}\n\n"
        f"대상\n{row['target_user_name']}\n\n"
        f"진행도\n{progress} / {required_score}\n\n"
        f"🎁 성공 보상\n{reward_line}\n"
        f"{extra}\n\n"
        "━━━━━━━━━━\n\n"
        f"대상과 친밀도 {required_score} 달성 시\n"
        "자동으로 성공 처리됩니다.\n\n"
        "🎲 대상 변경\n"
        "/마니또변경\n\n"
        f"남은 변경횟수\n{max(0, MANITTO_REROLL_LIMIT - reroll_count)} / {MANITTO_REROLL_LIMIT}"
        f"{near}"
    )


def manitto_completed_reward_amount(row, user_id):
    reward = int(row["reward"] or 0) if "reward" in row.keys() else 0
    if reward > 0:
        return reward

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT amount
        FROM currency_logs
        WHERE user_id = ?
          AND staff_user_name = '마니또'
          AND reason LIKE ?
        ORDER BY created_at DESC
        LIMIT 1
        """, (user_id, f"마니또 성공: {row['target_user_name']}%"))
        log_row = cur.fetchone()
        conn.close()
        if log_row and int(log_row["amount"] or 0) > 0:
            return int(log_row["amount"] or 0)
    except Exception as e:
        log_error("MANITTO_REWARD_LOOKUP_ERROR", e)

    return 0


def reroll_manitto(user_id, user_name):
    row = assign_manitto_if_missing(user_id, user_name)
    if not row:
        return "🎭 마니또를 변경할 대상이 부족합니다."

    if int(row["completed"] or 0) == 1:
        return "❌ 완료된 마니또는 변경할 수 없습니다."

    reroll_count = int(row["reroll_count"] or 0) if "reroll_count" in row.keys() else 0
    if reroll_count >= MANITTO_REROLL_LIMIT:
        return (
            "❌ 이번 주 변경 횟수를 모두 사용했습니다.\n\n"
            f"사용 횟수\n{reroll_count} / {MANITTO_REROLL_LIMIT}"
        )

    previous_ids = set()
    if "previous_target_ids" in row.keys() and row["previous_target_ids"]:
        previous_ids.update(x for x in str(row["previous_target_ids"]).split(",") if x)

    previous_ids.add(row["target_user_id"])
    previous_ids.add(user_id)

    target = manitto_target_pick(user_id, previous_ids)

    # 후보가 너무 부족하면 현재 대상/본인만 제외하고 재시도
    if not target:
        target = manitto_target_pick(user_id, {user_id, row["target_user_id"]})

    if not target:
        return "🎭 변경 가능한 새 대상이 없습니다."

    new_previous = ",".join(sorted(previous_ids - {user_id}))

    # 변경 시 마니또 타입과 보상도 다시 랜덤
    manitto_type = pick_manitto_type()
    required_score, reward_min, reward_max = calculate_manitto_goal_and_rewards(user_id, target["user_id"], manitto_type)
    reward = random.randint(reward_min, reward_max)

    conn = db()
    cur = conn.cursor()
    week_start, week_end = event_week_key()
    cur.execute("""
    UPDATE manitto_assignments
    SET target_user_id = ?,
        target_user_name = ?,
        manitto_type = ?,
        required_score = ?,
        reward_min = ?,
        reward_max = ?,
        reward = ?,
        reroll_count = COALESCE(reroll_count, 0) + 1,
        previous_target_ids = ?,
        updated_at = ?
    WHERE week_start = ?
      AND hunter_user_id = ?
    """, (
        target["user_id"],
        target["user_name"],
        manitto_type,
        required_score,
        reward_min,
        reward_max,
        reward,
        new_previous,
        now_str(),
        week_start,
        user_id
    ))
    conn.commit()
    conn.close()

    title = "👑 황금 마니또" if manitto_type == "gold" else "🎭 마니또"

    return (
        f"{title} 변경 완료\n\n"
        f"기존 대상\n{row['target_user_name']}\n\n"
        "⬇️\n\n"
        f"새로운 대상\n{target['user_name']}\n\n"
        f"남은 변경 횟수\n{max(0, MANITTO_REROLL_LIMIT - reroll_count - 1)} / {MANITTO_REROLL_LIMIT}"
    )


def complete_manitto_if_ready(hunter_user_id, hunter_user_name, partner_user_id):
    row = get_current_manitto(hunter_user_id)
    if not row:
        return None

    if int(row["completed"] or 0) == 1:
        return None

    if row["target_user_id"] != partner_user_id:
        return None

    required_score = int(row["required_score"] or MANITTO_REQUIRED_SCORE)
    progress = get_pair_weekly_affinity(hunter_user_id, partner_user_id)
    if progress < required_score:
        return None

    reward = int(row["reward"] or 0)
    if reward <= 0:
        manitto_type = row["manitto_type"] or "normal"
        reward_min, reward_max = manitto_reward_range(manitto_type)
        reward = random.randint(reward_min, reward_max)

    conn = db()
    cur = conn.cursor()
    week_start, week_end = event_week_key()
    try:
        cur.execute("""
        UPDATE manitto_assignments
        SET completed = 1,
            reward = ?,
            completed_at = ?,
            updated_at = ?
        WHERE week_start = ?
          AND hunter_user_id = ?
          AND completed = 0
        """, (reward, now_str(), now_str(), week_start, hunter_user_id))
        changed = cur.rowcount

        if changed:
            apply_money_change(
                cur,
                hunter_user_id,
                hunter_user_name,
                reward,
                f"마니또 성공: {row['target_user_name']}",
                None,
                "마니또"
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log_error("COMPLETE_MANITTO_ERROR", e)
        changed = 0
    finally:
        conn.close()

    if not changed:
        return None

    try:
        grant_achievement_once(
            hunter_user_id,
            hunter_user_name,
            "first_manitto",
            "🎭 첫 마니또",
            5,
            f"target={row['target_user_name']}"
        )
    except Exception as e:
        print("MANITTO_ACHIEVEMENT_ERROR:", repr(e))

    manitto_type = row["manitto_type"] or "normal"
    if manitto_type == "gold":
        dm_title = "👑 황금 마니또 성공!"
        public_text = "👑 황금 마니또 성공!\n\n누군가가 황금 마니또를 달성했습니다!\n\n축하해주세요 🎉"
    else:
        dm_title = "🎭 마니또 미션 성공!"
        public_text = "🎭 누군가의 마니또 미션이 성공했습니다!\n\n축하해주세요 😊"

    dm_text = (
        f"{dm_title}\n\n"
        f"대상\n{row['target_user_name']}\n\n"
        f"달성 친밀도\n{int(row['required_score'] or MANITTO_REQUIRED_SCORE)} / {int(row['required_score'] or MANITTO_REQUIRED_SCORE)}\n\n"
        "🎁 랜덤 보상 획득!\n\n"
        f"💰 +{coin_text(reward)}\n\n"
        "축하합니다 😊"
    )

    try:
        delay_count = random.randint(10, 20)
        release_after_log_id = latest_chat_log_id(COUNT_SOURCE_ID) + delay_count
        queue_public_announcement(
            COUNT_SOURCE_ID,
            public_text,
            "manitto_success",
            release_after_log_id=release_after_log_id
        )
        print(
            "MANITTO_PUBLIC_NOTICE_QUEUED:",
            f"hunter={hunter_user_id}",
            f"delay={delay_count}",
            f"release_after_log_id={release_after_log_id}",
        )
    except Exception as e:
        print("MANITTO_PUBLIC_NOTICE_QUEUE_ERROR:", repr(e))

    return None


def send_manitto_reply(event, user_id, user_name):
    if is_private_chat(event):
        reply_many(event.reply_token, split_text_messages(manitto_status_text(user_id, user_name)))
    else:
        reply(
            event.reply_token,
            "🎭 마니또 정보는 꽃봇과 1:1 채팅에서 확인해주세요.\n\n"
            "개인정보 보호를 위해\n"
            "공개방에서는 표시되지 않습니다."
        )


# =========================
# 마니또 / 친밀도
# =========================
AFFINITY_REPLY_WINDOW_SECONDS = 180
AFFINITY_PAIR_COOLDOWN_SECONDS = 30
AFFINITY_CUMULATIVE_JAGIYA_SCORE = 500
AFFINITY_CUMULATIVE_JAGIYA_REWARD = 30  # 3코인
MANITTO_REQUIRED_SCORE = 15
MANITTO_REWARD_MIN = 15   # 1.5코인
MANITTO_REWARD_MAX = 60   # 6코인
MANITTO_TARGET_MAX_WEEKLY_ASSIGNED = 2  # 이번 주 같은 타겟 최대 배정 횟수
MANITTO_REROLL_LIMIT = 2  # 주간 마니또 변경 가능 횟수
GOLDEN_MANITTO_RATE = 5  # 5%


def parse_time_kst(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None


def pair_key(user_id_1, user_id_2):
    return tuple(sorted([user_id_1, user_id_2]))


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
        return None, "마니또 대상을 지정할 수 없습니다. 활성 유저가 부족하거나 이번 주 타겟 배정 제한에 걸렸습니다."

    manitto_type = "golden" if random.randint(1, 100) <= GOLDEN_MANITTO_RATE else "normal"
    reward_min = MANITTO_REWARD_MIN
    reward_max = 150 if manitto_type == "golden" else MANITTO_REWARD_MAX
    required_score = MANITTO_REQUIRED_SCORE

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



def get_cumulative_affinity_score(user_id_1, user_id_2):
    a, b = pair_key(user_id_1, user_id_2)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT total_score
    FROM affinity_cumulative_scores
    WHERE user_a = ? AND user_b = ?
    """, (a, b))
    row = cur.fetchone()
    conn.close()
    return int(row["total_score"] or 0) if row else 0


def grant_jagiya_achievement_if_ready(user_id_1, user_name_1, user_id_2, user_name_2, total_score):
    """
    누적 친밀도 500 이상을 상대별 최초 달성하면
    양쪽에게 '자기야' 업적과 3코인을 지급합니다.
    achievement_key에 상대 user_id를 포함해 같은 상대와는 1회만 지급합니다.
    """
    if int(total_score or 0) < AFFINITY_CUMULATIVE_JAGIYA_SCORE:
        return None

    paid = []
    for owner_id, owner_name, partner_id, partner_name in [
        (user_id_1, user_name_1, user_id_2, user_name_2),
        (user_id_2, user_name_2, user_id_1, user_name_1),
    ]:
        key = "jagiya"
        title = "💕 자기야"
        meta = f"partner_id={partner_id};partner_name={partner_name};total_affinity={total_score}"
        if grant_achievement_once(owner_id, owner_name, key, title, AFFINITY_CUMULATIVE_JAGIYA_REWARD, meta):
            paid.append(owner_name)

    if paid:
        return (
            "🏆 업적 달성!\n\n"
            "💕 자기야\n\n"
            f"{user_name_1}님과 {user_name_2}님이\n"
            f"누적 친밀도 {total_score}을 달성했습니다.\n\n"
            f"보상: 각 {coin_text(AFFINITY_CUMULATIVE_JAGIYA_REWARD)}"
        )
    return None




def operator_affinity_report_text(keyword="", min_score=50):
    keyword = (keyword or "").strip()
    target = None

    if keyword:
        target, err = resolve_active_user_by_nickname(keyword, purpose="대상")
        if err:
            return err

    week_start, week_end = event_week_key()
    conn = db()
    cur = conn.cursor()

    if target:
        target_user_id = target["user_id"]
        target_user_name = target["user_name"]
        cur.execute("""
        SELECT user_a, user_b, user_a_name, user_b_name, score, updated_at
        FROM affinity_scores
        WHERE week_start = ?
          AND score >= ?
          AND (user_a = ? OR user_b = ?)
        ORDER BY score DESC, updated_at DESC
        """, (week_start, min_score, target_user_id, target_user_id))
        weekly_rows = cur.fetchall()

        cur.execute("""
        SELECT user_a, user_b, user_a_name, user_b_name, total_score, updated_at
        FROM affinity_cumulative_scores
        WHERE total_score >= ?
          AND (user_a = ? OR user_b = ?)
        ORDER BY total_score DESC, updated_at DESC
        """, (min_score, target_user_id, target_user_id))
        cumulative_rows = cur.fetchall()
    else:
        target_user_id = None
        target_user_name = "전체"
        cur.execute("""
        SELECT user_a, user_b, user_a_name, user_b_name, score, updated_at
        FROM affinity_scores
        WHERE week_start = ?
          AND score >= ?
        ORDER BY score DESC, updated_at DESC
        """, (week_start, min_score))
        weekly_rows = cur.fetchall()

        cur.execute("""
        SELECT user_a, user_b, user_a_name, user_b_name, total_score, updated_at
        FROM affinity_cumulative_scores
        WHERE total_score >= ?
        ORDER BY total_score DESC, updated_at DESC
        """, (min_score,))
        cumulative_rows = cur.fetchall()

    conn.close()

    def pair_label(row, target_id=None):
        if target_id:
            return row["user_b_name"] if row["user_a"] == target_id else row["user_a_name"]
        return f"{row['user_a_name']} ↔ {row['user_b_name']}"

    lines = [
        "💞 운영진 친밀도 확인",
        f"대상: {target_user_name}",
        f"기준: {min_score} 이상",
        "",
        "이번 주 친밀도",
        f"기간: {week_start} ~ {week_end}",
    ]

    if not weekly_rows:
        lines.append("기록 없음")
    else:
        for i, row in enumerate(weekly_rows, 1):
            lines.append(f"{i}. {pair_label(row, target_user_id)} - {int(row['score'] or 0)}")

    lines += ["", "누적 친밀도"]
    if not cumulative_rows:
        lines.append("기록 없음")
    else:
        for i, row in enumerate(cumulative_rows, 1):
            lines.append(f"{i}. {pair_label(row, target_user_id)} - {int(row['total_score'] or 0)}")

    return "\n".join(lines)


def manitto_status_text_from_row(row, user_id):
    progress = get_affinity_score(user_id, row["target_user_id"], row["week_start"])
    completed = int(row["completed"] or 0) == 1
    status = "완료" if completed else "진행중"
    reroll_count = int(row["reroll_count"] or 0) if "reroll_count" in row.keys() else 0
    reward = int(row["reward"] or 0) if "reward" in row.keys() else 0
    if reward <= 0:
        reward = random.randint(int(row["reward_min"] or MANITTO_REWARD_MIN), int(row["reward_max"] or MANITTO_REWARD_MAX))
    return (
        "🎭 이번 주 마니또\n\n"
        f"대상: {row['target_user_name']}\n"
        f"상태: {status}\n"
        f"진행도: {progress} / {row['required_score']}\n"
        f"보상: {coin_text(reward)}\n\n"
        f"남은 변경: {max(0, MANITTO_REROLL_LIMIT - reroll_count)} / {MANITTO_REROLL_LIMIT}\n"
        "변경: /마니또변경"
    )


def send_manitto_reroll_reply(event, user_id, user_name):
    if not is_private_chat(event):
        reply(event.reply_token, "🎭 마니또 변경은 꽃봇 1:1 채팅에서만 가능합니다.")
        return

    row, err = ensure_weekly_manitto(user_id, user_name)
    if err:
        reply(event.reply_token, err)
        return
    if int(row["completed"] or 0) == 1:
        reply(event.reply_token, "❌ 완료된 마니또는 변경할 수 없습니다.")
        return

    reroll_count = int(row["reroll_count"] or 0) if "reroll_count" in row.keys() else 0
    if reroll_count >= MANITTO_REROLL_LIMIT:
        reply(event.reply_token, f"❌ 이번 주 변경 횟수를 모두 사용했습니다.\n\n사용 횟수: {reroll_count} / {MANITTO_REROLL_LIMIT}")
        return

    exclude = {user_id, row["target_user_id"]}
    history = row["reroll_history"] if "reroll_history" in row.keys() else None
    if history:
        exclude.update(x for x in str(history).split(',') if x)

    target = manitto_target_candidates(user_id, exclude)
    if not target:
        target = manitto_target_candidates(user_id, {user_id, row["target_user_id"]})
    if not target:
        reply(event.reply_token, "🎭 변경 가능한 새 대상이 없습니다.")
        return

    new_history = list(exclude - {user_id})
    week_start, _ = event_week_key()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE manitto_assignments
    SET target_user_id = ?,
        target_user_name = ?,
        reroll_count = COALESCE(reroll_count, 0) + 1,
        reroll_history = ?,
        updated_at = ?
    WHERE week_start = ? AND hunter_user_id = ?
    """, (target["user_id"], target["user_name"], ",".join(new_history), now_str(), week_start, user_id))
    conn.commit()
    conn.close()

    reply(
        event.reply_token,
        "🎭 마니또 변경 완료\n\n"
        f"기존 대상: {row['target_user_name']}\n"
        f"새로운 대상: {target['user_name']}\n\n"
        f"남은 변경 횟수: {max(0, MANITTO_REROLL_LIMIT - reroll_count - 1)} / {MANITTO_REROLL_LIMIT}"
    )



# =========================
# 족보 / 코인 표시
# =========================
def strip_coin_suffix(line):
    """
    족보를 다시 붙여넣을 때 기존 코인 표기를 전부 제거한다.

    예)
    🪩미트🪩 남 37 강원 철원 💰21.8 -> 🪩미트🪩 남 37 강원 철원
    28망치🏁 남 서울 광진 / 용왕 💰1.7 -> 28망치🏁 남 서울 광진 / 용왕

    저장할 때는 기존 족보에 붙어 있던 코인을 무시하고,
    /족보 조회 시 현재 DB 잔액 기준으로 다시 붙인다.
    """
    value = str(line)

    # 💰21.8 / 💰 21.8 / 💰21.8코인 / 💰 21.8 코인 전부 제거
    value = re.sub(r"\s*💰\s*[-+]?\d+(?:\.\d+)?\s*(?:코인)?", "", value)

    # 혹시 텍스트로 붙은 코인 표기도 제거: 21.8코인
    value = re.sub(r"\s*[-+]?\d+(?:\.\d+)?\s*코인\b", "", value)

    # 제거 후 남는 공백 정리
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.rstrip()


def normalize_genealogy_content(content):
    text_value = str(content or "")

    # LINE/복사 과정에서 실제 줄바꿈이 아니라 문자 \\n 으로 들어온 경우 복구
    text_value = text_value.replace("\\r\\n", "\n").replace("\\n", "\n")
    text_value = text_value.replace("\r\n", "\n").replace("\r", "\n")

    # 실수로 본문 앞에 /족보입력, /족보저장 명령어를 같이 붙여넣은 경우 제거
    text_value = text_value.strip()
    while True:
        stripped = text_value.lstrip()
        lowered = stripped.lower()
        removed = False
        for cmd in ["/족보입력", "/족보저장"]:
            if lowered.startswith(cmd):
                stripped = stripped[len(cmd):].lstrip()
                text_value = stripped
                removed = True
                break
        if not removed:
            break

    lines = text_value.split("\n")
    return "\n".join(strip_coin_suffix(line) for line in lines).strip()


def save_genealogy_content(content, staff_user_name=""):
    content = normalize_genealogy_content(content)
    if not content:
        return False, "저장할 족보 내용이 없습니다."

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO genealogy_text (id, content, updated_by, updated_at)
    VALUES (1, ?, ?, ?)
    ON CONFLICT(id)
    DO UPDATE SET
        content = excluded.content,
        updated_by = excluded.updated_by,
        updated_at = excluded.updated_at
    """, (content, staff_user_name, now_str()))
    conn.commit()
    conn.close()
    return True, (
        "📖 족보 저장 완료\n\n"
        "붙여넣은 족보 안의 기존 💰코인 표기는 무시하고 저장했습니다.\n"
        "이후 /족보 조회 시 현재 DB 잔액 기준으로 코인이 다시 표시됩니다.\n\n"
        "/족보 또는 /족보보기 로 확인할 수 있습니다."
    )


def get_genealogy_content():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT content FROM genealogy_text WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return row["content"] if row else ""


def genealogy_coin_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT u.user_id, u.user_name, COALESCE(c.balance, 0) AS balance
    FROM users u
    JOIN currency c ON u.user_id = c.user_id
    WHERE COALESCE(c.balance, 0) > 0
      AND COALESCE(u.is_active, 1) = 1
    ORDER BY LENGTH(u.user_name) DESC, u.user_name ASC
    """)
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    prepared = []
    for row in rows:
        name = row.get("user_name") or ""
        clean_name = clean_keyword(name)
        if not clean_name:
            continue
        # 숫자/나이/이모티콘이 섞인 족보 닉네임 매칭을 위해 이름 안의 한글/숫자 토큰도 같이 준비
        prepared.append({
            "user_id": row["user_id"],
            "user_name": name,
            "clean_name": clean_name,
            "balance": int(row["balance"]),
        })
    return prepared


def genealogy_first_member_key(line):
    """
    족보 한 줄에서 '맨 앞 사람 닉네임'만 추출한다.
    소개자/동반자 이름 때문에 코인이 겹쳐 붙는 것을 방지한다.

    예)
    26요뜨🔻 대전 / 미트        -> 26요뜨
    🪩미트🪩  남 37 강원 철원   -> 미트
    37이안🔹 경기 파주 / 미트 소다동반 -> 37이안
    """
    base_line = strip_coin_suffix(line).strip()
    if not base_line:
        return ""

    # 구분선/제목/설명 줄은 제외
    if base_line.startswith(("---", "——", "━━━━━━━━", "설명은", "방장 ", "관리자 ", "인증자 ", "남미클자", "여미클자", "노미클자")):
        return ""
    if base_line in {"🔹족보🔻", "🪩방장🪩", "🔗관리자🔗", "🏁인증자🏁"}:
        return ""
    if base_line.startswith(("🔹남자", "🔰노미클", "🔻여자", "👾외출", "STD검사", "피검사", "외출 ", "바쁨 ", "경고 ", "벙금지", "무제한", "미션클리어", "봇등록권", "칭호권", "닉변권", "임티권")):
        return ""

    first = base_line.split()[0] if base_line.split() else ""
    return clean_keyword(first)


def coin_for_genealogy_line(line, coin_users):
    first_key = genealogy_first_member_key(line)
    if not first_key:
        return None

    for user in coin_users:
        cn = user["clean_name"]
        if not cn:
            continue

        # 줄 맨 앞 닉네임만 기준으로 매칭.
        # '26요뜨 ... / 미트'에서 미트 코인이 붙는 문제 방지.
        if first_key == cn or first_key.startswith(cn) or cn.startswith(first_key):
            return user["balance"]

    return None


def genealogy_text_with_coins():
    content = get_genealogy_content()

    # 예전 버전에서 문자 "\\n" 형태로 저장된 족보도 출력 시 정상 줄바꿈으로 복구한다.
    content = normalize_genealogy_content(content)

    if not content:
        return "저장된 족보가 없습니다.\n\n운영진이 아래 형식으로 먼저 저장해주세요.\n\n/족보입력\n족보 내용 붙여넣기"

    coin_users = genealogy_coin_users()
    lines = []
    for line in content.split("\n"):
        base = strip_coin_suffix(line)
        balance = coin_for_genealogy_line(base, coin_users)
        if balance and balance > 0:
            lines.append(f"{base} 💰{points_to_coin(balance)}")
        else:
            lines.append(base)

    return "\n".join(lines).strip()

# =========================
# 프로필 / 칭호
# =========================
def get_user_row_by_keyword_or_self(keyword, default_user_id=None, default_user_name=None):
    if keyword:
        rows = find_users(keyword, limit=5)
        if not rows:
            return None, f"검색 결과가 없습니다.\n\n검색어: {keyword}"
        if len(rows) > 1:
            lines = ["검색 결과가 여러 명입니다:", ""]
            for idx, row in enumerate(rows, 1):
                lines.append(f"{idx}. {row['user_name']}")
            lines += ["", "더 정확한 닉네임으로 다시 입력해주세요."]
            return None, "\n".join(lines)
        return rows[0], None
    if not default_user_id:
        return None, "USER_ID를 확인할 수 없습니다."
    row = get_user_by_id(default_user_id)
    if row:
        return dict(row), None
    return {"user_id": default_user_id, "user_name": default_user_name or "알 수 없음", "is_active": 1}, None


def get_achievement_count(user_id):
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM achievements WHERE user_id = ?", (user_id,))
    row = cur.fetchone(); conn.close()
    return int(row["cnt"] or 0) if row else 0


def get_attendance_count(user_id):
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM attendance WHERE user_id = ?", (user_id,))
    row = cur.fetchone(); conn.close()
    return int(row["cnt"] or 0) if row else 0


def get_best_affinity(user_id):
    conn = db(); cur = conn.cursor()
    cur.execute("""
    SELECT user_a, user_b, user_a_name, user_b_name, total_score
    FROM affinity_cumulative_scores
    WHERE user_a = ? OR user_b = ?
    ORDER BY total_score DESC, updated_at DESC
    LIMIT 1
    """, (user_id, user_id))
    row = cur.fetchone(); conn.close()
    if not row:
        return None, 0
    other = row["user_b_name"] if row["user_a"] == user_id else row["user_a_name"]
    return other, int(row["total_score"] or 0)


def get_public_title(user_id):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
        SELECT title
        FROM user_titles
        WHERE user_id = ? AND is_active = 1
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    conn.close()
    if row and row["title"]:
        return row["title"]
    return "칭호 없음"


def set_user_title(user_id, user_name, title, staff_name):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE user_titles SET is_active = 0, updated_at = ? WHERE user_id = ?", (now_str(), user_id))
    cur.execute("""
    INSERT INTO user_titles (user_id, user_name, title, is_active, created_by, created_at, updated_at)
    VALUES (?, ?, ?, 1, ?, ?, ?)
    """, (user_id, user_name, title, staff_name, now_str(), now_str()))
    conn.commit()
    conn.close()


def clear_user_title(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE user_titles SET is_active = 0, updated_at = ? WHERE user_id = ?", (now_str(), user_id))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def title_list_text():
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
        SELECT user_name, title, created_by, updated_at
        FROM user_titles
        WHERE is_active = 1
        ORDER BY updated_at DESC, user_name ASC
        """)
        rows = cur.fetchall()
    except Exception:
        rows = []
    conn.close()
    if not rows:
        return "👑 등록된 칭호가 없습니다."
    lines = ["👑 칭호 목록", ""]
    for i, row in enumerate(rows, 1):
        by = f" / 지급: {row['created_by']}" if row['created_by'] else ""
        lines.append(f"{i}. {row['user_name']} - {row['title']}{by}")
    return "\n".join(lines)


def admin_user_detail_text(keyword):
    rows = find_users(keyword, limit=5)
    if not rows:
        return f"검색 결과가 없습니다.\n\n검색어: {keyword}"
    if len(rows) > 1:
        lines = [f"검색 결과가 여러 명입니다: {keyword}", ""]
        for i, row in enumerate(rows, 1):
            status = "활성" if int(row.get("is_active", 1)) == 1 else "비활성"
            lines.append(f"{i}. {row['user_name']} / {status}\n   USER_ID: {row['user_id']}")
        lines.append("\n더 정확한 닉네임으로 다시 입력해주세요.")
        return "\n".join(lines)
    user = rows[0]
    uid = user["user_id"]
    status = "활성" if int(user.get("is_active", 1)) == 1 else "비활성"
    best_name, best_score = get_best_affinity(uid)
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM purchases WHERE user_id = ? AND status = 'owned'", (uid,))
    owned = int(cur.fetchone()["cnt"] or 0)
    cur.execute("SELECT COUNT(*) AS cnt FROM purchases WHERE user_id = ? AND status = 'used'", (uid,))
    used = int(cur.fetchone()["cnt"] or 0)
    conn.close()
    lines = [
        "🔎 유저 상세",
        "",
        f"닉네임: {user['user_name']}",
        f"상태: {status}",
        f"USER_ID: {uid}",
        "",
        f"💰 코인: {coin_text(get_balance(uid))}",
        f"📅 출석: {get_attendance_count(uid)}일",
        f"🏆 업적: {get_achievement_count(uid)}개",
        f"👑 칭호: {get_public_title(uid)}",
        f"💕 최고 친밀도: {best_name} ({best_score})" if best_name else "💕 최고 친밀도: 기록 없음",
        f"🎁 보유상품: 미사용 {owned}개 / 사용완료 {used}개",
    ]
    return "\n".join(lines)


def grant_item_to_user(keyword, item_name, staff_name):
    target, err = resolve_active_user_by_nickname(keyword, purpose="대상")
    if err:
        return False, err
    purchase_id = add_reward_purchase(target["user_id"], target["user_name"], item_name)
    return True, (
        "🎁 아이템 지급 완료\n\n"
        f"대상: {target['user_name']}\n"
        f"상품: {item_name}\n"
        f"구매번호: #{purchase_id}\n"
        f"처리: {staff_name}"
    )

# =========================
# WEBHOOK
# =========================

# =========================
# 자동 주간정산 스케줄러
# =========================
def run_weekly_settlement_auto():
    """
    매주 일요일 23:50(KST)에 주간정산을 1회 자동 실행합니다.
    system_flags로 중복 실행을 방지합니다.
    """
    date_str = today()
    week_start, week_end = week_range_for_today()
    flag_key = f"auto_weekly_settlement:{date_str}"

    try:
        if get_system_flag(flag_key):
            return
    except Exception as e:
        log_error("AUTO_WEEKLY_FLAG_READ_ERROR", e)
        return

    try:
        result_text = None

        if "weekly_settlement_text" in globals():
            result_text = weekly_settlement_text(COUNT_SOURCE_ID)
        elif "settle_weekly_rewards" in globals():
            result_text = settle_weekly_rewards(COUNT_SOURCE_ID)
        else:
            result_text = "⚠️ 자동 주간정산 실패\n\n주간정산 함수를 찾지 못했습니다."

        record_settlement_run(date_str, week_start, week_end, "done", result_text)
        set_system_flag(flag_key, "done")

        notify_text = "🏆 자동 주간정산 완료\n\n" + str(result_text)
        print("[PUSH_DISABLED] AUTO_WEEKLY_SETTLEMENT_NOTIFY", notify_text)
        print("AUTO_WEEKLY_SETTLEMENT_DONE:", date_str)

    except Exception as e:
        log_error("AUTO_WEEKLY_SETTLEMENT_ERROR", e)
        try:
            record_settlement_run(date_str, week_start, week_end, "error", repr(e))
        except Exception as record_error:
            log_error("AUTO_WEEKLY_SETTLEMENT_RECORD_ERROR", record_error)


def weekly_settlement_scheduler_loop():
    """
    KST 기준 매주 일요일 23:50에 자동 주간정산.
    """
    while True:
        try:
            now = datetime.now(KST)
            if now.weekday() == 6 and now.hour == 23 and now.minute == 50:
                run_weekly_settlement_auto()
                time.sleep(70)
            else:
                time.sleep(20)
        except Exception as e:
            log_error("WEEKLY_SETTLEMENT_SCHEDULER_ERROR", e)
            time.sleep(60)


def start_weekly_settlement_scheduler():
    t = threading.Thread(target=weekly_settlement_scheduler_loop, daemon=True)
    t.start()

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
        log_error("CALLBACK_ERROR", e)
        abort(500)

    return "OK"


# =========================
# 최종 운영 보조 함수 v10
# =========================
def economy_status_text():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(balance), 0) AS total, COUNT(*) AS cnt FROM currency c JOIN users u ON u.user_id = c.user_id WHERE COALESCE(u.is_active, 1) = 1")
    row = cur.fetchone()
    circulating = int(row["total"] or 0)
    cnt = int(row["cnt"] or 0)
    cur.execute("SELECT COALESCE(SUM(amount), 0) AS issued FROM currency_logs WHERE amount > 0")
    issued = int((cur.fetchone() or {"issued": 0})["issued"] or 0)
    cur.execute("SELECT COALESCE(SUM(-amount), 0) AS spent FROM currency_logs WHERE amount < 0")
    spent = int((cur.fetchone() or {"spent": 0})["spent"] or 0)
    cur.execute("""
    SELECT u.user_name, c.balance
    FROM currency c JOIN users u ON u.user_id = c.user_id
    WHERE COALESCE(u.is_active, 1) = 1
    ORDER BY c.balance DESC
    LIMIT 1
    """)
    top = cur.fetchone()
    conn.close()
    avg = int(round(circulating / cnt)) if cnt else 0
    return "\n".join([
        "💰 경제 현황", "",
        f"총 발행량: {coin_text(issued)}",
        f"총 사용량: {coin_text(spent)}",
        f"현재 유통량: {coin_text(circulating)}",
        f"활성 보유자: {cnt}명",
        f"평균 보유: {coin_text(avg)}",
        f"최고 보유자: {(top['user_name'] + ' ' + coin_text(top['balance'])) if top else '-'}",
    ])


def coin_audit_text(keyword):
    try:
        keyword = str(keyword or "").strip()
        if not keyword:
            return "사용법: /코인검증 닉네임"

        target, err = resolve_active_user_by_nickname(keyword, purpose="대상")
        if err:
            return "💰 코인검증 실패\n\n" + err

        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(balance, 0) AS balance FROM currency WHERE user_id = ?", (target["user_id"],))
        balance_row = cur.fetchone()
        balance = int(balance_row["balance"] or 0) if balance_row else 0

        cur.execute("""
        SELECT
            COALESCE(SUM(amount), 0) AS log_total,
            COUNT(*) AS log_count,
            MAX(created_at) AS last_log
        FROM currency_logs
        WHERE user_id = ?
        """, (target["user_id"],))
        log_row = cur.fetchone()
        log_total = int(log_row["log_total"] or 0) if log_row else 0
        log_count = int(log_row["log_count"] or 0) if log_row else 0
        last_log = log_row["last_log"] if log_row else None
        conn.close()

        diff = balance - log_total
        status = "정상" if diff == 0 else "확인 필요"
        return "\n".join([
            "💰 코인검증",
            "",
            f"대상: {target['user_name']}",
            f"현재 잔액: {coin_text(balance)}",
            f"로그 합계: {coin_text(log_total)}",
            f"차이: {coin_text(diff)}",
            f"로그 수: {log_count}건",
            f"최근 로그: {last_log or '-'}",
            "",
            f"상태: {status}",
        ])
    except Exception as e:
        log_error("COIN_AUDIT_ERROR", e)
        return "💰 코인검증 중 문제가 생겼어요. 최근오류를 확인해 주세요."


def recent_errors_text(limit=10):
    try:
        limit = max(1, min(30, int(limit or 10)))
    except Exception:
        limit = 10

    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT context, detail, created_at
        FROM bot_errors
        ORDER BY id DESC
        LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()

        lines = ["🧯 최근오류", ""]
        if not rows:
            lines.append("저장된 오류 로그가 없습니다.")
        else:
            for i, row in enumerate(rows, 1):
                detail = str(row["detail"] or "")
                if len(detail) > 180:
                    detail = detail[:180] + "..."
                lines.append(f"{i}. {row['created_at']} / {row['context']}")
                lines.append(f"   {detail}")
        return "\n".join(lines)
    except Exception as e:
        print("RECENT_ERRORS_TEXT_ERROR:", repr(e))
        return "🧯 최근오류를 불러오지 못했습니다."


def record_settlement_run(date_str, week_start, week_end, status, summary):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO settlement_runs (
        date, week_start, week_end, status, summary, created_at
    ) VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(date, week_start, week_end)
    DO UPDATE SET
        status = excluded.status,
        summary = excluded.summary,
        created_at = excluded.created_at
    """, (date_str, week_start, week_end, status, str(summary)[:1800], now_str()))
    conn.commit()
    conn.close()


def settlement_audit_text():
    try:
        week_start, week_end = week_range_for_today()
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(reward), 0) AS total FROM weekly_rewards WHERE week_start = ? AND week_end = ?", (week_start, week_end))
        weekly = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(reward), 0) AS total FROM heart_pick_rewards WHERE week_start = ? AND week_end = ?", (week_start, week_end))
        heart = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(prize), 0) AS total FROM sns_lucky_draw_prizes WHERE week_start = ? AND week_end = ?", (week_start, week_end))
        lucky_prizes = cur.fetchone()
        cur.execute("SELECT participants, prize, burned, created_at FROM sns_lucky_draw_results WHERE week_start = ? AND week_end = ?", (week_start, week_end))
        lucky = cur.fetchone()
        cur.execute("SELECT status, summary, created_at FROM settlement_runs WHERE week_start = ? AND week_end = ? ORDER BY id DESC LIMIT 1", (week_start, week_end))
        run = cur.fetchone()
        conn.close()

        lines = [
            "🧾 정산검증",
            f"기간: {week_start} ~ {week_end}",
            "",
            f"마디수 보상: {int(weekly['cnt'] or 0)}건 / {coin_text(int(weekly['total'] or 0))}",
            f"설렘픽 보상: {int(heart['cnt'] or 0)}건 / {coin_text(int(heart['total'] or 0))}",
            f"럭키드로우 보상: {int(lucky_prizes['cnt'] or 0)}건 / {coin_text(int(lucky_prizes['total'] or 0))}",
        ]
        if lucky:
            lines.append(f"럭키드로우 결과: 참여 {int(lucky['participants'] or 0)}명 / 풀 {coin_text(int(lucky['prize'] or 0))} / 소각 {coin_text(int(lucky['burned'] or 0))}")
            lines.append(f"럭키드로우 추첨: {lucky['created_at']}")
        else:
            lines.append("럭키드로우 결과: 아직 없음")

        if run:
            lines += ["", f"자동정산 기록: {run['status']} / {run['created_at']}"]
        else:
            lines += ["", "자동정산 기록: 아직 없음"]

        return "\n".join(lines)
    except Exception as e:
        log_error("SETTLEMENT_AUDIT_ERROR", e)
        return "🧾 정산검증 중 문제가 생겼어요. 최근오류를 확인해 주세요."


def snapshot_user_data(user_id):
    conn = db()
    cur = conn.cursor()
    tables = [
        "users", "currency", "currency_logs", "revival_claims", "purchases", "attendance", "attendance_streak_rewards", "danbung_attendance", "mission_claims",
        "hidden_rewards", "gacha_settings", "gacha_pity", "gacha_pieces", "gacha_weekly_counts",
        "weekly_rewards", "sns_lucky_draw_entries", "achievements", "chat_logs", "counts",
        "heart_pick_rewards", "chemistry_rewards", "truth_game_sessions", "truth_game_resets",
    ]
    snap = {}
    for table in tables:
        try:
            cur.execute(f"SELECT * FROM {table} WHERE user_id = ?", (user_id,))
            snap[table] = [dict(r) for r in cur.fetchall()]
        except Exception:
            snap[table] = []
    for table, col in [
        ("mention_logs", "sender_user_id"),
        ("mention_logs", "target_user_id"),
        ("anonymous_pokes", "sender_user_id"),
        ("anonymous_pokes", "target_user_id"),
        ("heart_picks", "sender_user_id"),
        ("heart_picks", "target_user_id"),
        ("chemistry_signals", "sender_user_id"),
        ("chemistry_signals", "target_user_id"),
        ("chemistry_rewards", "matched_user_id"),
        ("truth_game_sessions", "requester_user_id"),
        ("affinity_scores", "user_a"),
        ("affinity_scores", "user_b"),
        ("affinity_cumulative_scores", "user_a"),
        ("affinity_cumulative_scores", "user_b"),
        ("manitto_assignments", "hunter_user_id"),
        ("manitto_assignments", "target_user_id"),
    ]:
        key = f"{table}:{col}"
        try:
            cur.execute(f"SELECT * FROM {table} WHERE {col} = ?", (user_id,))
            snap[key] = [dict(r) for r in cur.fetchall()]
        except Exception:
            snap[key] = []
    conn.close()
    return snap


def move_user_to_deleted(user_id, user_name, deleted_by):
    snap = snapshot_user_data(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO deleted_users (original_user_id, user_name, deleted_by, deleted_at, snapshot_json)
    VALUES (?, ?, ?, ?, ?)
    """, (user_id, user_name, deleted_by, now_str(), json.dumps(snap, ensure_ascii=False)))
    conn.commit()
    conn.close()
    delete_users_by_ids({user_id: user_name})


def deleted_users_text():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, user_name, deleted_by, deleted_at FROM deleted_users ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return "🗑 삭제유저 목록이 없습니다."
    lines = ["🗑 삭제유저 목록", ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. #{row['id']} {row['user_name']} / 삭제일: {row['deleted_at']} / 삭제자: {row['deleted_by'] or '-'}")
    lines += ["", "복구: /삭제복구 번호 또는 /삭제복구 #ID"]
    return "\n".join(lines)


def restore_deleted_user_by_index(arg):
    conn = db()
    cur = conn.cursor()
    if str(arg).startswith('#'):
        cur.execute("SELECT * FROM deleted_users WHERE id = ?", (str(arg).lstrip('#'),))
    else:
        try:
            idx = int(arg)
        except Exception:
            conn.close()
            return False, "사용법: /삭제복구 번호"
        cur.execute("SELECT * FROM deleted_users ORDER BY id DESC LIMIT 1 OFFSET ?", (idx - 1,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "복구할 삭제유저를 찾을 수 없습니다."
    snap = json.loads(row["snapshot_json"] or "{}")
    restored = 0
    for table, records in snap.items():
        if ':' in table:
            continue
        for rec in records:
            cols = list(rec.keys())
            placeholders = ','.join('?' for _ in cols)
            col_sql = ','.join(cols)
            try:
                cur.execute(f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})", [rec[c] for c in cols])
                restored += 1
            except Exception as e:
                print("RESTORE_SKIP", table, e)
    cur.execute("DELETE FROM deleted_users WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True, f"✅ 삭제유저 복구 완료\n\n대상: {row['user_name']}\n복구 레코드: {restored}개"


def calculate_manitto_goal_and_rewards(hunter_user_id, target_user_id, manitto_type):
    affinity = get_cumulative_affinity_between(hunter_user_id, target_user_id)
    if affinity >= 500:
        multiplier = 2.0
    elif affinity >= 400:
        multiplier = 1.75
    elif affinity >= 300:
        multiplier = 1.5
    elif affinity >= 200:
        multiplier = 1.2
    elif affinity >= 100:
        multiplier = 1.1
    else:
        multiplier = 1.0
    required = min(30, max(15, int(round(MANITTO_REQUIRED_SCORE * multiplier))))
    min_reward, max_reward = manitto_reward_range(manitto_type)
    if affinity < 100:
        bonus = 1.5
    elif affinity < 200:
        bonus = 1.3
    elif affinity < 300:
        bonus = 1.1
    else:
        bonus = 1.0
    return required, int(round(min_reward * bonus)), int(round(max_reward * bonus))


def get_cumulative_affinity_between(user_a, user_b):
    a, b = pair_key(user_a, user_b)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT total_score FROM affinity_cumulative_scores WHERE user_a = ? AND user_b = ?", (a, b))
    row = cur.fetchone()
    conn.close()
    return int(row["total_score"] or 0) if row else 0


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

    public_notices = []

    if user_id:
        upsert_user(user_id, user_name, source_id)

    # 메인방 + 운영진방 둘 다 마디수/로그 카운트
    if source_id in count_source_ids() and user_id:
        add_count(date_str, source_id, user_id, user_name)

        # 당일 로그상 순번 계산을 위해 보상 체크 전에 먼저 저장
        if isinstance(event.message, TextMessageContent):
            message_type = "text"
            message_text = event.message.text or ""
        else:
            message_type = type(event.message).__name__
            message_text = ""

        current_chat_log_id = save_chat_log(
            date_str,
            source_id,
            user_id,
            user_name,
            message_type,
            message_text
        )

        try:
            if message_type == "text":
                process_mentions(date_str, source_id, user_id, user_name, message_text)
        except Exception as e:
            log_error("MENTION_PROCESS_ERROR", e)

        try:
            if source_id == COUNT_SOURCE_ID:
                public_notices.extend(pop_public_announcements(source_id, current_chat_log_id))
        except Exception as e:
            log_error("PUBLIC_ANNOUNCEMENT_ERROR", e)

        # 히든 미션 자동 체크
        try:
            hidden_1000_msg = check_hidden_1000_reward(date_str, source_id, user_id, user_name)
            if hidden_1000_msg:
                public_notices.append(hidden_1000_msg)
            hidden_2000_msg = check_hidden_2000_reward(date_str, source_id, user_id, user_name)
            if hidden_2000_msg:
                public_notices.append(hidden_2000_msg)
            public_notices.extend(check_daily_chat_jackpot_rewards(date_str, source_id, user_id, user_name))
            for achievement_name, reward in check_chatter_achievements(date_str, source_id, user_id, user_name):
                public_notices.append(achievement_message(achievement_name, user_name, reward))
        except Exception as e:
            log_error("HIDDEN_REWARD_ERROR", e)

    if not isinstance(event.message, TextMessageContent):
        if public_notices:
            reply_many(event.reply_token, split_text_messages("\n\n".join(dict.fromkeys(public_notices))))
        return

    text = simplified_command_text((event.message.text or "").strip())

    if public_notices and text.startswith("/"):
        reply_many(event.reply_token, split_text_messages("\n\n".join(dict.fromkeys(public_notices))))
        return

    # 운영진 전용 명령어 통합 차단
    # 일반 유저가 운영 명령어를 입력하면 모든 기능에서 같은 경고 문구만 출력한다.
    if is_operator_command(text) and not is_staff(user_id):
        reply(event.reply_token, operator_only_warning())
        return

    if is_operator_command(text) and is_staff(user_id) and text not in ("/방정보", "/버전") and source_id not in ADMIN_SOURCE_IDS:
        reply(event.reply_token, "⛔ 운영방에서만 사용 가능합니다.")
        return

    # /족보입력 이후 다음 메시지를 족보 본문으로 저장
    if user_id in JOKBO_PENDING:
        if source_id not in ADMIN_SOURCE_IDS or not is_staff(user_id):
            JOKBO_PENDING.pop(user_id, None)
            reply(event.reply_token, operator_only_warning())
            return

        # 명령어를 잘못 입력한 경우 족보로 저장하지 않음
        if text.startswith("/"):
            JOKBO_PENDING.pop(user_id, None)
            reply(event.reply_token, "족보 입력을 취소했습니다. 다시 입력하려면 /족보입력 을 사용해주세요.")
            return

        ok, msg = save_genealogy_content(text, user_name)
        JOKBO_PENDING.pop(user_id, None)
        reply(event.reply_token, msg)
        return

    try:
        affinity_msg = process_affinity_message(source_id, user_id, user_name, text)
        if public_notices or affinity_msg:
            notice_text = "\n\n".join(dict.fromkeys(public_notices + ([affinity_msg] if affinity_msg else [])))
            reply_many(event.reply_token, split_text_messages(notice_text))
            return
    except Exception as e:
        log_error("AFFINITY_PROCESS_ERROR", e)

    # 토요일 21시 자동 스케줄러가 기본 처리합니다. 메시지 수신 시에도 보조 확인합니다.
    try:
        maybe_auto_lucky_draw()
    except Exception as e:
        log_error("SNS_LUCKY_AUTO_ERROR", e)


    # =========================
    # 운영진 명령어
    # =========================
    if text == "/운영명령어":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(operator_commands_text()))
        return

    if text == "/방정보":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply(
            event.reply_token,
            "🏠 방 정보\n\n"
            f"SOURCE_ID: {source_id}\n"
            f"USER_ID: {user_id or '-'}\n"
            f"USER_NAME: {user_name}\n\n"
            f"ADMIN_SOURCE_ID: {ADMIN_SOURCE_ID or '-'}\n"
            f"COUNT_SOURCE_ID: {COUNT_SOURCE_ID or '-'}\n"
            f"ADMIN_SOURCE_IDS: {', '.join(sorted(ADMIN_SOURCE_IDS)) if ADMIN_SOURCE_IDS else '-'}\n\n"
            f"운영방 여부: {'✅ YES' if source_id in ADMIN_SOURCE_IDS else '❌ NO'}\n"
            f"운영자 여부: {'✅ YES' if is_staff(user_id) else '❌ NO'}\n"
            f"BOT_VERSION: {BOT_VERSION}"
        )
        return

    if text == "/버전":
        reply(
            event.reply_token,
            "🤖 S.N.S 꽃봇\n\n"
            f"버전: {BOT_VERSION}\n"
            "빌드: v10.5\n"
            "환경변수: ADMIN_SOURCE_ID / COUNT_SOURCE_ID / ADMIN_USER_IDS / OPERATOR_USER_IDS"
        )
        return

    if text.startswith("/DM테스트 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply(
            event.reply_token,
            "📩 DM 테스트는 비활성화되어 있습니다.\n\n"
            "개인 기능은 사용자가 꽃봇 1:1 채팅에서 직접 명령어를 입력해야 합니다."
        )
        return

    if text == "/DB상태":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        conn = db()
        cur = conn.cursor()
        counts = []
        for table in ["users", "counts", "currency", "currency_logs", "revival_claims", "purchases", "attendance", "danbung_attendance", "mission_claims", "weekly_rewards", "settlement_runs", "bot_errors", "manitto_assignments", "affinity_scores", "mention_logs", "heart_picks", "heart_pick_rewards", "sns_lucky_draw_entries", "sns_lucky_draw_results", "sns_lucky_draw_prizes", "chemistry_signals", "chemistry_rewards", "public_announcements", "truth_game_sessions", "truth_game_questions", "truth_game_resets"]:
            try:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
                counts.append(f"{table}: {cur.fetchone()['cnt']}")
            except Exception:
                counts.append(f"{table}: 확인 실패")
        conn.close()
        reply(event.reply_token, "🗄️ DB 상태\n\n" + "\n".join(counts))
        return

    if text == "/설렘픽초기화":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply(event.reply_token, reset_today_heart_picks(date_str))
        return

    if text == "/설렘픽정산":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(heart_pick_settlement_text()))
        return

    if text == "/운영진친밀도" or text.startswith("/운영진친밀도 ") or text == "/운영진친밀도확인" or text.startswith("/운영진친밀도확인 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        if text.startswith("/운영진친밀도확인"):
            keyword = text.replace("/운영진친밀도확인", "", 1).strip()
        else:
            keyword = text.replace("/운영진친밀도", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(operator_affinity_report_text(keyword)))
        return

    if text == "/수집상태":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        log_row, count_row, all_rows = collection_status(COUNT_SOURCE_ID, date_str)
        reply(
            event.reply_token,
            "📊 수집상태\n\n"
            f"기준일: {date_str}\n"
            f"기준방: {COUNT_SOURCE_ID}\n\n"
            f"채팅 로그: {log_row['total_logs'] if log_row else 0}건\n"
            f"활동 유저: {log_row['active_users'] if log_row else 0}명\n"
            f"집계 유저: {count_row['counted_users'] if count_row else 0}명\n"
            f"전체 마디: {count_row['total_madi'] if count_row else 0}"
        )
        return

    if text == "/최근로그":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        rows = recent_chat_logs(COUNT_SOURCE_ID, limit=20)
        if not rows:
            reply(event.reply_token, "최근 로그가 없습니다.")
            return
        lines = ["🧾 최근 로그", ""]
        for row in rows:
            lines.append(f"{row['created_at']} / {row['user_name']} / {row['text'] or '-'}")
        reply_many(event.reply_token, split_text_messages("\n".join(lines)))
        return

    if text == "/수집누락":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        users_no_count, logs_no_count, counts_no_user = collection_missing(COUNT_SOURCE_ID, date_str)
        lines = ["🧩 수집누락", f"기준일: {date_str}", ""]
        lines.append(f"users 등록 / 오늘 counts 없음: {len(users_no_count)}명")
        for row in users_no_count[:20]:
            lines.append(f"- {row['user_name']}")
        lines.append("")
        lines.append(f"chat_logs 있음 / counts 없음: {len(logs_no_count)}명")
        for row in logs_no_count[:20]:
            lines.append(f"- {row['user_name']} / 로그 {row['logs']}건")
        lines.append("")
        lines.append(f"counts 있음 / users 없음: {len(counts_no_user)}명")
        for row in counts_no_user[:20]:
            lines.append(f"- {row['user_name']} / {row['count']}마디")
        reply_many(event.reply_token, split_text_messages("\n".join(lines)))
        return

    if text == "/경고":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(warning_text_for_staff(date_str, COUNT_SOURCE_ID)))
        return

    if text.startswith("/마디수 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        target_date, err = parse_date_arg(text.replace("/마디수", "", 1).strip())
        if err:
            reply(event.reply_token, err)
            return
        reply_many(event.reply_token, split_text_messages(madi_history_text(target_date, COUNT_SOURCE_ID)))
        return

    if text == "/경고누적일" or text.startswith("/경고누적일 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        raw_days = text.replace("/경고누적일", "", 1).strip()
        try:
            min_days = int(raw_days) if raw_days else 1
        except Exception:
            reply(event.reply_token, "사용법: /경고누적일 또는 /경고누적일 최소횟수")
            return
        reply_many(event.reply_token, split_text_messages(warning_accumulated_days_text(COUNT_SOURCE_ID, min_days)))
        return

    if text == "/단벙참여확인" or text.startswith("/단벙참여확인 ") or text == "/단벙참석확인" or text.startswith("/단벙참석확인 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        if text.startswith("/단벙참석확인"):
            raw_date = text.replace("/단벙참석확인", "", 1).strip()
        else:
            raw_date = text.replace("/단벙참여확인", "", 1).strip()
        target_date, err = parse_date_arg(raw_date)
        if err:
            reply_many(event.reply_token, split_text_messages(danbung_attendance_event_text(raw_date)))
            return
        reply_many(event.reply_token, split_text_messages(danbung_attendance_status_text(target_date)))
        return

    if text == "/전체유저":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(all_registered_users_text()))
        return

    if text.startswith("/유저검색 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        keyword = text.replace("/유저검색", "", 1).strip()
        rows = user_debug(keyword)
        if not rows:
            reply(event.reply_token, "검색 결과가 없습니다.")
            return
        lines = ["🔍 유저검색", ""]
        for row in rows:
            status = "활성" if int(row["is_active"] or 0) == 1 else "비활성"
            lines.append(
                f"{row['user_name']} / {status}\n"
                f"코인: {coin_text(row['balance'])}\n"
                f"총마디: {row['total_count']} / 활동일: {row['active_days']}\n"
                f"최근로그: {row['last_log'] or '-'}"
            )
            lines.append("")
        reply_many(event.reply_token, split_text_messages("\n".join(lines)))
        return

    if text.startswith("/유저상세 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        keyword = text.replace("/유저상세", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(admin_user_detail_text(keyword)))
        return

    if text == "/닉삭제" or text.startswith("/닉삭제 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        keyword = text.replace("/닉삭제", "", 1).strip()
        if not keyword:
            reply(event.reply_token, "사용법: /닉삭제 닉네임")
            return
        rows = find_users(keyword, limit=10)
        if not rows:
            reply(event.reply_token, "대상 유저를 찾지 못했어요. 닉네임을 조금만 더 정확히 입력해 주세요.")
            return
        DELETE_PENDING[user_id] = {"mode": "soft_delete", "candidates": rows}
        if len(rows) > 1:
            lines = ["검색 결과가 여러 명입니다.", ""]
            for i, row in enumerate(rows, 1):
                lines.append(f"{i}. {row['user_name']}")
            lines += ["", "삭제할 번호를 /닉삭제번호 번호 로 입력해 주세요."]
            reply(event.reply_token, "\n".join(lines))
            return
        changed, name = set_user_active_by_id_with_name(rows[0]["user_id"], 0)
        DELETE_PENDING[user_id] = {"mode": "deleted_selected", "target": rows[0]}
        reply(event.reply_token, f"✅ 닉삭제 완료\n\n대상: {name}\n\n완전삭제가 필요하면 /완전삭제 를 입력해 주세요.")
        return

    if text.startswith("/닉삭제번호"):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        pending = DELETE_PENDING.get(user_id)
        if not pending or "candidates" not in pending:
            reply(event.reply_token, "진행 중인 닉삭제 후보가 없습니다.")
            return
        try:
            idx = int(text.split()[1]) - 1
            target = pending["candidates"][idx]
        except Exception:
            reply(event.reply_token, "번호를 한 번 확인해 주세요.")
            return
        changed, name = set_user_active_by_id_with_name(target["user_id"], 0)
        DELETE_PENDING[user_id] = {"mode": "deleted_selected", "target": target}
        reply(event.reply_token, f"✅ 닉삭제 완료\n\n대상: {name}\n\n완전삭제가 필요하면 /완전삭제 를 입력해 주세요.")
        return

    if text == "/완전삭제":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        pending = DELETE_PENDING.get(user_id)
        if not pending or pending.get("mode") != "deleted_selected" or not pending.get("target"):
            reply(event.reply_token, "⛔ 먼저 /닉삭제 또는 /닉삭제번호 로 대상을 특정해주세요.")
            return
        target = pending["target"]
        move_user_to_deleted(target["user_id"], target["user_name"], user_name)
        DELETE_PENDING.pop(user_id, None)
        reply(event.reply_token, f"🗑 완전삭제 완료\n\n대상: {target['user_name']}\n\n삭제유저 DB로 이동했습니다.\n조회: /삭제유저\n복구: /삭제복구 번호")
        return

    if text == "/삭제유저":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(deleted_users_text()))
        return

    if text.startswith("/삭제복구"):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            reply(event.reply_token, "사용법: /삭제복구 번호")
            return
        ok, msg = restore_deleted_user_by_index(parts[1].strip())
        reply_many(event.reply_token, split_text_messages(msg))
        return

    if text == "/경제현황":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply(event.reply_token, economy_status_text())
        return

    if text == "/회생초기화" or text.startswith("/회생초기화 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        target = text.replace("/회생초기화", "", 1).strip()
        ok, msg = reset_revival_claims(target)
        reply(event.reply_token, msg)
        return

    if text == "/코인검증" or text.startswith("/코인검증 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        keyword = text.replace("/코인검증", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(coin_audit_text(keyword)))
        return

    if text == "/정산검증":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(settlement_audit_text()))
        return

    if text == "/최근오류" or text.startswith("/최근오류 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        raw_limit = text.replace("/최근오류", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(recent_errors_text(raw_limit or 10)))
        return

    if text == "/유저아이템보유":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(user_item_holdings_text()))
        return

    if text == "/유저아이템삭제" or text.startswith("/유저아이템삭제 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        parts = text.split()
        if len(parts) < 4:
            reply(event.reply_token, "사용법: /유저아이템삭제 닉네임 아이템명 개수")
            return
        user_keyword = parts[1]
        amount_text = parts[-1]
        item_keyword = " ".join(parts[2:-1]).strip()
        if not item_keyword:
            reply(event.reply_token, "사용법: /유저아이템삭제 닉네임 아이템명 개수")
            return
        ok, msg = remove_user_items_by_name(user_keyword, item_keyword, amount_text, user_name)
        reply_many(event.reply_token, split_text_messages(msg))
        return

    if text == "/조각정리":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        converted = migrate_old_pieces_to_iron()
        reply(event.reply_token, f"🧩 조각 정리 완료\n\n기존 기타 조각 {converted}개를 철 조각으로 변환했습니다.")
        return

    if text.startswith("/지급 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        parts = text.split()
        if len(parts) < 3:
            reply(event.reply_token, "사용법: /지급 닉네임 금액")
            return
        target, err = resolve_active_user_by_nickname(parts[1], purpose="대상")
        if err:
            reply_many(event.reply_token, split_text_messages(err))
            return
        try:
            amount = coin_to_points(parts[2])
        except Exception as e:
            reply(event.reply_token, str(e))
            return
        balance = change_money(target["user_id"], target["user_name"], amount, "운영진 지급", user_id, user_name)
        reply(event.reply_token, f"✅ 지급 완료\n\n대상: {target['user_name']}\n금액: {coin_text(amount)}\n잔액: {coin_text(balance)}")
        return

    if text.startswith("/차감 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        parts = text.split()
        if len(parts) < 3:
            reply(event.reply_token, "사용법: /차감 닉네임 금액")
            return
        target, err = resolve_active_user_by_nickname(parts[1], purpose="대상")
        if err:
            reply_many(event.reply_token, split_text_messages(err))
            return
        try:
            amount = coin_to_points(parts[2])
        except Exception as e:
            reply(event.reply_token, str(e))
            return
        balance = change_money(target["user_id"], target["user_name"], -amount, "운영진 차감", user_id, user_name)
        reply(event.reply_token, f"✅ 차감 완료\n\n대상: {target['user_name']}\n금액: -{coin_text(amount)}\n잔액: {coin_text(balance)}")
        return

    if text == "/코인내역":
        rows = currency_history(user_id, limit=10)
        lines = [f"💰 내 코인내역: {user_name}", ""]
        if not rows:
            lines.append("내역이 없습니다.")
        else:
            for row in rows:
                sign = "+" if int(row["amount"]) > 0 else ""
                lines.append(f"{row['created_at']} / {sign}{coin_text(row['amount'])} / {row['reason'] or '-'}")
        lines.append("")
        lines.append(f"현재 보유: {coin_text(get_balance(user_id))}")
        push_or_reply_private_info(event, user_id, "\n".join(lines), "📩 코인내역을 개인 메시지로 보내드렸습니다.", "/코인내역")
        return

    if text.startswith("/코인내역 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        parts = text.split(maxsplit=1)
        keyword = parts[1].strip()
        target, err = resolve_active_user_by_nickname(keyword, purpose="대상")
        if err:
            reply_many(event.reply_token, split_text_messages(err))
            return
        rows = currency_history(target["user_id"], limit=10)
        lines = [f"💰 코인내역: {target['user_name']}", ""]
        if not rows:
            lines.append("내역이 없습니다.")
        else:
            for row in rows:
                sign = "+" if int(row["amount"]) > 0 else ""
                lines.append(f"{row['created_at']} / {sign}{coin_text(row['amount'])} / {row['reason'] or '-'}")
        lines.append("")
        lines.append(f"현재 보유: {coin_text(get_balance(target['user_id']))}")
        reply_many(event.reply_token, split_text_messages("\n".join(lines)))
        return

    if text.startswith("/상품추가 ") or text.startswith("/상품등록 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        raw = text.split(maxsplit=3)
        if len(raw) < 4:
            reply(event.reply_token, "사용법: /상품추가 상품명 가격 설명")
            return
        _, item_name, price_text, desc = raw
        try:
            price = coin_to_points(price_text)
        except Exception as e:
            reply(event.reply_token, str(e))
            return
        add_shop_item(item_name, price, desc)
        reply(event.reply_token, f"✅ 상품 추가 완료\n\n{item_name} / {coin_text(price)}")
        return

    if text.startswith("/상품삭제 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        item_name = text.replace("/상품삭제", "", 1).strip()
        changed = remove_shop_item(item_name)
        reply(event.reply_token, "✅ 상품 삭제 완료" if changed else "상품을 찾을 수 없습니다.")
        return

    if text.startswith("/사용 "):
        if not is_private_chat(event):
            reply(event.reply_token, "아이템 사용은 꽃봇 1:1 채팅에서만 가능합니다.\n\n사용법: /사용 구매번호")
            return
        try:
            purchase_id = int(text.split()[1])
        except Exception:
            reply(event.reply_token, "사용법: /사용 구매번호")
            return
        ok, msg = use_purchase(purchase_id, user_id, user_name)
        reply_many(event.reply_token, split_text_messages(msg))
        return

    if text.startswith("/사용처리 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        try:
            purchase_id = int(text.split()[1])
        except Exception:
            reply(event.reply_token, "사용법: /사용처리 구매번호")
            return
        ok, msg = staff_use_purchase(purchase_id, user_name)
        reply_many(event.reply_token, split_text_messages(msg))
        return

    if text.startswith("/구매취소 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        try:
            purchase_id = int(text.split()[1])
        except Exception:
            reply(event.reply_token, "사용법: /구매취소 구매번호")
            return
        ok, msg = cancel_purchase(purchase_id, user_name)
        reply_many(event.reply_token, split_text_messages(msg))
        return

    if text.startswith("/아이템지급 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            reply(event.reply_token, "사용법: /아이템지급 닉네임 상품명")
            return
        target, err = resolve_active_user_by_nickname(parts[1], purpose="대상")
        if err:
            reply_many(event.reply_token, split_text_messages(err))
            return
        purchase_id = add_reward_purchase(target["user_id"], target["user_name"], parts[2])
        reply(event.reply_token, f"🎁 아이템 지급 완료\n\n대상: {target['user_name']}\n상품: {parts[2]}\n구매번호: {purchase_id}")
        return

    if text == "/족보입력":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        JOKBO_PENDING[user_id] = True
        reply(event.reply_token, "족보 내용을 다음 메시지로 보내주세요.\n기존 코인은 무시하고 족보 내용으로 갱신됩니다.")
        return

    if text == "/족보":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(genealogy_text_with_coins()))
        return

    if text == "/럭키정산":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        ok, msg = settle_lucky_draw(user_name)
        reply_many(event.reply_token, split_text_messages(msg))
        return

    if text == "/럭키초기화":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        week_start, week_end = event_week_key()
        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM sns_lucky_draw_entries WHERE week_start = ?", (week_start,))
        entries = cur.rowcount
        cur.execute("DELETE FROM sns_lucky_draw_results WHERE week_start = ?", (week_start,))
        results = cur.rowcount
        cur.execute("DELETE FROM sns_lucky_draw_prizes WHERE week_start = ?", (week_start,))
        prizes = cur.rowcount
        conn.commit()
        conn.close()
        reply(event.reply_token, f"🧹 럭키드로우 초기화 완료\n\n참여 {entries}건 / 결과 {results}건 / 순위 {prizes}건 삭제")
        return

    if text == "/럭키현황전체":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(lucky_draw_status_text()))
        return

    if text == "/진실질문추가" or text.startswith("/진실질문추가 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        raw_args = text.replace("/진실질문추가", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(add_truth_game_question(raw_args, user_id, user_name)))
        return

    if text == "/진실기록" or text.startswith("/진실기록 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        keyword = text.replace("/진실기록", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(truth_game_user_history_text(keyword)))
        return

    if text == "/진실목록":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        reply_many(event.reply_token, split_text_messages(truth_game_list_text(limit=10)))
        return

    # =========================
    # 유저 명령어
    # =========================
    if text == "/가이드":
        if is_private_chat(event):
            reply_many(event.reply_token, split_text_messages(beginner_guide_text()))
        else:
            reply(event.reply_token, one_to_one_command_notice("가이드", "/가이드"))
        return

    if text == "/명령어":
        if is_private_chat(event):
            reply_many(event.reply_token, split_text_messages(user_commands_text()))
        else:
            reply(event.reply_token, one_to_one_command_notice("명령어", "/명령어"))
        return

    if text == "/주사위":
        dice_value = random.randint(0, 99)
        reply(
            event.reply_token,
            "🎲 주사위 결과\n\n"
            f"{display_nickname(user_name)}님: {dice_value}"
        )
        return

    if text == "/마디수":
        rows = ranking(date_str, COUNT_SOURCE_ID, limit=30)
        my_count = 0
        for row in rows:
            if row["user_id"] == user_id:
                my_count = int(row["count"] or 0)
                break
        lines = ["📊 오늘의 마디수", f"기준일: {date_str}", "", f"내 마디수: {my_count}", ""]
        for i, row in enumerate(rows[:10], 1):
            lines.append(f"{i}. {row['user_name']} - {row['count']}마디")
        reply_many(event.reply_token, split_text_messages("\n".join(lines)))
        return

    if text == "/전체순위":
        rows = total_ranking(COUNT_SOURCE_ID, limit=20)
        if not rows:
            reply(event.reply_token, "전체순위 데이터가 없습니다.")
            return
        lines = ["🏆 전체 마디수 순위", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']} - {row['count']}마디")
        reply_many(event.reply_token, split_text_messages("\n".join(lines)))
        return

    if text == "/친밀도랭킹":
        push_or_reply_private_info(event, user_id, affinity_ranking_text(limit=10), "📩 친밀도 랭킹을 개인 메시지로 보내드렸습니다.", "/친밀도랭킹")
        return

    if text == "/마니또보상":
        reply(
            event.reply_token,
            "🎭 마니또 보상 안내\n\n"
            "일반 마니또: 1.5 ~ 6코인\n"
            "황금 마니또: 6 ~ 15코인\n\n"
            "친밀도 낮음: 보상 보너스 최대 +50%\n"
            "친밀도 높음: 목표 횟수 최대 30회"
        )
        return

    if text in ["/마니또", "/마니또확인"]:
        send_manitto_reply(event, user_id, user_name)
        return

    if text == "/마니또변경":
        if is_private_chat(event):
            reply_many(event.reply_token, split_text_messages(reroll_manitto(user_id, user_name)))
        else:
            reply(event.reply_token, "🎭 마니또 변경은 꽃봇 1:1 채팅에서만 가능합니다.")
        return

    if text == "/출석":
        ok, balance = attendance_check(date_str, user_id, user_name)
        if ok:
            try:
                streak, streak_paid = check_attendance_streak_reward(date_str, user_id, user_name)
            except Exception:
                streak, streak_paid = 1, []
            extra = ""
            if streak_paid:
                paid_lines = [f"{days}일차 출석일수 보상 {coin_text(reward)}" for days, reward in streak_paid]
                extra = "\n\n🎁 출석일수 보상\n" + "\n".join(paid_lines)
                balance = get_balance(user_id)
            reply(event.reply_token, f"✅ 출석 완료\n\n{user_name}님\n보상: {coin_text(ATTENDANCE_REWARD)}\n현재 보유: {coin_text(balance)}{extra}\n\n{streak}일차 출석완료")
        else:
            try:
                streak = attendance_streak_days(user_id, date_str)
            except Exception:
                streak = 0
            streak_text = f"\n\n{streak}일차 출석완료" if streak > 0 else ""
            reply(event.reply_token, f"이미 오늘 출석했습니다.\n\n현재 보유: {coin_text(balance)}{streak_text}")
        return

    if text == "/단벙":
        reply_many(event.reply_token, split_text_messages(danbung_info_text()))
        return

    if text == "/단벙참여" or text.startswith("/단벙참여 ") or text == "/단벙참석" or text.startswith("/단벙참석 "):
        if text.startswith("/단벙참석"):
            event_name = text.replace("/단벙참석", "", 1).strip()
        else:
            event_name = text.replace("/단벙참여", "", 1).strip()
        ok, msg = charge_danbung_attendance(user_id, user_name, event_name)
        reply(event.reply_token, msg)
        return

    if text == "/미션":
        count, missions = mission_status(date_str, COUNT_SOURCE_ID, user_id)
        lines = ["🎯 오늘의 미션", "", f"현재 마디수: {count}", ""]
        for mission in missions:
            status = "✅ 수령완료" if mission["received"] else ("🎁 수령가능" if mission["done"] else "❌ 진행중")
            lines.append(f"{status} {mission['required']}마디 → {coin_text(mission['reward'])}")
        lines += ["", "보상 수령", "/수령"]
        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/수령":
        total_reward, count, claimed_names = claim_missions(date_str, COUNT_SOURCE_ID, user_id, user_name)
        if total_reward <= 0:
            reply(event.reply_token, f"수령 가능한 미션 보상이 없습니다.\n\n현재 마디수: {count}\n확인: /미션")
        else:
            reply(event.reply_token, f"🎉 미션 보상 수령 완료\n\n달성 미션: {', '.join(claimed_names)}\n지급: {coin_text(total_reward)}\n현재 보유: {coin_text(get_balance(user_id))}")
        return

    if text == "/회생":
        ok, msg = revival_claim(date_str, user_id, user_name)
        reply(event.reply_token, msg)
        return

    if text == "/잔액":
        reply(event.reply_token, f"💰 {user_name}님의 보유 코인\n\n{coin_text(get_balance(user_id))}")
        return

    if text == "/내정보":
        push_or_reply_private_info(
            event,
            user_id,
            user_summary_text(user_id, user_name),
            "📩 내정보를 개인 메시지로 보내드렸습니다.",
            "/내정보"
        )
        return

    if text == "/내보유":
        msg = (
            f"💰 {user_name}님의 보유 코인\n\n"
            f"{coin_text(get_balance(user_id))}\n\n"
            f"{user_purchases_text(user_id, 'all')}"
        )
        push_or_reply_private_info(
            event,
            user_id,
            msg,
            "📩 보유 정보를 개인 메시지로 보내드렸습니다.",
            "/내보유",
            allow_admin_room=True
        )
        return

    if text == "/내보유 미사용":
        push_or_reply_private_info(
            event,
            user_id,
            user_purchases_text(user_id, "owned"),
            "📩 미사용 아이템 목록을 개인 메시지로 보내드렸습니다.",
            "/내보유 미사용",
            allow_admin_room=True
        )
        return

    if text == "/내보유 사용":
        push_or_reply_private_info(
            event,
            user_id,
            user_purchases_text(user_id, "used"),
            "📩 사용완료 아이템 목록을 개인 메시지로 보내드렸습니다.",
            "/내보유 사용",
            allow_admin_room=True
        )
        return

    if text == "/코인랭킹":
        rows = currency_ranking(limit=10)
        if not rows:
            reply(event.reply_token, "💰 코인 순위가 없습니다.")
            return
        lines = ["💰 코인 순위", ""]
        for i, row in enumerate(rows, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            lines.append(f"{medal} {row['user_name']} - {coin_text(row['balance'])}")
        reply(event.reply_token, "\n".join(lines))
        return

    if text == "/업적":
        push_or_reply_private_info(event, user_id, achievement_status_text(user_id, user_name), "📩 업적 현황을 개인 메시지로 보내드렸습니다.", "/업적")
        return

    if text in ["/인기인", "/오늘인기인"]:
        rank_source_id = source_id if source_id in count_source_ids() else COUNT_SOURCE_ID
        reply_many(event.reply_token, split_text_messages(popular_mentions_text(date_str, rank_source_id, "daily")))
        return

    if text == "/주간인기인":
        rank_source_id = source_id if source_id in count_source_ids() else COUNT_SOURCE_ID
        reply_many(event.reply_token, split_text_messages(popular_mentions_text(date_str, rank_source_id, "weekly")))
        return

    if text.startswith("/언급랭킹"):
        keyword = text.replace("/언급랭킹", "", 1).strip()
        if not keyword:
            reply(event.reply_token, "사용법: /언급랭킹 닉네임")
            return
        rank_source_id = source_id if source_id in count_source_ids() else COUNT_SOURCE_ID
        reply_many(event.reply_token, split_text_messages(mention_ranking_text(keyword, date_str, rank_source_id)))
        return

    if text == "/설렘픽" or text.startswith("/설렘픽 "):
        if not is_private_chat(event):
            reply(event.reply_token, one_to_one_command_notice("설렘픽", "/설렘 닉네임"))
            return
        keyword = text.replace("/설렘픽", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(heart_pick(user_id, user_name, keyword, announce_public=True)))
        return

    if text == "/설렘픽현황":
        push_or_reply_private_info(event, user_id, heart_pick_status_text(user_id, user_name), "📩 설렘픽 현황을 개인 메시지로 보내드렸습니다.", "/설렘 현황")
        return

    if text == "/설렘픽랭킹":
        reply_many(event.reply_token, split_text_messages(heart_pick_ranking_text()))
        return

    if text == "/케미" or text.startswith("/케미 "):
        if not is_private_chat(event):
            reply(event.reply_token, one_to_one_command_notice("케미", "/케미 닉네임"))
            return
        keyword = text.replace("/케미", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(chemistry_signal(user_id, user_name, keyword, announce_public=True)))
        return

    if text == "/케미확인":
        if not is_private_chat(event):
            reply(event.reply_token, one_to_one_command_notice("케미확인", "/케미 확인"))
            return
        reply_many(event.reply_token, split_text_messages(personal_chemistry_check_text(user_id)))
        return

    if text == "/쌍방케미확인":
        if not is_private_chat(event) or not is_admin(user_id):
            reply(event.reply_token, "이 명령어는 지금 사용할 수 없어요.")
            return
        reply_many(event.reply_token, split_text_messages(mutual_chemistry_report_text()))
        return

    if text == "/진실게임초기화":
        reply_many(event.reply_token, split_text_messages(truth_game_reset_user_questions(user_id, user_name)))
        return

    if text == "/진실게임" or text.startswith("/진실게임 ") or text == "/진실질문" or text.startswith("/진실질문 "):
        if text.startswith("/진실게임"):
            raw_truth_args = text.replace("/진실게임", "", 1).strip()
        else:
            raw_truth_args = text.replace("/진실질문", "", 1).strip()
        target_keyword, truth_difficulty, truth_err = parse_truth_game_args(raw_truth_args)
        if truth_err:
            reply_many(event.reply_token, split_text_messages(truth_err))
            return
        reply_many(event.reply_token, split_text_messages(truth_game_start(user_id, user_name, target_keyword, truth_difficulty)))
        return

    if text.startswith("/진실답변"):
        answer_text = text.replace("/진실답변", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(truth_game_answer(user_id, user_name, answer_text)))
        return

    if text == "/진실패스":
        reply_many(event.reply_token, split_text_messages(truth_game_pass(user_id, user_name)))
        return

    if text == "/진실취소" or text.startswith("/진실취소 "):
        target_keyword = text.replace("/진실취소", "", 1).strip()
        reply_many(event.reply_token, split_text_messages(truth_game_cancel(user_id, user_name, target_keyword)))
        return

    if text == "/주간랭킹":
        week_start, week_end = week_range_for_today()
        rows = weekly_ranking_rows(COUNT_SOURCE_ID, week_start, week_end, limit=10)
        if not rows:
            reply(event.reply_token, f"🏆 이번 주 랭킹이 없습니다.\n기간: {week_start} ~ {week_end}")
            return
        lines = ["🏆 이번 주 마디수 랭킹", f"기간: {week_start} ~ {week_end}", ""]
        for i, row in enumerate(rows, 1):
            reward = weekly_reward_amount(i)
            reward_text = f" / 보상 {coin_text(reward)}" if reward > 0 else ""
            lines.append(f"{i}. {row['user_name']} - {row['total_count']}마디{reward_text}")
        reply(event.reply_token, "\n".join(lines))
        return

    # =========================
    # 운영방/운영진 전용 묶음 명령어
    # =========================
    gacha_commands = {
        "/가챠", "/가챠시스템", "/가챠횟수",
        "/상가챠", "/중가챠", "/하가챠",
        "/조각가챠", "/조각", "/대장장이",
        "/김미트상가챠",
    }
    shop_lucky_commands = {
        "/상점",
        "/럭키드로우", "/럭키드로우구매", "/럭키드로우현황", "/럭키드로우결과",
    }

    if text in gacha_commands or text in shop_lucky_commands or text.startswith("/구매 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return

        if source_id not in ADMIN_SOURCE_IDS:
            reply(event.reply_token, "⛔ 운영방에서만 사용 가능합니다.")
            return

        if text in gacha_commands:
            if text == "/가챠":
                reply_many(event.reply_token, split_text_messages(gacha_system_text()))
                return

            if text in ["/상가챠", "/중가챠", "/하가챠"]:
                tier = text.replace("/", "", 1).replace("가챠", "", 1)
                success, message = run_gacha(user_id, user_name, tier)
                if success:
                    grant_achievement_once(user_id, user_name, "first_gacha", "🎰 첫 가챠", 2, tier)
                reply_many(event.reply_token, split_text_messages(message))
                return

            if text == "/김미트상가챠":
                if not is_admin(user_id):
                    reply(event.reply_token, "⛔ 방장 전용 명령어입니다.")
                    return
                success, message = run_kimmeat_sang_gacha(user_id, user_name)
                if success:
                    grant_achievement_once(user_id, user_name, "first_gacha", "🎰 첫 가챠", 2, "kimmeat_sang")
                reply_many(event.reply_token, split_text_messages(message))
                return

            if text == "/조각가챠":
                success, message = run_piece_gacha(user_id, user_name)
                if success:
                    grant_achievement_once(user_id, user_name, "first_gacha", "🎰 첫 가챠", 2, "piece")
                reply_many(event.reply_token, split_text_messages(message))
                return

            if text == "/조각":
                reply_many(event.reply_token, split_text_messages(gacha_piece_text(user_id)))
                return

            if text == "/대장장이":
                reply_many(event.reply_token, split_text_messages(blacksmith_exchange(user_id, user_name)))
                return

            if text == "/가챠시스템":
                reply_many(event.reply_token, split_text_messages(gacha_system_text()))
                return

            if text == "/가챠횟수":
                reply(event.reply_token, weekly_gacha_count_text(user_id))
                return

        if text == "/상점":
            reply_many(event.reply_token, split_text_messages(shop_text()))
            return

        if text.startswith("/구매 "):
            item_name = text.replace("/구매", "", 1).strip()
            ok, msg = buy_item(user_id, user_name, item_name)
            reply_many(event.reply_token, split_text_messages(msg))
            return

        if text in ["/럭키드로우", "/럭키드로우현황"]:
            reply_many(event.reply_token, split_text_messages(lucky_draw_status_text()))
            return

        if text == "/럭키드로우결과":
            reply_many(event.reply_token, split_text_messages(lucky_draw_result_text()))
            return

        if text == "/럭키드로우구매":
            ok, msg = buy_lucky_draw_ticket(user_id, user_name)
            reply_many(event.reply_token, split_text_messages(msg))
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
            log_error("MEMBER_LEFT_ERROR", e)


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
            log_error("MEMBER_JOINED_ERROR", e)


# 럭키드로우 자동 정산 스케줄러 시작
start_lucky_draw_auto_scheduler()



# 자동 주간정산 스케줄러 시작
try:
    if os.getenv("DISABLE_AUTO_WEEKLY_SETTLEMENT", "0") != "1":
        start_weekly_settlement_scheduler()
except Exception as e:
    log_error("START_WEEKLY_SETTLEMENT_SCHEDULER_ERROR", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
