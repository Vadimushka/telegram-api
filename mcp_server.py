#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["telethon>=1.36", "python-socks[asyncio]", "qrcode", "mcp>=2"]
# ///
"""MCP-сервер поверх tg.py: искать чаты, читать и отправлять сообщения со своего аккаунта.

Регистрация в Claude Code для всех проектов:

    claude mcp add --scope user telegram -- uv run --script <путь>/mcp_server.py

Сессия та же, что у CLI (sessions/personal.session), так что сначала
`uv run tg.py login`. Клиент открывается на каждый вызов и сразу закрывается:
sqlite-сессия держит блокировку, пока клиент подключён, и иначе CLI
параллельно не запустится.
"""
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from telethon import utils

import tg

mcp = MCPServer("telegram")

SIGNATURE = "— отправлено ИИ-ассистентом Claude (Anthropic)"


def _signed(text: str, sign: bool) -> str:
    return f"{text}\n\n{SIGNATURE}" if sign else text


def _client():
    try:
        return tg.client()
    except SystemExit as err:  # load_env выходит через sys.exit, серверу так нельзя
        raise ToolError(str(err)) from None


def _chat_info(entity, d=None):
    info = {
        "id": utils.get_peer_id(entity),
        "name": d.name if d else utils.get_display_name(entity),
        "kind": tg.chat_kind(entity),
        "username": getattr(entity, "username", None),
    }
    if d:
        info["unread"] = d.unread_count
        info["last_message_at"] = d.date.astimezone().isoformat(timespec="minutes") if d.date else None
    return info


async def _resolve(c, chat):
    try:
        return await tg.find_recipient(c, chat)
    except tg.RecipientError as err:
        if not err.candidates:
            raise ToolError(str(err)) from None
        names = "; ".join(f"{d.name} (id {d.id})" for d in err.candidates)
        raise ToolError(f"{err} {names}") from None


def _media_note(m):
    if not m.media:
        return None
    if m.voice:
        return f"голосовое {m.file.duration}s"
    if m.photo:
        return "фото"
    if m.file:
        return f"файл {m.file.name or ''} {m.file.mime_type or ''}".strip()
    return type(m.media).__name__


@mcp.tool()
async def search_chats(query: str = "", limit: int = 30) -> list[dict]:
    """Найти диалоги (лички, группы, каналы) по подстроке в названии или @username.

    Пустой query - последние диалоги. Имена часто записаны латиницей
    («Костя» может быть «Konstantin A»), поэтому если ничего не нашлось -
    попробуй транслитерацию и полные формы имени.
    """
    q = query.casefold().lstrip("@")
    out = []
    async with _client() as c:
        async for d in c.iter_dialogs():
            uname = getattr(d.entity, "username", None) or ""
            if q and q not in (d.name or "").casefold() and q not in uname.casefold():
                continue
            out.append(_chat_info(d.entity, d))
            if len(out) >= limit:
                break
    return out


@mcp.tool()
async def read_messages(chat: str, limit: int = 20) -> dict:
    """Последние сообщения чата, от новых к старым.

    chat - id из search_chats, @username, +телефон или уникальная часть имени диалога.
    """
    async with _client() as c:
        e = await _resolve(c, chat)
        msgs = []
        async for m in c.iter_messages(e, limit=limit):
            sender = await m.get_sender()
            msgs.append({
                "id": m.id,
                "date": m.date.astimezone().isoformat(timespec="minutes"),
                "from": "я" if m.out else (utils.get_display_name(sender) if sender else None),
                "text": m.text or "",
                "media": _media_note(m),
                "reply_to": m.reply_to_msg_id,
            })
        return {"chat": _chat_info(e), "messages": msgs}


@mcp.tool()
async def send_message(chat: str, text: str, reply_to: int | None = None, sign: bool = True) -> dict:
    """Отправить текстовое сообщение от имени пользователя.

    Сообщение уходит от его личного аккаунта и отменить его нельзя: вызывай
    только после того, как пользователь подтвердил и получателя, и точный текст.
    chat - id, @username, +телефон или уникальная часть имени диалога.
    Подпись Claude дописывается сама, в text её не включать. sign=False -
    только если пользователь явно попросил отправить без подписи.
    """
    if not text.strip():
        raise ToolError("Пустое сообщение")
    async with _client() as c:
        e = await _resolve(c, chat)
        m = await c.send_message(e, _signed(text, sign), reply_to=reply_to)
        return {"sent_to": _chat_info(e), "message_id": m.id}


@mcp.tool()
async def send_file(chat: str, path: str, caption: str = "", sign: bool = True) -> dict:
    """Отправить файл с локального диска (caption - подпись) от имени пользователя.

    Как и send_message: только после подтверждения пользователем получателя и файла.
    Подпись Claude дописывается к caption сама, sign - как в send_message.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ToolError(f"Нет файла {p}")
    async with _client() as c:
        e = await _resolve(c, chat)
        m = await c.send_file(e, str(p), caption=_signed(caption, sign).strip() or None)
        return {"sent_to": _chat_info(e), "message_id": m.id}


if __name__ == "__main__":
    mcp.run()
