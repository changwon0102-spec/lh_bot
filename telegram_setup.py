"""Read the bot identity and private chat IDs. Never sends messages or edits .env."""
from pathlib import Path
import sys

import requests
from dotenv import dotenv_values


def main() -> int:
    env_path = Path(__file__).resolve().parent / ".env"
    token = (dotenv_values(env_path).get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("먼저 .env의 TELEGRAM_BOT_TOKEN= 뒤에 BotFather 토큰을 넣고 저장하세요.")
        return 1

    def read(method: str, **params):
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{token}/{method}",
                params=params, timeout=(10, 20), allow_redirects=False,
            )
            if response.status_code != 200:
                hints = {401: "봇 토큰을 확인하세요.", 404: "봇 토큰 형식을 확인하세요.",
                         409: "같은 봇을 사용하는 다른 수신 프로그램을 확인하세요.",
                         429: "잠시 후 다시 실행하세요."}
                raise ValueError(f"Telegram HTTP {response.status_code}: " + hints.get(response.status_code, "연결 상태를 확인하세요."))
            try:
                data = response.json()
            except ValueError:
                raise ValueError("Telegram 응답을 해석하지 못했습니다.") from None
            if data.get("ok") is not True:
                raise ValueError("Telegram이 조회 요청을 거절했습니다.")
            return data["result"]
        except requests.RequestException:
            # Request exception strings may contain the bot token in the URL.
            raise ValueError("Telegram 연결에 실패했습니다. 네트워크를 확인하세요.") from None

    try:
        bot = read("getMe")
        print(f"연결된 봇: @{bot.get('username', '(이름 없음)')}")
        if read("getWebhookInfo").get("url"):
            print("이 봇에 기존 webhook 연결이 있습니다. 해당 연동에서 Chat ID를 확인하세요.")
            return 1
        # No offset/allowed_updates: do not acknowledge or change update subscriptions.
        updates = read("getUpdates", timeout=0, limit=100)
        chats = {}
        for update in updates:
            chat = (update.get("message") or {}).get("chat", {})
            if chat.get("type") == "private":
                chats[chat["id"]] = chat
        if not chats:
            print("개인 대화를 찾지 못했습니다. 위 봇과의 대화에 /start를 다시 보내고 재실행하세요.")
            return 1
        for chat_id, chat in chats.items():
            print(f"개인 대화: {chat.get('first_name', '')} @{chat.get('username', '(아이디 없음)')}")
            print(f"TELEGRAM_CHAT_ID={chat_id}")
        print("본인의 대화에 해당하는 TELEGRAM_CHAT_ID 줄을 .env에 넣고 저장하세요.")
        return 0
    except (ValueError, KeyError, TypeError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else "Telegram 응답 구조를 확인하세요.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
