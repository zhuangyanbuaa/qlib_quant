"""Token-redacted Telegram Bot API with conservative delivery semantics."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TelegramError(RuntimeError):
    def __init__(self, code, *, retry_after=0, ambiguous=False):
        super().__init__(code)
        self.retry_after = retry_after
        self.ambiguous = ambiguous


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https(url, payload, timeout):
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 429:
            try:
                wait = int(json.load(error).get("parameters", {}).get("retry_after", 60))
            except (ValueError, TypeError, AttributeError):
                wait = 60
            raise TelegramError("telegram_rate_limited", retry_after=max(1, wait)) from None
        raise TelegramError(f"telegram_http_{error.code}", ambiguous=error.code >= 500) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise TelegramError("telegram_transport_delivery_unknown", ambiguous=True) from None


class TelegramClient:
    def __init__(self, token, *, timeout=15, transport=None):
        self._token = token.get_secret_value() if hasattr(token, "get_secret_value") else token
        self.timeout = timeout
        self.transport = transport or _https

    def call(self, method, payload=None):
        if method not in {"getMe", "getUpdates", "sendMessage"}:
            raise ValueError("unsupported Telegram operation")
        try:
            response = self.transport(
                f"https://api.telegram.org/bot{self._token}/{method}", payload or {}, self.timeout
            )
        except TelegramError:
            raise
        except Exception:
            raise TelegramError("telegram_transport_delivery_unknown", ambiguous=True) from None
        if not isinstance(response, dict) or response.get("ok") is not True:
            code = response.get("error_code", 0) if isinstance(response, dict) else 0
            if not isinstance(code, int):
                code = 0
            if code == 429:
                try:
                    wait = max(1, int(response.get("parameters", {}).get("retry_after", 60)))
                except (ValueError, TypeError):
                    wait = 60
                raise TelegramError("telegram_rate_limited", retry_after=wait)
            raise TelegramError(f"telegram_rejected_{code}", ambiguous=code == 0 or code >= 500)
        return response.get("result")

    def check(self):
        result = self.call("getMe")
        if not isinstance(result, dict) or result.get("is_bot") is not True:
            raise TelegramError("telegram_invalid_bot_identity")
        return {"status": "VALID_BOT", "username": result.get("username")}

    def private_chats(self):
        updates = self.call(
            "getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]}
        )
        if not isinstance(updates, list):
            raise TelegramError("telegram_invalid_updates")
        chats = set()
        for update in updates:
            if not isinstance(update, dict):
                raise TelegramError("telegram_invalid_updates")
            message = update.get("message", {})
            chat, sender = message.get("chat", {}), message.get("from", {})
            if (
                chat.get("type") == "private"
                and sender.get("is_bot") is False
                and chat.get("id") == sender.get("id")
            ):
                chats.add(str(chat["id"]))
        return sorted(chats)

    def send(self, chat_id, text):
        if not text or len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Telegram text must be 1-4096 UTF-16 units")
        result = self.call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}},
        )
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramError("telegram_delivery_receipt_unknown", ambiguous=True)
        return result["message_id"]
