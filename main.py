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
    PushMessageRequest,
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
BOT_VERSION = "sns-flowerbot-v10.4"
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


def is_admin_room(source_id):
    return bool(source_id and source_id in ADMIN_SOURCE_IDS)


def is_operator_command(text):
    """
    운영진 전용 명령어를 일반 유저가 입력했을 때
    기능별 다른 문구 대신 동일한 경고 문구를 출력하기 위한 통합 체크.
    """
    if not text:
        return False

    exact_commands = {
        "/운영명령어", "/방정보", "/DB상태", "/전체유저",
        "/족보입력", "/족보", "/경고", "/완전삭제",
        "/삭제유저", "/경제현황", "/럭키정산", "/럭키초기화", "/럭키현황전체",
        "/조각정리", "/버전",
    }

    prefix_commands = [
        "/유저검색 ", "/유저상세 ", "/닉삭제", "/닉삭제번호",
        "/지급 ", "/차감 ", "/코인내역 ", "/삭제복구",
        "/상품추가 ", "/상품등록 ", "/상품삭제 ",
        "/사용 ", "/사용처리 ", "/구매취소 ", "/아이템지급 ",
        "/DM테스트 ",
    ]

    return text in exact_commands or any(text.startswith(prefix) for prefix in prefix_commands)


def operator_only_warning():
    return "⛔ 운영진 전용 명령어입니다."


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

    if not paid:
        return (
            "🏆 주간정산\n\n"
            f"기간: {week_start} ~ {week_end}\n"
            "새로 지급할 주간 보상이 없습니다.\n"
            "이미 정산했거나 랭킹 데이터가 없습니다."
        )

    lines = ["🏆 주간정산 완료", f"기간: {week_start} ~ {week_end}", ""]
    for item in paid:
        lines.append(f"{item['rank']}위 {item['user_name']} - {item['count']}마디 / {coin_text(item['reward'])}")
    return "\n".join(lines)


def weekly_settlement(source_id=None):
    return weekly_settlement_text(source_id)


def weekly_reward_settlement(source_id=None):
    return weekly_settlement_text(source_id)


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


def push_private_message(user_id, text_value):
    """
    USER_ID(U...)로만 개인 DM PushMessage를 보냅니다.
    성공 True / 실패 False
    """
    user_id = str(user_id or "").strip()
    if not user_id.startswith("U"):
        print("[DM_FAIL] INVALID_USER_ID", user_id)
        return False

    try:
        messages = [TextMessage(text=str(t)[:4900]) for t in split_text_messages(text_value)]
        if not messages:
            messages = [TextMessage(text="내용이 없습니다.")]

        with ApiClient(config) as client:
            api = MessagingApi(client)
            api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=messages[:5]
                )
            )
        print("[DM_OK]", user_id)
        return True
    except Exception as e:
        print("[DM_FAIL]", user_id, repr(e))
        return False


def push_or_reply_private_info(event, user_id, text_value, public_notice="📩 개인 메시지로 전송했습니다."):
    """
    1:1 채팅에서는 현재 대화에 바로 reply.
    공개방/그룹/룸에서는 유저에게 1:1 Push 후 공개방에는 안내만 출력.
    """
    if is_private_chat(event):
        reply_many(event.reply_token, split_text_messages(text_value))
        return

    if not user_id:
        reply(event.reply_token, "개인 메시지를 보내려면 USER_ID가 필요합니다.\n방에서 채팅 1회 후 다시 입력해주세요.")
        return

    ok = push_private_message(user_id, text_value)
    if ok:
        reply(event.reply_token, public_notice)
    else:
        reply(
            event.reply_token,
            "📩 개인 메시지 전송에 실패했습니다.\n\n"
            "꽃봇을 친구추가한 뒤 다시 입력해주세요.\n\n"
            "※ 이미 친구추가가 되어 있다면 운영진에게 알려주세요."
        )


def private_only_notice(*args):
    """
    공개방에서 1:1 전용 기능을 눌렀을 때 안내.
    호환:
      private_only_notice("가챠")
      private_only_notice(event, user_id, text_value, "가챠")
    """
    if len(args) >= 4:
        event, user_id, text_value, feature_name = args[0], args[1], args[2], args[3]
        ok = push_private_message(user_id, text_value) if user_id else False
        if ok:
            reply(event.reply_token, f"📩 {feature_name} 안내를 개인 메시지로 보내드렸습니다.")
        else:
            reply(
                event.reply_token,
                f"📩 {feature_name} 안내 전송에 실패했습니다.\n\n"
                "꽃봇을 친구추가한 뒤 다시 입력해주세요."
            )
        return None

    feature_name = args[0] if args else "해당 기능"
    return (
        f"📩 {feature_name}은 꽃봇 1:1 채팅에서 이용해주세요.\n\n"
        "공개방에는 개인정보 보호를 위해 자세한 내용을 표시하지 않습니다."
    )


def gacha_private_guide_text():
    return (
        "🎰 가챠 안내\n\n"
        "가챠는 꽃봇 1:1 채팅에서 이용해주세요.\n\n"
        "운영시간: 매주 토요일 00:00 ~ 21:00\n"
        "주간 제한: 상/중/하/조각 합산 15회\n\n"
        "명령어\n"
        "/상가챠\n"
        "/중가챠\n"
        "/하가챠\n"
        "/조각가챠\n"
        "/조각\n"
        "/대장장이\n"
        "/가챠횟수\n"
        "/가챠시스템"
    )


def shop_private_guide_text():
    return (
        "🛒 상점 안내\n\n"
        "상점과 구매/사용 기능은 꽃봇 1:1 채팅에서 이용해주세요.\n\n"
        "명령어\n"
        "/상점\n"
        "/구매 상품명\n"
        "/내보유\n"
        "/내보유 미사용\n"
        "/내보유 사용\n"
        "/사용 구매번호"
    )


def safe_call(label, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"{label}_ERROR:", repr(e))
        return None

def user_guide_text():
    return beginner_guide_text()


def user_commands_text():
    return """🤖 S.N.S 꽃봇 명령어

━━━━━━━━━━
📖 정보
━━━━━━━━━━
/명령어
/가이드

━━━━━━━━━━
🎯 활동
━━━━━━━━━━
/출석
/미션
/수령
/마디수
/전체순위
/주간랭킹

━━━━━━━━━━
💰 재화
━━━━━━━━━━
/내보유
/잔액
/코인랭킹
/코인내역

━━━━━━━━━━
🛒 상점
━━━━━━━━━━
/상점
/구매 상품명

━━━━━━━━━━
🎰 가챠
━━━━━━━━━━
/가챠
/가챠시스템
/가챠횟수
/상가챠
/중가챠
/하가챠
/조각가챠
/조각
/대장장이

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
/친밀도
/친밀도 닉네임
/친밀도랭킹

━━━━━━━━━━
🏆 업적
━━━━━━━━━━
/업적

━━━━━━━━━━
🎟 럭키드로우
━━━━━━━━━━
/럭키드로우
/럭키드로우구매
/럭키드로우현황
/럭키드로우결과"""

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

7️⃣ /내보유 로 보유 코인과 아이템을 확인할 수 있습니다.

8️⃣ /상점 에서 다양한 아이템을 구매할 수 있습니다.

9️⃣ /마니또 와 /친밀도 시스템을 통해 추가 보상을 획득할 수 있습니다.

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
/내보유
/상점
/마니또
/친밀도

좋은 인연과 즐거운 대화를 만들어보세요 😀"""

def operator_commands_text():
    return """🔒 운영진 전용 명령어

━━━━━━━━━━
💰 재화
━━━━━━━━━━
/지급 닉네임 금액
/차감 닉네임 금액
/코인내역 닉네임
/경제현황

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
/상품등록 상품명 가격 설명
/상품추가 상품명 가격 설명
/상품삭제 상품명
/아이템지급 닉네임 상품명
/사용 구매번호
/사용처리 구매번호
/구매취소 구매번호

━━━━━━━━━━
🎟 럭키드로우
━━━━━━━━━━
/럭키정산
/럭키초기화
/럭키현황전체

━━━━━━━━━━
⚙️ 시스템
━━━━━━━━━━
/방정보
/DB상태
/수집상태
/최근로그
/수집누락
/경고
/조각정리
/DM테스트 닉네임
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


def warning_text(date_str, source_id):
    rows = warning_list(date_str, source_id)
    rows = sorted(rows, key=lambda r: (int(r["count"] or 0), str(r["user_name"])))

    lines = [
        "⚠️ 오늘의 경고 대상",
        "",
        "기준",
        f"📌 {WARNING_LIMIT}마디 미만",
        "",
        "━━━━━━━━━━",
    ]

    if not rows:
        return (
            "✅ 오늘의 경고 대상이 없습니다.\n\n"
            "기준\n"
            f"📌 {WARNING_LIMIT}마디 미만\n\n"
            "현재 모든 인원이 기준을 충족했습니다."
        )

    for row in rows:
        lines.append(f"{row['user_name']} - {row['count']}마디")

    lines += [
        "━━━━━━━━━━",
        "",
        f"총 {len(rows)}명",
        "",
        "🚨 위험구간",
        f"{WARNING_LIMIT}마디 미만 인원입니다.",
        "운영진 확인 대상입니다.",
    ]

    return "\n".join(lines)



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


def purchase_status_counts(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT status, COUNT(*) AS cnt
    FROM purchases
    WHERE user_id = ?
    GROUP BY status
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()

    result = {"owned": 0, "used": 0, "cancel": 0, "pending": 0, "done": 0}
    for row in rows:
        result[row["status"]] = int(row["cnt"] or 0)
    return result


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
    ("daily_500", 500, 2),  # 500마디 = 0.2코인
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
    reward = 5  # 0.5코인

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

# 주간 가챠 횟수 제한
# KST 기준 매주 토요일 00:00에 새 가챠 주차로 자동 초기화됩니다.
# 이용 가능 시간: 토요일 00:00 ~ 21:00 이전
WEEKLY_GACHA_LIMIT = 15

GACHA_TYPE_LABELS = {
    "coin": "코인형",
    "piece": "조각형",
    "random": "랜덤형",
}

PIECE_INFO = {
    "iron": {"label": "철 조각", "need": 10, "reward": 5},
    "silver": {"label": "은 조각", "need": 10, "reward": 10},
    "gold": {"label": "금 조각", "need": 10, "reward": 20},
}
OLD_PIECE_KEYS = {"선갠라", "단벙", "봇등록", "미션", "임티", "칭호"}


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
        # 코인형: 과도한 복사 방지 밸런스
        # 손해 50.1%, 본전 24.9%, 소이득 20%, 고이득 5% 내외
        if tier == "하":
            return weighted_pick([
                (50.1, "F"), (24.9, "E"), (17, "D"), (6, "C"), (2, "B")
            ])
        if tier == "중":
            return weighted_pick([
                (50.1, "F"), (24.9, "E"), (17, "D"), (6, "C"), (1.7, "B"), (0.3, "A")
            ])
        return weighted_pick([
            (50.1, "F"), (24.9, "E"), (17, "D"), (5.5, "C"), (2.0, "B"), (0.45, "A"), (0.05, "S")
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


def random_piece_by_group(group=None):
    return weighted_pick([(60, "iron"), (30, "silver"), (10, "gold")])


def coin_prize_for(tier, grade):
    prize_table = {
        "하": {
            "F": [2, 3, 5],      # 0.2~0.5코인
            "E": [10],           # 본전 1코인
            "D": [12],           # 1.2코인
            "C": [15],           # 1.5코인
            "B": [20],           # 2코인
        },
        "중": {
            "F": [10, 15, 20],   # 1~2코인
            "E": [30],           # 본전 3코인
            "D": [35, 40],       # 3.5~4코인
            "C": [50],           # 5코인
            "B": [70],           # 7코인
            "A": [100],          # 10코인
        },
        "상": {
            "F": [20, 30, 40],   # 2~4코인
            "E": [50],           # 본전 5코인
            "D": [60, 70],       # 6~7코인
            "C": [90],           # 9코인
            "B": [120],          # 12코인
            "A": [180],          # 18코인
            "S": [250],          # 25코인
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

    value = table[grade]
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


def is_gacha_open_now():
    """
    가챠 운영시간 체크.
    KST 기준 매주 토요일 00:00 이상 21:00 미만만 이용 가능.
    Python weekday(): 월=0, 토=5
    """
    now = datetime.now(KST)
    return now.weekday() == 5 and 0 <= now.hour < 21


def gacha_closed_text():
    return (
        "🎰 가챠 운영시간이 아닙니다.\n\n"
        "운영시간\n"
        "매주 토요일 00:00 ~ 21:00\n\n"
        "21시 이후에는 다음 주 토요일에 이용 가능합니다.\n\n"
        "※ 가챠는 봇 1:1 개인채팅에서만 이용 가능합니다."
    )


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
    week_start, week_end = gacha_week_range_for_today()
    conn = db()
    cur = conn.cursor()
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
    conn.commit()
    conn.close()
    return get_weekly_gacha_count(user_id)


def gacha_count_status_text(user_id):
    week_start, week_end = gacha_week_range_for_today()
    used = get_weekly_gacha_count(user_id)
    remain = max(0, WEEKLY_GACHA_LIMIT - used)
    return (
        "🎰 주간 가챠 사용 현황\n\n"
        f"기간: {week_start} ~ {week_end}\n"
        f"사용: {used} / {WEEKLY_GACHA_LIMIT}회\n"
        f"남은 횟수: {remain}회\n\n"
        "※ 매주 토요일 00:00(KST)에 자동 초기화됩니다."
    )


def run_gacha(user_id, user_name, tier):
    if tier not in GACHA_COSTS:
        return False, "사용법\n\n/가챠 하\n/가챠 중\n/가챠 상"

    if not is_gacha_open_now():
        return False, gacha_closed_text()

    gacha_type = "coin"
    cost = GACHA_COSTS[tier]
    balance = get_balance(user_id)

    used_count = get_weekly_gacha_count(user_id)
    if used_count >= WEEKLY_GACHA_LIMIT:
        return False, (
            "🎰 이번 주 가챠 횟수를 모두 사용했습니다.\n\n"
            f"사용: {used_count} / {WEEKLY_GACHA_LIMIT}회\n"
            "초기화: 매주 토요일 00:00(KST)\n\n"
            "확인: /가챠횟수"
        )

    if balance < cost:
        return False, (
            f"코인이 부족합니다.\n\n"
            f"필요: {coin_text(cost)}\n"
            f"보유: {coin_text(balance)}"
        )

    change_money(user_id, user_name, -cost, f"{tier} 가챠 이용", None, "가챠시스템")
    weekly_used_after = add_weekly_gacha_count(user_id, user_name)

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
    lines.append(f"이번 주 가챠: {weekly_used_after} / {WEEKLY_GACHA_LIMIT}회")
    lines.append(f"현재 잔액: {coin_text(get_balance(user_id))}")

    return True, "\n".join(lines)


def gacha_system_text():
    return (
        "🎰 가챠 시스템 🎰\n\n"
        "운영시간\n"
        "매주 토요일 00:00 ~ 21:00\n\n"
        "※ 가챠는 봇 1:1 개인채팅 전용입니다.\n"
        "※ 주간 최대 15회입니다.\n"
        "※ 상/중/하/조각가챠 횟수는 합산됩니다.\n\n"
        "━━━━━━━━━━\n"
        "💰 코인 가챠\n"
        "━━━━━━━━━━\n\n"
        "/하가챠 : 1코인\n"
        "/중가챠 : 3코인\n"
        "/상가챠 : 5코인\n\n"
        "공통 손해확률: 50.1%\n"
        "많이 돌릴수록 평균적으로 손해가 나게 설계되어 있습니다.\n\n"
        "━━━━━━━━━━\n"
        "🧩 조각 가챠\n"
        "━━━━━━━━━━\n\n"
        "/조각가챠 : 1코인\n"
        "성공확률: 51%\n"
        "획득 조각: 철 / 은 / 금\n\n"
        "━━━━━━━━━━\n"
        "🔨 대장장이\n"
        "━━━━━━━━━━\n\n"
        "철 조각 10개 → 0.5코인\n"
        "은 조각 10개 → 1코인\n"
        "금 조각 10개 → 2코인\n\n"
        "확인: /조각\n"
        "교환: /대장장이\n"
        "횟수: /가챠횟수"
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

        with ApiClient(config) as client:
            api = MessagingApi(client)
            api.push_message(
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


def broadcast_hidden_reward_to(source_id, reason, user_name, reward):
    """히든 보상 알림을 해당 방으로 발송."""
    try:
        from linebot.v3.messaging import PushMessageRequest, TextMessage

        msg = (
            "🎉 히든 보상 달성!\n\n"
            f"{reason}\n"
            f"달성자: {user_name}\n"
            f"보상: 💰{coin_text(reward)}"
        )

        with ApiClient(config) as client:
            api = MessagingApi(client)
            api.push_message(
                PushMessageRequest(
                    to=source_id,
                    messages=[TextMessage(text=msg)]
                )
            )
    except Exception as e:
        print("HIDDEN_BROADCAST_TO_ERROR:", e)


def grant_daily_chat_jackpot(date_str, source_id, seq, user_id, user_name, reward, reason, meta=""):
    """
    당일 + 방 + 순번별 1회만 보상 지급.
    hidden_rewards에 저장하고, 지급 성공 시 코인 지급 + 방 알림.
    """
    mission_key = daily_jackpot_mission_key(source_id, seq)

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
        meta or f"source_id={source_id};seq={seq}",
        now_str()
    ))

    inserted = cur.rowcount
    conn.commit()
    conn.close()

    if not inserted:
        return False

    change_money(
        user_id,
        user_name,
        reward,
        reason,
        None,
        "채팅잭팟"
    )

    broadcast_hidden_reward_to(source_id, reason, user_name, reward)
    return True


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
            ok = grant_daily_chat_jackpot(
                date_str,
                source_id,
                pending_seq,
                user_id,
                user_name,
                pending_reward,
                f"{pending_reason} / 봇 순번으로 다음 채팅자 지급",
                f"source_id={source_id};seq={pending_seq};pending_to_next=1"
            )
            if ok:
                paid.append((pending_seq, pending_reward))

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

        ok = grant_daily_chat_jackpot(
            date_str,
            source_id,
            target_seq,
            user_id,
            user_name,
            reward,
            reason,
            f"source_id={source_id};seq={seq};lucky_number={lucky_number}"
        )
        if ok:
            paid.append((target_seq, reward))

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

    conn.commit()
    conn.close()

    for rank, winner, prize in ranks:
        change_money(winner["user_id"], winner["user_name"], prize, f"S.N.S 럭키드로우 {rank}등 {week_start}~{week_end}", None, settled_by)

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

    try:
        from linebot.v3.messaging import PushMessageRequest, TextMessage
        with ApiClient(config) as client:
            api = MessagingApi(client)
            api.push_message(PushMessageRequest(to=COUNT_SOURCE_ID, messages=[TextMessage(text=msg)]))
        return True
    except Exception as e:
        print("SNS_LUCKY_AUTO_PUSH_ERROR:", e)
        return False


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
# 업적 / 현상금
# =========================
BOUNTY_REQUIRED_COUNT = 5
BOUNTY_REWARD = 10  # 1코인

ACHIEVEMENT_CATALOG = [
    ("first_attendance", "✅ 첫 출석", "출석을 처음 완료", 2),
    ("first_gacha", "🎰 첫 가챠", "가챠를 처음 이용", 2),
    ("first_lucky", "🎟️ 첫 럭키드로우", "S.N.S 럭키드로우 첫 참여", 2),
    ("bounty_complete", "🎯 첫 현상금", "현상금을 처음 완료", 5),
    ("first_manitto", "🎭 첫 마니또", "마니또를 처음 성공", 5),
    ("daily_500_chatter", "💬 수다왕", "하루 500마디 달성", 10),
    ("weekly_500_emperor", "👑 수다황제", "7일 연속 500마디 이상 달성", 50),
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
            granted.append("💬 수다왕")

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
                granted.append("👑 수다황제")

    return granted

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
            ("weekly_bounties", "hunter_user_id"),
            ("weekly_bounties", "target_user_id"),
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



def format_hard_delete_warning(target):
    return (
        "⚠️ 완전삭제 경고\n\n"
        f"대상\n{target['user_name']}\n\n"
        "아래 기록이 DB에서 영구 삭제됩니다.\n"
        "- 유저 정보\n"
        "- 코인 / 코인 내역\n"
        "- 구매 내역\n"
        "- 마디수 / 채팅 로그\n"
        "- 출석 / 미션 / 업적\n"
        "- 가챠 / 조각 / 행운포인트\n"
        "- 주간랭킹 / 이벤트 / 마니또 / 친밀도 기록\n\n"
        "현재 최종 삭제 흐름은 /닉삭제 → /닉삭제번호 → /완전삭제 입니다.\n"
        "완전삭제 시 삭제유저 DB로 이동하며 /삭제복구 로 복구할 수 있습니다."
    )


def format_hard_delete_done(target_name, deleted_users, deleted_counts, deleted_names, deleted_detail):
    names_text = "\n".join([f"- {name}" for name in deleted_names]) or f"- {target_name}"
    detail_lines = []
    for table, count in sorted(deleted_detail.items()):
        if count:
            detail_lines.append(f"- {table}: {count}개")

    detail_text = "\n".join(detail_lines) if detail_lines else "- 삭제된 세부 기록 없음"

    return (
        "🗑️ 완전삭제 완료\n\n"
        f"대상: {target_name}\n"
        f"삭제 유저DB: {deleted_users}명\n"
        f"삭제 마디수 데이터: {deleted_counts}개\n"
        f"삭제 전체 기록: {sum(deleted_detail.values())}개\n\n"
        f"삭제된 닉네임:\n{names_text}\n\n"
        f"삭제 상세:\n{detail_text}\n\n"
        "DB에서 완전히 제거되었습니다."
    )







def jagiya_achievement_notice(user_name, other_name):
    return (
        "🏆 신규 업적 달성!\n\n"
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
        jagiya_msg = grant_jagiya_achievement_if_ready(
            user_id, user_name,
            last["user_id"], last["user_name"],
            cumulative_score
        )
        if jagiya_msg:
            messages.append(jagiya_msg)
    except Exception as e:
        print("JAGIYA_ACHIEVEMENT_ERROR:", repr(e))

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
        "10포인트 달성 시 1코인 자동 지급"
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
    if not is_gacha_open_now():
        return False, gacha_closed_text()
    used_count = get_weekly_gacha_count(user_id)
    if used_count >= WEEKLY_GACHA_LIMIT:
        return False, f"🎰 이번 주 가챠 횟수를 모두 사용했습니다.\n\n사용: {used_count} / {WEEKLY_GACHA_LIMIT}회"
    cost = 10
    balance = get_balance(user_id)
    if balance < cost:
        return False, f"코인이 부족합니다.\n\n필요: {coin_text(cost)}\n보유: {coin_text(balance)}"
    change_money(user_id, user_name, -cost, "조각가챠 이용", None, "가챠시스템")
    used_after = add_weekly_gacha_count(user_id, user_name)
    if random.random() < 0.51:
        piece_key = random_piece_by_group()
        add_simple_piece(user_id, user_name, piece_key, 1)
        label = PIECE_INFO[piece_key]["label"]
        result = f"획득: {label} x1"
    else:
        result = "획득: 꽝"
    return True, f"🧩 조각가챠 결과\n\n{result}\n\n이번 주 가챠: {used_after} / {WEEKLY_GACHA_LIMIT}회\n현재 잔액: {coin_text(get_balance(user_id))}"


def blacksmith_exchange(user_id, user_name):
    conn = db()
    cur = conn.cursor()
    paid = []
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
    conn.commit()
    conn.close()
    if not paid:
        return "🔨 대장장이\n\n교환 가능한 조각이 없습니다.\n\n철/은/금 조각은 각 10개 단위로 교환됩니다."
    total = sum(x[2] for x in paid)
    change_money(user_id, user_name, total, "대장장이 조각 교환", None, "대장장이")
    lines = ["🔨 대장장이 교환 완료", ""]
    for label, sets, reward in paid:
        lines.append(f"{label} 10개 x{sets}세트 → {coin_text(reward)}")
    lines += ["", f"총 지급: {coin_text(total)}", f"현재 보유: {coin_text(get_balance(user_id))}"]
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
        return (
            f"{title}\n\n"
            "✅ 미션 성공\n\n"
            f"대상\n{row['target_user_name']}\n\n"
            f"달성 친밀도\n{int(row['required_score'] or MANITTO_REQUIRED_SCORE)} / {int(row['required_score'] or MANITTO_REQUIRED_SCORE)}\n\n"
            "🎁 보상은 이미 지급 완료되었습니다.\n\n"
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
    cur.execute("""
    UPDATE manitto_assignments
    SET completed = 1,
        completed_at = ?,
        updated_at = ?
    WHERE week_start = ?
      AND hunter_user_id = ?
      AND completed = 0
    """, (now_str(), now_str(), week_start, hunter_user_id))
    changed = cur.rowcount
    conn.commit()
    conn.close()

    if not changed:
        return None

    change_money(
        hunter_user_id,
        hunter_user_name,
        reward,
        f"마니또 성공: {row['target_user_name']}",
        None,
        "마니또"
    )

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
        push_private_message(hunter_user_id, dm_text)
    except Exception as e:
        print("MANITTO_REWARD_DM_ERROR:", repr(e))

    return public_text


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
            "🏆 신규 업적 달성!\n\n"
            "💕 자기야\n\n"
            f"{user_name_1}님과 {user_name_2}님이\n"
            f"누적 친밀도 {total_score}을 달성했습니다.\n\n"
            f"보상: 각 {coin_text(AFFINITY_CUMULATIVE_JAGIYA_REWARD)}"
        )
    return None




# =========================
# 마니또/친밀도 활성화 보정
# =========================
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

    lines = ["💞 이번 주 친밀도", f"기간: {week_start} ~ {week_end}", ""]
    if not rows:
        lines.append("이번 주 친밀도 기록이 없습니다.")
    else:
        for i, row in enumerate(rows, 1):
            other_name = row["user_b_name"] if row["user_a"] == user_id else row["user_a_name"]
            lines.append(f"{i}. {other_name} - {row['score']}")

    lines += ["", "친밀도 확인: /친밀도"]
    return "\n".join(lines)


def cumulative_affinity_status_text(user_id, user_name):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_a, user_b, user_a_name, user_b_name, total_score, updated_at
    FROM affinity_cumulative_scores
    WHERE user_a = ? OR user_b = ?
    ORDER BY total_score DESC, updated_at DESC
    LIMIT 20
    """, (user_id, user_id))
    rows = cur.fetchall()
    conn.close()

    lines = ["🌱 누적 친밀도", f"대상: {user_name}", ""]
    if not rows:
        lines.append("누적 친밀도 기록이 없습니다.")
    else:
        for i, row in enumerate(rows, 1):
            other_name = row["user_b_name"] if row["user_a"] == user_id else row["user_a_name"]
            total = int(row["total_score"] or 0)
            mark = " 💕" if total >= AFFINITY_CUMULATIVE_JAGIYA_SCORE else ""
            lines.append(f"{i}. {other_name} - {total}{mark}")

    lines += [
        "",
        "💕 자기야 업적",
        f"상대와 누적 친밀도 {AFFINITY_CUMULATIVE_JAGIYA_SCORE} 달성 시",
        f"각 {coin_text(AFFINITY_CUMULATIVE_JAGIYA_REWARD)} 지급",
    ]
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
# 개인 메시지 / 내정보 통합
# =========================
def my_info_text(user_id, user_name):
    balance = get_balance(user_id)
    pity = get_gacha_pity_point(user_id)
    week_start, week_end = gacha_week_range_for_today()
    gacha_used = get_weekly_gacha_count(user_id)
    gacha_remain = max(0, WEEKLY_GACHA_LIMIT - gacha_used)

    lines = [
        "📌 S.N.S 내정보",
        "",
        f"닉네임: {user_name}",
        f"보유 코인: {coin_text(balance)}",
        f"행운포인트: {pity} / 10",
        "",
        "🎰 이번 주 가챠",
        f"기간: {week_start} ~ {week_end}",
        f"사용: {gacha_used} / {WEEKLY_GACHA_LIMIT}회",
        f"남음: {gacha_remain}회",
        "이용시간: 토요일 00:00 ~ 21:00",
    ]

    try:
        row, err = ensure_weekly_manitto(user_id, user_name)
        if row and not err:
            score = get_affinity_score(user_id, row["target_user_id"], row["week_start"])
            reward_text = coin_text(row["reward"]) if row["reward"] else f"{coin_text(row['reward_min'])} ~ {coin_text(row['reward_max'])}"
            lines += [
                "",
                "🎭 마니또",
                f"타겟: {row['target_user_name']}",
                f"진행도: {score} / {row['required_score']}",
                f"보상: {reward_text}",
                "상태: 완료" if row["completed"] else "상태: 진행중",
            ]
        elif err:
            lines += ["", "🎭 마니또", err]
    except Exception as e:
        print("MY_INFO_MANITTO_ERROR:", e)

    try:
        w_start, _ = event_week_key()
        conn = db()
        cur = conn.cursor()
        cur.execute("""
        SELECT user_a, user_b, user_a_name, user_b_name, score
        FROM affinity_scores
        WHERE week_start = ? AND (user_a = ? OR user_b = ?)
        ORDER BY score DESC, updated_at DESC
        LIMIT 3
        """, (w_start, user_id, user_id))
        affinity_rows = cur.fetchall()
        conn.close()
        lines += ["", "💕 친밀도 TOP3"]
        if not affinity_rows:
            lines.append("이번 주 친밀도 기록이 없습니다.")
        else:
            for i, row in enumerate(affinity_rows, 1):
                other = row["user_b_name"] if row["user_a"] == user_id else row["user_a_name"]
                lines.append(f"{i}. {other} - {row['score']}")
    except Exception as e:
        print("MY_INFO_AFFINITY_ERROR:", e)

    try:
        piece_rows = get_all_gacha_pieces(user_id)
        lines += ["", "🧩 조각"]
        if not piece_rows:
            lines.append("보유한 조각이 없습니다.")
        else:
            for row in piece_rows[:8]:
                info = PIECE_INFO.get(row["piece_key"])
                if info:
                    lines.append(f"{info['label']} {row['count']} / {info['need']}")
            if len(piece_rows) > 8:
                lines.append(f"외 {len(piece_rows) - 8}개")
    except Exception as e:
        print("MY_INFO_PIECE_ERROR:", e)

    try:
        counts = purchase_status_counts(user_id)
        lines += ["", "🎁 보유 상품"]
        lines.append(f"미사용: {counts.get('owned', 0) + counts.get('pending', 0)}개")
        lines.append(f"사용완료: {counts.get('used', 0) + counts.get('done', 0)}개")
        if counts.get('cancel', 0):
            lines.append(f"취소됨: {counts.get('cancel', 0)}개")
        lines.append("자세히 보기: /내보유")
    except Exception as e:
        print("MY_INFO_PURCHASE_ERROR:", e)

    lines += ["", "자세히 보기", "/내보유 /친밀도 /업적 /조각 /가챠횟수"]
    return "\n".join(lines)


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


def title_text(user_id, user_name):
    return (
        f"👑 {user_name}\n\n"
        "현재 칭호\n\n"
        f"{get_public_title(user_id)}\n\n"
        "※ 칭호는 업적, 럭키드로우, 코인, 출석, 친밀도 기록을 기준으로 자동 표시됩니다."
    )


def profile_text(target_user_id, target_user_name):
    best_name, best_score = get_best_affinity(target_user_id)
    lines = [
        f"👤 {target_user_name}", "",
        "👑 칭호", get_public_title(target_user_id), "",
        "💰 코인", coin_text(get_balance(target_user_id)), "",
        "🏆 업적", f"{get_achievement_count(target_user_id)}개", "",
        "📅 출석", f"{get_attendance_count(target_user_id)}일", "",
        "💕 최고 누적 친밀도",
    ]
    lines.append(f"{best_name} ({best_score})" if best_name else "누적 친밀도 기록 없음")
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
    rows = find_users(keyword, limit=5)
    if not rows:
        return False, f"대상을 찾을 수 없습니다.\n검색어: {keyword}"
    if len(rows) > 1:
        lines = [f"검색 결과가 여러 명입니다: {keyword}", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']}")
        lines.append("\n더 정확한 닉네임으로 다시 입력해주세요.")
        return False, "\n".join(lines)
    target = rows[0]
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
    flag_key = f"auto_weekly_settlement:{date_str}"

    try:
        if get_system_flag(flag_key):
            return
    except Exception as e:
        print("AUTO_WEEKLY_FLAG_READ_ERROR:", repr(e))
        return

    try:
        result_text = None

        if "weekly_settlement_text" in globals():
            result_text = weekly_settlement_text(COUNT_SOURCE_ID)
        elif "weekly_settlement" in globals():
            result_text = weekly_settlement(COUNT_SOURCE_ID)
        elif "settle_weekly_rewards" in globals():
            result_text = settle_weekly_rewards(COUNT_SOURCE_ID)
        elif "weekly_reward_settlement" in globals():
            result_text = weekly_reward_settlement(COUNT_SOURCE_ID)
        else:
            result_text = "⚠️ 자동 주간정산 실패\n\n주간정산 함수를 찾지 못했습니다."

        set_system_flag(flag_key, "done")

        notify_text = "🏆 자동 주간정산 완료\n\n" + str(result_text)
        if ADMIN_SOURCE_IDS:
            for sid in ADMIN_SOURCE_IDS:
                push_private_message(sid, notify_text)
        elif ADMIN_SOURCE_ID:
            push_private_message(ADMIN_SOURCE_ID, notify_text)

        print("AUTO_WEEKLY_SETTLEMENT_DONE:", date_str)

    except Exception as e:
        print("AUTO_WEEKLY_SETTLEMENT_ERROR:", repr(e))


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
            print("WEEKLY_SETTLEMENT_SCHEDULER_ERROR:", repr(e))
            time.sleep(60)


def start_weekly_settlement_scheduler():
    t = threading.Thread(target=weekly_settlement_scheduler_loop, daemon=True)
    t.start()





# =========================
# 운영 명령어 호환 함수
# =========================
def grant_user_title(user_id, user_name, title, created_by=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE user_titles SET is_active = 0 WHERE user_id = ?", (user_id,))
    cur.execute("""
    INSERT INTO user_titles (user_id, user_name, title, is_active, created_by, created_at, updated_at)
    VALUES (?, ?, ?, 1, ?, ?, ?)
    """, (user_id, user_name, title, created_by, now_str(), now_str()))
    conn.commit()
    conn.close()


def remove_user_title(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE user_titles SET is_active = 0, updated_at = ? WHERE user_id = ?", (now_str(), user_id))
    conn.commit()
    conn.close()


def user_title_list_text():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_name, title, created_at
    FROM user_titles
    WHERE is_active = 1
    ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()

    lines = ["👑 칭호 목록", ""]
    if not rows:
        lines.append("활성 칭호가 없습니다.")
    else:
        for row in rows:
            lines.append(f"{row['user_name']} - {row['title']}")
    return "\n".join(lines)

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


def snapshot_user_data(user_id):
    conn = db()
    cur = conn.cursor()
    tables = [
        "users", "currency", "currency_logs", "purchases", "attendance", "mission_claims",
        "hidden_rewards", "gacha_settings", "gacha_pity", "gacha_pieces", "gacha_weekly_counts",
        "weekly_rewards", "sns_lucky_draw_entries", "achievements", "chat_logs", "counts",
    ]
    snap = {}
    for table in tables:
        try:
            cur.execute(f"SELECT * FROM {table} WHERE user_id = ?", (user_id,))
            snap[table] = [dict(r) for r in cur.fetchall()]
        except Exception:
            snap[table] = []
    for table, col in [("affinity_scores", "user_a"), ("affinity_scores", "user_b"), ("affinity_cumulative_scores", "user_a"), ("affinity_cumulative_scores", "user_b"), ("manitto_assignments", "hunter_user_id"), ("manitto_assignments", "target_user_id")]:
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

        save_chat_log(
            date_str,
            source_id,
            user_id,
            user_name,
            message_type,
            message_text
        )

        # 히든 미션 자동 체크
        try:
            check_hidden_1000_reward(date_str, source_id, user_id, user_name)
            check_hidden_2000_reward(date_str, source_id, user_id, user_name)
            check_daily_chat_jackpot_rewards(date_str, source_id, user_id, user_name)
            check_chatter_achievements(date_str, source_id, user_id, user_name)
        except Exception as e:
            print("HIDDEN_REWARD_ERROR:", e)

    if not isinstance(event.message, TextMessageContent):
        return

    text = (event.message.text or "").strip()

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
        if affinity_msg:
            reply_many(event.reply_token, split_text_messages(affinity_msg))
            return
    except Exception as e:
        print("AFFINITY_PROCESS_ERROR:", e)

    # 토요일 21시 자동 스케줄러가 기본 처리합니다. 메시지 수신 시에도 보조 확인합니다.
    try:
        maybe_auto_lucky_draw()
    except Exception as e:
        print("SNS_LUCKY_AUTO_ERROR:", e)


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
            "빌드: v10.4\n"
            "환경변수: ADMIN_SOURCE_ID / COUNT_SOURCE_ID / ADMIN_USER_IDS / OPERATOR_USER_IDS"
        )
        return

    if text.startswith("/DM테스트 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        if source_id not in ADMIN_SOURCE_IDS:
            reply(event.reply_token, "⛔ 운영방에서만 사용 가능합니다.")
            return
        keyword = text.replace("/DM테스트", "", 1).strip()
        target = find_user(keyword)
        if not target:
            reply(event.reply_token, f"❌ 유저를 찾을 수 없습니다.\n검색어: {keyword}")
            return
        target_user_id = str(target["user_id"] or "").strip()
        target_name = target["user_name"]
        if not target_user_id.startswith("U"):
            reply(event.reply_token, f"❌ USER_ID가 올바르지 않습니다.\n대상: {target_name}\nUSER_ID: {target_user_id}")
            return
        ok = push_private_message(target_user_id, "📩 DM 테스트 메시지입니다.\n이 메시지가 보이면 DM 전송은 정상입니다.")
        if ok:
            reply(event.reply_token, f"✅ DM 테스트 성공\n대상: {target_name}\nUSER_ID: {target_user_id}")
        else:
            reply(event.reply_token, f"❌ DM 테스트 실패\n대상: {target_name}\nUSER_ID: {target_user_id}\nRailway 로그의 [DM_FAIL] 내용을 확인해주세요.")
        return

    if text == "/DB상태":
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        conn = db()
        cur = conn.cursor()
        counts = []
        for table in ["users", "counts", "currency", "currency_logs", "purchases", "attendance", "mission_claims", "manitto_assignments", "affinity_scores"]:
            try:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
                counts.append(f"{table}: {cur.fetchone()['cnt']}")
            except Exception:
                counts.append(f"{table}: 확인 실패")
        conn.close()
        reply(event.reply_token, "🗄️ DB 상태\n\n" + "\n".join(counts))
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

    if text.startswith("/닉삭제"):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        keyword = text.replace("/닉삭제", "", 1).strip()
        if not keyword:
            reply(event.reply_token, "사용법: /닉삭제 닉네임")
            return
        rows = find_users(keyword, limit=10)
        if not rows:
            reply(event.reply_token, "대상 유저를 찾을 수 없습니다.")
            return
        DELETE_PENDING[user_id] = {"mode": "soft_delete", "candidates": rows}
        if len(rows) > 1:
            lines = ["검색 결과가 여러 명입니다.", ""]
            for i, row in enumerate(rows, 1):
                lines.append(f"{i}. {row['user_name']}")
            lines += ["", "삭제할 번호를 /닉삭제번호 번호 로 입력해주세요."]
            reply(event.reply_token, "\n".join(lines))
            return
        changed, name = set_user_active_by_id_with_name(rows[0]["user_id"], 0)
        DELETE_PENDING[user_id] = {"mode": "deleted_selected", "target": rows[0]}
        reply(event.reply_token, f"✅ 닉삭제 완료\n\n대상: {name}\n\n완전삭제를 원하면 /완전삭제 를 입력해주세요.")
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
            reply(event.reply_token, "번호를 확인해주세요.")
            return
        changed, name = set_user_active_by_id_with_name(target["user_id"], 0)
        DELETE_PENDING[user_id] = {"mode": "deleted_selected", "target": target}
        reply(event.reply_token, f"✅ 닉삭제 완료\n\n대상: {name}\n\n완전삭제를 원하면 /완전삭제 를 입력해주세요.")
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
        target = find_user(parts[1])
        if not target:
            reply(event.reply_token, "대상 유저를 찾을 수 없습니다.")
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
        target = find_user(parts[1])
        if not target:
            reply(event.reply_token, "대상 유저를 찾을 수 없습니다.")
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
        push_or_reply_private_info(event, user_id, "\n".join(lines), "📩 코인내역을 개인 메시지로 보내드렸습니다.")
        return

    if text.startswith("/코인내역 "):
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        parts = text.split(maxsplit=1)
        keyword = parts[1].strip()
        target = find_user(keyword)
        if not target:
            reply(event.reply_token, "대상 유저를 찾을 수 없습니다.")
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
        if not is_staff(user_id):
            reply(event.reply_token, operator_only_warning())
            return
        try:
            purchase_id = int(text.split()[1])
        except Exception:
            reply(event.reply_token, "사용법: /사용 구매번호")
            return
        ok, msg = staff_use_purchase(purchase_id, user_name)
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
        target = find_user(parts[1])
        if not target:
            reply(event.reply_token, "대상 유저를 찾을 수 없습니다.")
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

    # =========================
    # 유저 명령어
    # =========================
    if text == "/가이드":
        # 가이드는 개인정보가 아니므로 DM 실패 시 공개창에 본문을 표시합니다.
        if is_private_chat(event):
            reply_many(event.reply_token, split_text_messages(beginner_guide_text()))
        else:
            ok = push_private_message(user_id, beginner_guide_text()) if user_id else False
            if ok:
                reply(event.reply_token, "📩 가이드를 개인 메시지로 보내드렸습니다.")
            else:
                reply_many(event.reply_token, split_text_messages(beginner_guide_text()))
        return

    if text == "/명령어":
        reply_many(event.reply_token, split_text_messages(user_commands_text()))
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
        push_or_reply_private_info(event, user_id, affinity_ranking_text(limit=10), "📩 친밀도 랭킹을 개인 메시지로 보내드렸습니다.")
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
                paid_lines = [f"{days}일 연속 출석 보상 {coin_text(reward)}" for days, reward in streak_paid]
                extra = "\n\n🎁 연속출석 보상\n" + "\n".join(paid_lines)
            reply(event.reply_token, f"✅ 출석 완료\n\n{user_name}님\n보상: {coin_text(5)}\n현재 보유: {coin_text(balance)}{extra}")
        else:
            reply(event.reply_token, f"이미 오늘 출석했습니다.\n\n현재 보유: {coin_text(balance)}")
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

    if text == "/친밀도" or text.startswith("/친밀도 "):
        push_or_reply_private_info(event, user_id, affinity_status_text(user_id, user_name), "📩 친밀도 정보를 개인 메시지로 보내드렸습니다.")
        return

    if text in ["/잔액", "/내보유"]:
        push_or_reply_private_info(event, user_id, f"💰 {user_name}님의 보유 코인\n\n{coin_text(get_balance(user_id))}", "📩 보유 정보를 개인 메시지로 보내드렸습니다.")
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
        push_or_reply_private_info(event, user_id, achievement_status_text(user_id, user_name), "📩 업적 현황을 개인 메시지로 보내드렸습니다.")
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

    if text == "/럭키드로우결과":
        push_or_reply_private_info(event, user_id, lucky_draw_result_text(), "📩 럭키드로우 결과를 개인 메시지로 보내드렸습니다.")
        return

    # =========================
    # 1:1 전용 명령어
    # =========================
    if text in ["/가챠", "/가챠시스템", "/가챠횟수", "/상가챠", "/중가챠", "/하가챠", "/조각가챠", "/조각", "/대장장이", "/상점", "/럭키드로우", "/럭키드로우구매", "/럭키드로우현황", "/럭키드로우결과"] or text.startswith("/구매 "):
        if not is_private_chat(event):
            if text.startswith("/가챠") or text in ["/상가챠", "/중가챠", "/하가챠", "/조각가챠", "/조각", "/대장장이", "/가챠시스템", "/가챠횟수"]:
                private_only_notice(event, user_id, gacha_private_guide_text(), "가챠")
            elif text in ["/상점"] or text.startswith("/구매 "):
                private_only_notice(event, user_id, shop_private_guide_text(), "상점")
            else:
                private_only_notice(event, user_id, "꽃봇 1:1 채팅에서 이용해주세요.", "개인 기능")
            return

        if text == "/가챠":
            reply_many(event.reply_token, split_text_messages(gacha_system_text()))
            return

        if text in ["/상가챠", "/중가챠", "/하가챠"]:
            tier = text[1]
            success, message = run_gacha(user_id, user_name, tier)
            if success:
                grant_achievement_once(user_id, user_name, "first_gacha", "🎰 첫 가챠", 2, tier)
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


# 럭키드로우 자동 정산 스케줄러 시작
start_lucky_draw_auto_scheduler()



# 자동 주간정산 스케줄러 시작
try:
    if os.getenv("DISABLE_AUTO_WEEKLY_SETTLEMENT", "0") != "1":
        start_weekly_settlement_scheduler()
except Exception as e:
    print("START_WEEKLY_SETTLEMENT_SCHEDULER_ERROR:", repr(e))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
