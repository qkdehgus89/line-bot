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

# =========================
# ENV
# =========================
TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

COUNT_SOURCE_ID = os.getenv("COUNT_SOURCE_ID", "").strip()
ADMIN_SOURCE_ID = os.getenv("ADMIN_SOURCE_ID", "").strip()

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
CURRENCY_NAME = os.getenv("CURRENCY_NAME", "토큰").strip()

if not TOKEN or not SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET 값을 설정해야 합니다.")

KST = timezone(timedelta(hours=9))

app = Flask(__name__)
handler = WebhookHandler(SECRET)
config = Configuration(access_token=TOKEN)


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
    if ADMIN_SOURCE_ID:
        ids.add(ADMIN_SOURCE_ID)
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

    # 기존 DB 마이그레이션: 예전 버전 DB를 새 코드에 맞게 자동 보정
    cur.execute("PRAGMA table_info(users)")
    user_cols = {row["name"] for row in cur.fetchall()}

    for col, col_type, default_value in [
        ("gender", "TEXT", "'unknown'"),
        ("is_nomicl", "INTEGER", "0"),
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

    cur.execute("UPDATE counts SET user_name = ? WHERE user_id = ?", (user_name, user_id))

    conn.commit()
    conn.close()


def find_user(keyword):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_id, user_name
    FROM users
    WHERE user_name LIKE ?
    ORDER BY updated_at DESC
    LIMIT 1
    """, (f"%{keyword}%",))
    row = cur.fetchone()
    conn.close()
    return row


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
    WHERE u.last_seen_source_id = ?
       OR c.user_id IS NOT NULL
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
       OR c.user_id IS NOT NULL
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
            f"보유: {balance}{CURRENCY_NAME}\n"
            f"필요: {item['price']}{CURRENCY_NAME}"
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
        f"차감: {item['price']}{CURRENCY_NAME}\n"
        f"잔액: {new_balance}{CURRENCY_NAME}\n\n"
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

    cur.execute("""
    INSERT INTO currency (user_id, balance, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET
        balance = balance + excluded.balance,
        updated_at = excluded.updated_at
    """, (purchase["user_id"], purchase["price"], now_str()))

    cur.execute("""
    INSERT INTO currency_logs (
        user_id, user_name, amount, reason,
        staff_user_id, staff_user_name, created_at
    )
    VALUES (?, ?, ?, ?, NULL, ?, ?)
    """, (
        purchase["user_id"],
        purchase["user_name"],
        purchase["price"],
        f"구매 취소 환불: {purchase['item_name']}",
        staff_user_name,
        now_str()
    ))

    conn.commit()
    conn.close()
    return True, "구매 취소 및 환불 처리했습니다."


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


def delete_user_by_name(keyword):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT user_id, user_name FROM users WHERE user_name LIKE ?", (f"%{keyword}%",))
    users = cur.fetchall()

    deleted_users = 0
    deleted_counts = 0
    deleted_names = []

    for user in users:
        cur.execute("DELETE FROM counts WHERE user_id = ?", (user["user_id"],))
        deleted_counts += cur.rowcount
        cur.execute("DELETE FROM currency WHERE user_id = ?", (user["user_id"],))
        cur.execute("DELETE FROM currency_logs WHERE user_id = ?", (user["user_id"],))
        cur.execute("DELETE FROM purchases WHERE user_id = ?", (user["user_id"],))
        cur.execute("DELETE FROM users WHERE user_id = ?", (user["user_id"],))
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
    lines = [title, f"날짜: {date_str}", ""]
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
    lines = [title, ""]
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

    # 메인방 + 운영진방 둘 다 마디수 카운트
    if source_id in count_source_ids() and user_id:
        add_count(date_str, source_id, user_id, user_name)

    if not isinstance(event.message, TextMessageContent):
        return

    text = (event.message.text or "").strip()

    # =========================
    # 누구나 사용 가능한 명령어
    # =========================
    if text == "/방정보":
        reply(
            event.reply_token,
            f"방정보\n\n"
            f"SOURCE_ID:\n{source_id}\n\n"
            f"USER_ID:\n{user_id}\n\n"
            f"닉네임:\n{user_name}"
        )
        return

    if text == "/잔액":
        balance = get_balance(user_id)
        reply(event.reply_token, f"💰 {user_name}님의 보유 {CURRENCY_NAME}\n\n{balance}{CURRENCY_NAME}")
        return

    if text == "/상점":
        rows = list_shop_items()
        if not rows:
            reply(event.reply_token, "상점에 등록된 상품이 없습니다.")
            return

        lines = ["🛒 상점 목록", ""]
        for i, row in enumerate(rows, 1):
            desc = f"\n   {row['description']}" if row["description"] else ""
            lines.append(f"{i}. {row['name']} - {row['price']}{CURRENCY_NAME}{desc}")

        lines.append("")
        lines.append("구매 방법: /구매 상품명")
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
                f"#{row['id']} {row['item_name']} / {row['price']}{CURRENCY_NAME}\n"
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
    if source_id != ADMIN_SOURCE_ID:
        return

    # 관리자/운영자 아니면 무시
    if not is_staff(user_id):
        return

    # =========================
    # 도움말
    # =========================
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
            "/관리진마디수\n"
            "/관리진마디수 YYYY-MM-DD\n"
            "/관리진순위\n"
            "/관리진순위 YYYY-MM-DD\n"
            "/방정보\n\n"
            "성별 설정\n"
            "/남자 닉네임\n"
            "/여자 닉네임\n"
            "/성별해제 닉네임\n\n"
            "노미클 설정\n"
            "/노미클 닉네임\n"
            "/노미클해제 닉네임\n\n"
            "화폐\n"
            "/지급 닉네임 금액 사유\n"
            "/차감 닉네임 금액 사유\n"
            "/잔액\n"
            "/잔액 닉네임\n"
            "/화폐순위\n"
            "/화폐내역 닉네임\n\n"
            "상점\n"
            "/상품등록 상품명 가격 설명\n"
            "/상품삭제 상품명\n"
            "/상점\n"
            "/구매 상품명\n"
            "/내보유\n"
            "/보유목록\n"
            "/보유목록 닉네임\n"
            "/사용 구매번호\n"
            "/사용처리 구매번호 메모\n"
            "/구매목록\n"
            "/사용목록\n"
            "/구매취소 구매번호\n\n"
            "초기화\n"
            "/초기화 YYYY-MM-DD\n"
            "/전체초기화 확인\n"
            "/멤버초기화 확인\n"
            "/화폐초기화 확인\n"
            "/완전초기화 확인\n"
            "/닉삭제 닉네임\n\n"
            f"기준\n"
            f"남자 {MALE_LIMIT}마디 미만 경고\n"
            f"여자 {FEMALE_LIMIT}마디 미만 경고"
        )
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
    # 성별 / 노미클
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
    # 화폐
    # =========================
    if text.startswith("/지급 "):
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            reply(event.reply_token, f"사용법\n/지급 닉네임 금액 사유")
            return

        keyword = parts[1]
        try:
            amount = int(parts[2])
        except ValueError:
            reply(event.reply_token, "금액은 숫자로 입력해주세요.")
            return

        if amount <= 0:
            reply(event.reply_token, "지급 금액은 1 이상이어야 합니다.")
            return

        reason = parts[3] if len(parts) >= 4 else "운영진 지급"
        target = find_user(keyword)

        if not target:
            reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {keyword}")
            return

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
            f"지급: {amount}{CURRENCY_NAME}\n"
            f"잔액: {balance}{CURRENCY_NAME}\n"
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
            amount = int(parts[2])
        except ValueError:
            reply(event.reply_token, "금액은 숫자로 입력해주세요.")
            return

        if amount <= 0:
            reply(event.reply_token, "차감 금액은 1 이상이어야 합니다.")
            return

        reason = parts[3] if len(parts) >= 4 else "운영진 차감"
        target = find_user(keyword)

        if not target:
            reply(event.reply_token, f"대상을 찾을 수 없습니다.\n검색어: {keyword}")
            return

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
            f"차감: {amount}{CURRENCY_NAME}\n"
            f"잔액: {balance}{CURRENCY_NAME}\n"
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
        reply(event.reply_token, f"💰 {target['user_name']}님의 보유 {CURRENCY_NAME}\n\n{balance}{CURRENCY_NAME}")
        return

    if text == "/화폐순위":
        rows = currency_ranking()
        if not rows:
            reply(event.reply_token, f"{CURRENCY_NAME} 보유 데이터가 없습니다.")
            return

        lines = [f"🏆 {CURRENCY_NAME} 보유 순위", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['user_name']} - {row['balance']}{CURRENCY_NAME}")

        reply(event.reply_token, "\n".join(lines))
        return

    if text.startswith("/화폐내역"):
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
            lines.append(f"{row['created_at']} {sign}{row['amount']} / {row['reason']}{staff}")

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
            price = int(parts[2])
        except ValueError:
            reply(event.reply_token, "가격은 숫자로 입력해주세요.")
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
            f"가격: {price}{CURRENCY_NAME}\n"
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
                f"{row['price']}{CURRENCY_NAME}\n"
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
                f"{row['price']}{CURRENCY_NAME}\n"
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

    if text.startswith("/닉삭제"):
        keyword = text.replace("/닉삭제", "", 1).strip()

        if not keyword:
            reply(event.reply_token, "사용법\n\n/닉삭제 닉네임")
            return

        deleted_users, deleted_counts, deleted_names = delete_user_by_name(keyword)

        if deleted_users == 0:
            reply(event.reply_token, f"삭제 대상이 없습니다.\n\n검색어: {keyword}")
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
