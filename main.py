import os
import sqlite3
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
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
)

load_dotenv()

TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

COUNT_SOURCE_ID = os.getenv("COUNT_SOURCE_ID", "").strip()
ADMIN_SOURCE_ID = os.getenv("ADMIN_SOURCE_ID", "").strip()

ADMIN_USER_IDS = {
    x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
}

DB_PATH = os.getenv("DB_PATH", "madi_counter.db").strip()
PORT = int(os.getenv("PORT", "5000").strip())

if not TOKEN or not SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET 값을 설정해야 합니다.")

KST = timezone(timedelta(hours=9))

app = Flask(__name__)
handler = WebhookHandler(SECRET)
config = Configuration(access_token=TOKEN)


# =========================
# 시간 관련
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

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
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL,
        last_seen_source_id TEXT,
        updated_at TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================
# 공통 함수
# =========================
def get_source_id(event):
    source = event.source

    if source.type == "group":
        return source.group_id
    if source.type == "room":
        return source.room_id
    return source.user_id


def get_user_name(event):
    user_id = getattr(event.source, "user_id", None)
    source = event.source

    if not user_id:
        return "unknown"

    try:
        with ApiClient(config) as client:
            api = MessagingApi(client)

            if source.type == "group":
                profile = api.get_group_member_profile(source.group_id, user_id)
                return profile.display_name

            elif source.type == "room":
                profile = api.get_room_member_profile(source.room_id, user_id)
                return profile.display_name

            else:
                profile = api.get_profile(user_id)
                return profile.display_name

    except Exception as e:
        print("닉네임 조회 실패:", e)
        return f"user_{user_id[-4:]}"


def upsert_user(user_id, user_name, source_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO users (user_id, user_name, last_seen_source_id, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        user_name = excluded.user_name,
        last_seen_source_id = excluded.last_seen_source_id,
        updated_at = excluded.updated_at
    """, (user_id, user_name, source_id, now_str()))

    cur.execute("""
    UPDATE counts
    SET user_name = ?
    WHERE user_id = ?
    """, (user_name, user_id))

    conn.commit()
    conn.close()


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


def ranking(date_str, target_source_id, limit=10):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT user_name, count
    FROM counts
    WHERE date=? AND source_id=?
    ORDER BY count DESC, user_name ASC
    LIMIT ?
    """, (date_str, target_source_id, limit))

    rows = cur.fetchall()
    conn.close()
    return rows


def all_counts_with_zero(date_str, target_source_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT u.user_name,
           COALESCE(c.count, 0) AS count
    FROM users u
    LEFT JOIN counts c
      ON u.user_id = c.user_id
     AND c.date = ?
     AND c.source_id = ?
    WHERE u.last_seen_source_id = ?
    ORDER BY count DESC, u.user_name ASC
    """, (date_str, target_source_id, target_source_id))

    rows = cur.fetchall()
    conn.close()
    return rows


def reply(reply_token, text):
    with ApiClient(config) as client:
        api = MessagingApi(client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )


# =========================
# 웹훅
# =========================
@app.route("/", methods=["GET"])
def home():
    return "RUNNING"


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
# 이벤트 처리
# =========================
@handler.add(MessageEvent)
def handle(event):
    source_id = get_source_id(event)
    user_id = getattr(event.source, "user_id", None)
    user_name = get_user_name(event)
    date_str = today()

    print("SOURCE_ID:", source_id)
    print("USER_ID:", user_id)
    print("USER_NAME:", user_name)

    # 봇이 본 사용자 저장
    if user_id:
        upsert_user(user_id, user_name, source_id)

    # 메인방에서 모든 메시지 카운트
    if source_id == COUNT_SOURCE_ID and user_id:
        add_count(date_str, COUNT_SOURCE_ID, user_id, user_name)

    # 텍스트 아니면 끝
    if not isinstance(event.message, TextMessageContent):
        return

    text = (event.message.text or "").strip()

    # 관리자만 현재 방 ID 확인 가능
    if text == "/방정보":
        if user_id not in ADMIN_USER_IDS:
            return
        reply(event.reply_token, f"방ID:\n{source_id}")
        return

    # 운영진방이 아니면 조용히 무시
    if source_id != ADMIN_SOURCE_ID:
        return

    # 운영진방에서도 관리자만 조회 가능
    if user_id not in ADMIN_USER_IDS:
        return

    if text.startswith("/도움말"):
        reply(
            event.reply_token,
            "사용 가능한 명령어\n"
            "/마디수\n"
            "/마디수 YYYY-MM-DD\n"
            "/순위\n"
            "/순위 YYYY-MM-DD\n"
            "/방정보"
        )
        return

    if text.startswith("/마디수"):
        target_date = parse_date(text)
        rows = all_counts_with_zero(target_date, COUNT_SOURCE_ID)

        if not rows:
            reply(event.reply_token, f"메인방 {target_date} 마디수 데이터가 없습니다.")
            return

        msg_lines = [
            "📊 메인방 전체 마디수",
            f"날짜: {target_date}",
            ""
        ]

        for i, row in enumerate(rows, 1):
            msg_lines.append(f"{i}. {row['user_name']} - {row['count']}")

        msg = "\n".join(msg_lines)

        if len(msg) > 4500:
            msg = msg[:4400] + "\n...\n인원이 많아서 일부만 표시됐습니다."

        reply(event.reply_token, msg)
        return

    if text.startswith("/순위"):
        target_date = parse_date(text)
        rows = ranking(target_date, COUNT_SOURCE_ID, limit=10)

        if not rows:
            reply(event.reply_token, f"메인방 {target_date} 순위 데이터가 없습니다.")
            return

        msg_lines = [
            "🏆 메인방 순위 TOP 10",
            f"날짜: {target_date}",
            ""
        ]

        for i, row in enumerate(rows, 1):
            msg_lines.append(f"{i}. {row['user_name']} - {row['count']}")

        reply(event.reply_token, "\n".join(msg_lines))
        return


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)