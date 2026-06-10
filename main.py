# 족보 입력/출력 수정 코드

JOKBO_INPUT_PENDING = set()

def format_jokbo_text(text):
    if not text:
        return "저장된 족보가 없습니다."

    text = text.replace("\\r\\n", "\n")
    text = text.replace("\\n", "\n")
    text = text.replace("\\t", "\t")

    return text


# /족보입력
if text == "/족보입력":
    JOKBO_INPUT_PENDING.add(user_id)

    reply(
        event.reply_token,
        "📒 족보 입력 모드\\n\\n"
        "다음 메시지에 족보 전체를 붙여넣어 주세요.\\n\\n"
        "취소 : /족보취소"
    )
    return


# /족보취소
if text == "/족보취소":
    JOKBO_INPUT_PENDING.discard(user_id)

    reply(
        event.reply_token,
        "❌ 족보 입력이 취소되었습니다."
    )
    return


# 족보 저장 처리
if user_id in JOKBO_INPUT_PENDING:

    JOKBO_INPUT_PENDING.discard(user_id)

    jokbo_text = text

    jokbo_text = jokbo_text.replace("/족보입력", "").strip()
    jokbo_text = jokbo_text.replace("\\r\\n", "\n")
    jokbo_text = jokbo_text.replace("\\n", "\n")

    save_jokbo(jokbo_text)

    reply(
        event.reply_token,
        "✅ 족보 저장 완료"
    )
    return


# /족보
if text == "/족보":

    jokbo_text = load_jokbo()

    reply(
        event.reply_token,
        format_jokbo_text(jokbo_text)
    )
    return
