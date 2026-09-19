"""Telegram bot that turns a Diskwala share link into a Telegram upload.

Keep secrets in .env. This file is safe to commit; .env is deliberately ignored.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters


URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
DIRECT_URL_FIELDS = ("direct_link", "download_url", "downloadUrl", "direct_url", "download", "url", "link")
NAME_FIELDS = ("file_name", "filename", "name", "title")
LIST_FIELDS = ("files", "results", "items", "videos")
OBJECT_FIELDS = ("file", "result", "video")


class UserVisibleError(Exception):
    """An expected problem that should be described to the Telegram user."""


class FileTooLargeError(UserVisibleError):
    def __init__(self, direct_url: str, size: int | None, limit: int) -> None:
        self.direct_url = direct_url
        self.size = size
        self.limit = limit


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    diskwala_api_url: str
    diskwala_api_key: str
    request_method: str
    auth_header: str
    auth_prefix: str
    allowed_domains: frozenset[str]
    max_upload_bytes: int
    send_direct_link_on_large: bool


@dataclass(frozen=True)
class ResolvedFile:
    direct_url: str
    filename: str
    size_bytes: int | None


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    load_dotenv()
    method = os.getenv("DISKWALA_REQUEST_METHOD", "POST").strip().upper()
    if method not in {"GET", "POST"}:
        raise RuntimeError("DISKWALA_REQUEST_METHOD must be GET or POST")

    domains = frozenset(
        domain.strip().lower().removeprefix(".")
        for domain in os.getenv("ALLOWED_DOMAINS", "diskwala.com,www.diskwala.com").split(",")
        if domain.strip()
    )
    if not domains:
        raise RuntimeError("ALLOWED_DOMAINS cannot be empty")

    max_mb = int(os.getenv("MAX_UPLOAD_MB", "49"))
    if max_mb < 1:
        raise RuntimeError("MAX_UPLOAD_MB must be at least 1")

    return Settings(
        telegram_bot_token=required("8775966627:AAEfXXbyjSJv-ADUpCEji3At6CzsFn7eDAM"),
        diskwala_api_url=required("https://api.diskwala.com"),
        diskwala_api_key=required("6aae527617fa9fcfda4875b6"),
        request_method=method,
        auth_header=os.getenv("DISKWALA_AUTH_HEADER", "Authorization").strip(),
        auth_prefix=os.getenv("DISKWALA_AUTH_PREFIX", "Bearer").strip(),
        allowed_domains=domains,
        max_upload_bytes=max_mb * 1024 * 1024,
        send_direct_link_on_large=read_bool("SEND_DIRECT_LINK_ON_LARGE", True),
    )


def get_source_url(text: str, allowed_domains: frozenset[str]) -> str | None:
    for match in URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,!?;:)")
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme in {"http", "https"} and host in allowed_domains:
            return candidate
    return None


def is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def first_file_object(payload: Any) -> dict[str, Any]:
    """Normalize the common Diskwala API response shapes to one file dictionary."""
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return payload[0]
        raise UserVisibleError("The API returned an empty file list.")

    if not isinstance(payload, dict):
        raise UserVisibleError("The API returned an unsupported response.")

    current: dict[str, Any] = payload
    # Most APIs wrap the result in {data: ...}; unwrap it first.
    data = current.get("data")
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            return data[0]
        raise UserVisibleError("The API returned an empty file list.")
    if isinstance(data, dict):
        current = data

    for field in LIST_FIELDS:
        values = current.get(field)
        if isinstance(values, list):
            if values and isinstance(values[0], dict):
                return values[0]
            raise UserVisibleError("The API returned an empty file list.")
    for field in OBJECT_FIELDS:
        value = current.get(field)
        if isinstance(value, dict):
            return value
    return current


def read_size(file_data: dict[str, Any]) -> int | None:
    for field in ("sizebytes", "size_bytes", "file_size", "filesize", "size"):
        value = file_data.get(field)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def safe_filename(value: str | None, fallback_url: str) -> str:
    if value:
        # API-controlled names must not become a path outside our temporary folder.
        name = re.split(r"[\\/]", value)[-1].strip()
        if name:
            return name[:180]
    path_name = Path(urlparse(fallback_url).path).name
    return path_name if path_name else "diskwala-video.mp4"


async def resolve_diskwala_url(
    client: httpx.AsyncClient, settings: Settings, source_url: str
) -> ResolvedFile:
    headers = {"Accept": "application/json"}
    key_value = f"{settings.auth_prefix} {settings.diskwala_api_key}".strip()
    if settings.auth_header:
        headers[settings.auth_header] = key_value

    try:
        if settings.request_method == "GET":
            response = await client.get(settings.diskwala_api_url, params={"url": source_url}, headers=headers)
        else:
            response = await client.post(settings.diskwala_api_url, json={"url": source_url}, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {401, 403}:
            raise UserVisibleError("Diskwala API authentication failed. Check the API key and auth settings.") from error
        if error.response.status_code == 429:
            raise UserVisibleError("The Diskwala API rate limit was reached. Please try again shortly.") from error
        raise UserVisibleError(f"Diskwala API returned HTTP {error.response.status_code}.") from error
    except httpx.RequestError as error:
        raise UserVisibleError("Could not reach the Diskwala API. Check DISKWALA_API_URL and the server connection.") from error

    try:
        payload = response.json()
    except ValueError as error:
        raise UserVisibleError("The Diskwala API did not return JSON. Check the endpoint URL.") from error

    file_data = first_file_object(payload)
    direct_url = next((file_data.get(key) for key in DIRECT_URL_FIELDS if is_http_url(file_data.get(key))), None)
    if not direct_url:
        raise UserVisibleError("The API response did not include a usable direct download URL.")
    name = next((str(file_data[key]) for key in NAME_FIELDS if file_data.get(key)), None)
    return ResolvedFile(direct_url=direct_url, filename=safe_filename(name, direct_url), size_bytes=read_size(file_data))


async def download_limited(
    client: httpx.AsyncClient, resolved: ResolvedFile, limit: int
) -> Path:
    if resolved.size_bytes is not None and resolved.size_bytes > limit:
        raise FileTooLargeError(resolved.direct_url, resolved.size_bytes, limit)

    suffix = Path(resolved.filename).suffix or ".mp4"
    descriptor, temporary_name = tempfile.mkstemp(prefix="diskwala-", suffix=suffix)
    os.close(descriptor)
    path = Path(temporary_name)
    written = 0
    try:
        async with client.stream("GET", resolved.direct_url, follow_redirects=True) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > limit:
                raise FileTooLargeError(resolved.direct_url, int(content_length), limit)
            with path.open("wb") as temporary:
                async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise FileTooLargeError(resolved.direct_url, written, limit)
                    temporary.write(chunk)
        if written == 0:
            raise UserVisibleError("The direct download URL returned an empty file.")
        return path
    except FileTooLargeError:
        path.unlink(missing_ok=True)
        raise
    except (OSError, httpx.HTTPError) as error:
        path.unlink(missing_ok=True)
        raise UserVisibleError("The video could not be downloaded from the direct link.") from error


def readable_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    return f"{size / (1024 * 1024):.1f} MB"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "Send me a full Diskwala share link and I will fetch the video through the configured API.\n\n"
            "Only send content you are allowed to download and share."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        domains = ", ".join(sorted(context.bot_data["settings"].allowed_domains))
        await message.reply_text(
            f"Paste one Diskwala link from: {domains}\n"
            "The bot handles the first supported link in your message."
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    settings: Settings = context.bot_data["settings"]
    source_url = get_source_url(message.text, settings.allowed_domains)
    if source_url is None:
        return

    # One download at a time per chat prevents duplicated long jobs from one user/group.
    locks: dict[int, asyncio.Lock] = context.application.bot_data["chat_locks"]
    lock = locks.setdefault(message.chat_id, asyncio.Lock())
    if lock.locked():
        await message.reply_text("A download is already running in this chat. Please wait for it to finish.")
        return

    async with lock:
        status = await message.reply_text("Resolving your Diskwala link…")
        local_file: Path | None = None
        try:
            client: httpx.AsyncClient = context.application.bot_data["http_client"]
            resolved = await resolve_diskwala_url(client, settings, source_url)
            await status.edit_text("Downloading the video…")
            local_file = await download_limited(client, resolved, settings.max_upload_bytes)
            await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)
            await status.edit_text("Uploading to Telegram…")

            is_video = Path(resolved.filename).suffix.lower() in VIDEO_EXTENSIONS
            caption = resolved.filename[:900]
            with local_file.open("rb") as video_file:
                if is_video:
                    await message.reply_video(
                        video=video_file,
                        filename=resolved.filename,
                        caption=caption,
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                    )
                else:
                    await message.reply_document(
                        document=video_file,
                        filename=resolved.filename,
                        caption=caption,
                        read_timeout=120,
                        write_timeout=120,
                    )
            await status.delete()
        except FileTooLargeError as error:
            limit_mb = error.limit // (1024 * 1024)
            text = (
                f"This file is {readable_size(error.size)}, above this bot's {limit_mb} MB upload limit."
            )
            if settings.send_direct_link_on_large:
                text += f"\n\nDirect download link (may expire):\n{error.direct_url}"
            await status.edit_text(text, disable_web_page_preview=True)
        except UserVisibleError as error:
            await status.edit_text(str(error))
        except TelegramError:
            logging.exception("Telegram API error while processing chat %s", message.chat_id)
            await status.edit_text("Telegram could not accept the upload. Try a smaller file or try again later.")
        except Exception:
            logging.exception("Unexpected error while processing chat %s", message.chat_id)
            await status.edit_text("Unexpected server error. Please try again later.")
        finally:
            if local_file:
                local_file.unlink(missing_ok=True)


async def post_init(application: Application) -> None:
    application.bot_data["http_client"] = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=20, read=180, write=30, pool=20),
        follow_redirects=True,
        headers={"User-Agent": "diskwala-telegram-bot/1.0"},
    )
    await application.bot.set_my_commands([
        ("start", "Show how to use the bot"),
        ("help", "Show supported link domains"),
    ])


async def post_shutdown(application: Application) -> None:
    client: httpx.AsyncClient | None = application.bot_data.get("http_client")
    if client:
        await client.aclose()


def main() -> None:
    settings = load_settings()
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(4)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["chat_locks"] = {}
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    main()
