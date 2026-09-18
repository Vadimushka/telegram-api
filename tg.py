#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["telethon>=1.36"]
# ///
"""Работа с Telegram через MTProto (свой аккаунт).

    uv run tg.py login
    uv run tg.py chats -n 30
    uv run tg.py search php
    uv run tg.py media <чат> -n 50
    uv run tg.py get <чат> <msg_id>
    uv run tg.py probe downloads/файл.ogg
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from telethon import TelegramClient, functions, utils
from telethon.tl.types import Channel, DocumentAttributeAudio, DocumentAttributeFilename

ROOT = Path(__file__).parent
SESSION = ROOT / "sessions" / "personal"
DOWNLOADS = ROOT / "downloads"


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Нет TG_API_ID / TG_API_HASH — заполни .env (см. .env.example)")
    return int(api_id), api_hash


def client():
    api_id, api_hash = load_env()
    return TelegramClient(str(SESSION), api_id, api_hash)


def peer(arg):
    """Строка из CLI -> id чата или username."""
    s = arg.lstrip("@")
    return int(s) if s.lstrip("-").isdigit() else s


def chat_kind(entity):
    """Супергруппа - тоже Channel, отличается флагом broadcast."""
    if isinstance(entity, Channel):
        return "канал" if entity.broadcast else "группа"
    return "личка" if getattr(entity, "first_name", None) is not None else "группа"


def audio_attr(doc):
    for a in doc.attributes:
        if isinstance(a, DocumentAttributeAudio):
            return a
    return None


def file_name(doc):
    for a in doc.attributes:
        if isinstance(a, DocumentAttributeFilename):
            return a.file_name
    return None


async def cmd_login(args):
    async with client() as c:
        me = await c.get_me()
        print(f"Авторизован: {me.first_name} @{me.username} (id {me.id})")
        print(f"Сессия: {SESSION}.session")


async def cmd_chats(args):
    async with client() as c:
        async for d in c.iter_dialogs(limit=args.limit):
            kind = chat_kind(d.entity)
            uname = getattr(d.entity, "username", None)
            print(f"{d.id:>15}  {kind:<6}  {d.name}" + (f"  @{uname}" if uname else ""))


async def cmd_search(args):
    async with client() as c:
        res = await c(functions.contacts.SearchRequest(q=args.query, limit=args.limit))
        for e in list(res.chats) + list(res.users):
            uname = getattr(e, "username", None)
            print(f"{utils.get_peer_id(e):>15}  {chat_kind(e):<6}  "
                  f"{'@' + uname if uname else '':<24}  {utils.get_display_name(e)}")


async def cmd_media(args):
    async with client() as c:
        async for m in c.iter_messages(peer(args.chat), limit=args.limit):
            doc = m.document
            if not doc:
                continue
            a = audio_attr(doc)
            if not a:
                continue
            kind = "VOICE" if a.voice else "audio"
            tags = " ".join(filter(None, [a.title, a.performer, file_name(doc)]))
            print(
                f"{m.id:>8}  {m.date:%Y-%m-%d %H:%M}  {kind:<5}  "
                f"{a.duration:>4}s  {doc.size/1024:>7.0f}K  {doc.mime_type:<16}  "
                f"{'waveform' if a.waveform else 'no-wf':<8}  {tags}"
            )


async def cmd_get(args):
    async with client() as c:
        m = await c.get_messages(peer(args.chat), ids=args.msg_id)
        if not m or not m.media:
            sys.exit("В сообщении нет медиа")
        DOWNLOADS.mkdir(exist_ok=True)
        path = await c.download_media(m, file=str(DOWNLOADS))
        print(f"Скачано: {path}")
        if m.document and audio_attr(m.document):
            a = audio_attr(m.document)
            print(f"Флаг API: {'voice=True (голосовое)' if a.voice else 'voice=False (аудиофайл)'}")
            probe(Path(path))


def probe(path: Path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode:
        print(out.stderr.strip())
        return
    data = json.loads(out.stdout)
    fmt = data.get("format", {})
    st = (data.get("streams") or [{}])[0]
    tags = {**fmt.get("tags", {}), **st.get("tags", {})}
    channels = st.get("channels")
    bitrate = int(fmt.get("bit_rate") or st.get("bit_rate") or 0) // 1000

    print(f"\n{path.name}")
    print(f"  кодек     {st.get('codec_name')}  {st.get('sample_rate')} Гц  "
          f"{'моно' if channels == 1 else 'стерео' if channels == 2 else channels}")
    print(f"  битрейт   {bitrate} кбит/с   длительность {float(fmt.get('duration', 0)):.1f}s")
    if tags:
        print("  теги      " + ", ".join(f"{k}={v}" for k, v in tags.items()))

    flags = []
    enc = " ".join(str(v) for v in tags.values()).lower()
    if "lavf" in enc or "lavc" in enc or "ffmpeg" in enc:
        flags.append("теги ffmpeg (Lavf/Lavc) — файл перекодирован, а не записан в клиенте")
    if channels and channels > 1:
        flags.append("стерео — клиент Telegram пишет голос в моно")
    if bitrate > 64:
        flags.append(f"битрейт {bitrate} кбит/с — заметно выше типичных ~27-32 для голосового")
    if st.get("codec_name") not in (None, "opus"):
        flags.append(f"кодек {st.get('codec_name')}, а не opus")
    if any(k.lower() in ("title", "artist", "album") for k in tags):
        flags.append("музыкальные теги (title/artist) — исходно это трек")

    print("  вывод     " + ("похоже на честную запись с микрофона" if not flags else ""))
    for f in flags:
        print(f"            ! {f}")
    if flags:
        print("            (косвенные признаки, любой из них подделывается)")


def main():
    p = argparse.ArgumentParser(description="Telegram MTProto CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="авторизация, создаёт файл сессии")

    c = sub.add_parser("chats", help="список диалогов с id")
    c.add_argument("-n", "--limit", type=int, default=30)

    c = sub.add_parser("search", help="поиск публичных каналов и групп")
    c.add_argument("query")
    c.add_argument("-n", "--limit", type=int, default=20)

    c = sub.add_parser("media", help="аудио и голосовые в чате")
    c.add_argument("chat")
    c.add_argument("-n", "--limit", type=int, default=50)

    c = sub.add_parser("get", help="скачать медиа сообщения и разобрать")
    c.add_argument("chat")
    c.add_argument("msg_id", type=int)

    c = sub.add_parser("probe", help="разбор уже скачанного файла")
    c.add_argument("path", type=Path)

    args = p.parse_args()
    if args.cmd == "probe":
        probe(args.path)
    else:
        asyncio.run(globals()[f"cmd_{args.cmd}"](args))


if __name__ == "__main__":
    main()
