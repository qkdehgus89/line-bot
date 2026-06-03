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
from linebot.v3.webhooks import MessageEvent, TextMessageContent

load_dotenv()

TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

COUNT_SOURCE_ID = os.getenv("COUNT_SOURCE_ID", "").strip()
ADMIN_SOURCE_ID = os.getenv("ADMIN_SOURCE_ID", "").strip()

ADMIN_USER_IDS = {
    x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
}

DB_PATH = os.getenv("DB_PATH", "madi_counter.db").strip()
PORT = int(os.getenv("PORT", "5000"))

MALE_LIMIT = int(os.getenv("MALE_LIMIT", "70"))
FEMALE_LIMIT = int(os.getenv("FEMALE_LIMIT", "50"))

if not TOKEN or not SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET 값을 설정해야 합니다.")

KST = timezone(timedelta(hours=9))

app = Flask(__name__)
handler = WebhookHandler(SECRET)
config = Configuration(access_token=TOKEN)


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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL,
        gender TEXT DEFAULT 'unknown',
        is_nomicl INTEGER DEFAULT 0,
        last_seen_source_id TEXT,
        updated_at TEXT NOT NULL
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

    conn.commit()
    conn.close()


init_db()


# =========================
# LINE 공통
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

            if source.type == "room":
                profile = api.get_room_member_profile(source.room_id, user_id)
                return profile.display_name

            profile = api.get_profile(user_id)
            return profile.display_name

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
        user_id, user_name, gender, is_nomicl, last_seen_source_id, updated_at
    )
    VALUES (?, ?, 'unknown', 0, ?, ?)
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
    INSERT INTO counts (
        date, source_id, user_id, user_name, count
    )
    VALUES (?, ?, ?, ?, 1)
    ON CONFLICT(date, source_id, user_id)
    DO UPDATE SET
        count = count + 1,
        user_name = excluded.user_name
    """, (date_str, source_id, user_id, user_name))

    conn.commit()
    conn.close()


def set_gender(user_name_keyword, gender):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE users
    SET gender = ?
    WHERE user_name LIKE ?
    """, (gender, f"%{user_name_keyword}%"))

    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def set_nomicl(user_name_keyword, value):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE users
    SET is_nomicl = ?
    WHERE user_name LIKE ?
    """, (value, f"%{user_name_keyword}%"))

    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


# =========================
# 조회
# =========================
def ranking(date_str, source_id, limit=None):
    conn = db()
    cur = conn.cursor()

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
    WHERE u.last_seen_source_id = ?
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
    WHERE u.last_seen_source_id = ?
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
        gender = row["gender"]
        count = row["count"]
        is_nomicl = row["is_nomicl"]

        if is_nomicl:
            continue

        if gender == "male" and count < MALE_LIMIT:
            result.append(row)

        elif gender == "female" and count < FEMALE_LIMIT:
            result.append(row)

        elif gender == "unknown":
            result.append(row)

    return result


# =========================
# 초기화 / 삭제
# =========================
def reset_date(date_str, source_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    DELETE FROM counts
    WHERE date = ?
      AND source_id = ?
    """, (date_str, source_id))

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

    cur.execute("DELETE FROM counts")
    deleted_counts = cur.rowcount

    cur.execute("DELETE FROM users")
    deleted_users = cur.rowcount

    conn.commit()
    conn.close()

    return deleted_users, deleted_counts


def delete_user_by_name(keyword):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT user_id, user_name
    FROM users
    WHERE user_name LIKE ?
    """, (f"%{keyword}%",))

    users = cur.fetchall()

    deleted_users = 0
    deleted_counts = 0
    deleted_names = []

    for user in users:
        cur.execute(
            "DELETE FROM counts WHERE user_id = ?",
            (user["user_id"],)
        )
        deleted_counts += cur.rowcount

        cur.execute(
            "DELETE FROM users WHERE user_id = ?",
            (user["user_id"],)
        )
        deleted_users += cur.rowcount
        deleted_names.append(user["user_name"])

    conn.commit()
    conn.close()

    return deleted_users, deleted_counts, deleted_names


# =========================
# 출력 포맷
# =========================
def gender_icon(gender):
    if gender == "male":
        return "💙"

    if gender == "female":
        return "❤️"

    return "🤍"


def nomicl_text(is_nomicl):
    return " / 노미클" if is_nomicl else ""


def format_rows(title, date_str, rows):
    lines = [
        title,
        f"날짜: {date_str}",
        ""
    ]

    if not rows:
        lines.append("데이터가 없습니다.")
        return "\n".join(lines)

    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. {gender_icon(row['gender'])} "
            f"{row['user_name']} - {row['count']}"
            f"{nomicl_text(row['is_nomicl'])}"
        )

    return "\n".join(lines)


def format_total_rows(title, rows):
    lines = [
        title,
        ""
    ]

    if not rows:
        lines.append("데이터가 없습니다.")
        return "\n".join(lines)

    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. {gender_icon(row['gender'])} "
            f"{row['user_name']} - {row['count']}"
            f"{nomicl_text(row['is_nomicl'])}"
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
    user_id = getattr(event.source, "user_id", None)
    user_name = get_user_name(event)
    date_str = today()

    print("SOURCE_ID:", source_id)
    print("USER_ID:", user_id)
    print("USER_NAME:", user_name)

    if user_id:
        upsert_user(user_id, user_name, source_id)

    if source_id == COUNT_SOURCE_ID and user_id:
        add_count(date_str, COUNT_SOURCE_ID, user_id, user_name)

    if not isinstance(event.message, TextMessageContent):
        return

    text = (event.message.text or "").strip()

    # 관리자만 방정보 확인
    if text == "/방정보":
        if user_id not in ADMIN_USER_IDS:
            return

        reply(
            event.reply_token,
            f"방정보\n\n"
            f"SOURCE_ID:\n{source_id}\n\n"
            f"USER_ID:\n{user_id}\n\n"
            f"닉네임:\n{user_name}"
        )
        return

    # 운영진방 아니면 무시
    if source_id != ADMIN_SOURCE_ID:
        return

    # 관리자 아니면 무시
    if user_id not in ADMIN_USER_IDS:
        return

    if text.startswith("/도움말"):
        reply(
            event.reply_token,
            "📌 마디수 봇 명령어\n\n"
            "조회\n"
            "/마디수\n"
            "/마디수 YYYY-MM-DD\n"
            "/순위\n"
            "/순위 YYYY-MM-DD\n"
            "/전체순위\n"
            "/경고\n"
            "/경고 YYYY-MM-DD\n"
            "/방정보\n\n"
            "성별 설정\n"
            "/남자 닉네임\n"
            "/여자 닉네임\n"
            "/성별해제 닉네임\n\n"
            "노미클 설정\n"
            "/노미클 닉네임\n"
            "/노미클해제 닉네임\n\n"
            "초기화\n"
            "/초기화 YYYY-MM-DD\n"
            "/전체초기화 확인\n"
            "/멤버초기화 확인\n"
            "/완전초기화 확인\n"
            "/닉삭제 닉네임\n\n"
            f"기준\n"
            f"남자 {MALE_LIMIT}마디 미만 경고\n"
            f"여자 {FEMALE_LIMIT}마디 미만 경고"
        )
        return

    # =========================
    # 조회 명령어
    # =========================
    if text.startswith("/마디수"):
        target_date = parse_date(text)
        rows = ranking(target_date, COUNT_SOURCE_ID)

        reply(
            event.reply_token,
            format_rows("📊 메인방 전체 마디수", target_date, rows)
        )
        return

    if text.startswith("/순위"):
        target_date = parse_date(text)
        rows = ranking(target_date, COUNT_SOURCE_ID, limit=10)

        reply(
            event.reply_token,
            format_rows("🏆 메인방 순위 TOP 10", target_date, rows)
        )
        return

    if text.startswith("/전체순위"):
        rows = total_ranking(COUNT_SOURCE_ID, limit=30)

        reply(
            event.reply_token,
            format_total_rows("🏆 전체 누적 순위 TOP 30", rows)
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

    # =========================
    # 성별 / 노미클 설정
    # =========================
    if text.startswith("/남자 "):
        name = text.replace("/남자", "", 1).strip()
        changed = set_gender(name, "male")
        reply(event.reply_token, f"💙 남자 설정 완료: {changed}명")
        return

    if text.startswith("/여자 "):
        name = text.replace("/여자", "", 1).strip()
        changed = set_gender(name, "female")
        reply(event.reply_token, f"❤️ 여자 설정 완료: {changed}명")
        return

    if text.startswith("/성별해제 "):
        name = text.replace("/성별해제", "", 1).strip()
        changed = set_gender(name, "unknown")
        reply(event.reply_token, f"🤍 성별 해제 완료: {changed}명")
        return

    if text.startswith("/노미클 "):
        name = text.replace("/노미클", "", 1).strip()
        changed = set_nomicl(name, 1)
        reply(event.reply_token, f"🌱 노미클 설정 완료: {changed}명")
        return

    if text.startswith("/노미클해제 "):
        name = text.replace("/노미클해제", "", 1).strip()
        changed = set_nomicl(name, 0)
        reply(event.reply_token, f"노미클 해제 완료: {changed}명")
        return

    # =========================
    # 초기화 명령어
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
        reply(
            event.reply_token,
            "⚠️ 전체 마디수를 삭제하려면 아래처럼 입력하세요.\n\n"
            "/전체초기화 확인"
        )
        return

    if text == "/전체초기화 확인":
        deleted = reset_all_counts()

        reply(
            event.reply_token,
            f"🧹 전체 마디수 초기화 완료\n\n"
            f"삭제 데이터: {deleted}개\n"
            f"멤버 목록은 유지됩니다."
        )
        return

    if text == "/멤버초기화":
        reply(
            event.reply_token,
            "⚠️ 전체 멤버 목록을 삭제하려면 아래처럼 입력하세요.\n\n"
            "/멤버초기화 확인\n\n"
            "마디수 데이터는 유지됩니다."
        )
        return

    if text == "/멤버초기화 확인":
        deleted = reset_all_users()

        reply(
            event.reply_token,
            f"👥 전체 멤버 초기화 완료\n\n"
            f"삭제 인원: {deleted}명\n"
            f"마디수 데이터는 유지됩니다."
        )
        return

    if text == "/완전초기화":
        reply(
            event.reply_token,
            "⚠️ 멤버와 마디수를 전부 삭제하려면 아래처럼 입력하세요.\n\n"
            "/완전초기화 확인"
        )
        return

    if text == "/완전초기화 확인":
        deleted_users, deleted_counts = reset_everything()

        reply(
            event.reply_token,
            f"🔥 완전 초기화 완료\n\n"
            f"삭제 멤버: {deleted_users}명\n"
            f"삭제 마디수 데이터: {deleted_counts}개"
        )
        return

    if text.startswith("/닉삭제"):
        keyword = text.replace("/닉삭제", "", 1).strip()

        if not keyword:
            reply(
                event.reply_token,
                "사용법\n\n/닉삭제 닉네임"
            )
            return

        deleted_users, deleted_counts, deleted_names = delete_user_by_name(keyword)

        if deleted_users == 0:
            reply(
                event.reply_token,
                f"삭제 대상이 없습니다.\n\n검색어: {keyword}"
            )
            return

        names_text = "\n".join([f"- {name}" for name in deleted_names])

        reply(
            event.reply_token,
            f"❌ 닉네임 삭제 완료\n\n"
            f"검색어: {keyword}\n"
            f"삭제 인원: {deleted_users}명\n"
            f"삭제 마디수 데이터: {deleted_counts}개\n\n"
            f"삭제된 닉네임:\n{names_text}"
        )
        return


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
