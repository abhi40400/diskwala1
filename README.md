# Diskwala Telegram downloader bot

This bot accepts a Diskwala share link in a Telegram message, asks your Diskwala API to resolve a direct download URL, downloads the first returned file, and uploads it back to Telegram. It keeps both the Telegram token and Diskwala key in `.env`, not source code.

Use it only for links and material you are permitted to download and share. The direct link returned by an API may be short-lived, so the bot processes it immediately and removes its temporary local copy after upload.

## What you need

- Python 3.10 or newer (Python 3.12 recommended)
- Your `TELEGRAM_BOT_TOKEN` from [@BotFather](https://t.me/BotFather)
- A Diskwala extraction API key and its **exact** endpoint/authentication details
- Enough temporary disk space for one video per active chat

The code supports the two common Diskwala API contracts:

| API contract | `.env` values |
| --- | --- |
| `POST` JSON body `{"url":"..."}` and `Authorization: Bearer KEY` | `DISKWALA_REQUEST_METHOD=POST`, `DISKWALA_AUTH_HEADER=Authorization`, `DISKWALA_AUTH_PREFIX=Bearer` |
| `POST` JSON body `{"url":"..."}` and `X-API-Key: KEY` | `DISKWALA_REQUEST_METHOD=POST`, `DISKWALA_AUTH_HEADER=X-API-Key`, `DISKWALA_AUTH_PREFIX=` |
| `GET ?url=...` and Bearer token | `DISKWALA_REQUEST_METHOD=GET`, `DISKWALA_AUTH_HEADER=Authorization`, `DISKWALA_AUTH_PREFIX=Bearer` |

For the endpoint itself, copy it exactly from your provider dashboard. Do not guess the domain from the Diskwala share link. The sample endpoint in `.env.example` is only a commonly documented format, not a universal Diskwala endpoint.

## Windows setup

In PowerShell, in this folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python bot.py
```

Put your real `TELEGRAM_BOT_TOKEN`, `DISKWALA_API_URL`, and `DISKWALA_API_KEY` into `.env`. Change the request method and authentication lines only if your Diskwala API documentation calls for the other format. Keep `.env` private.

Then open your bot in Telegram, press **Start**, and send a full `https://diskwala.com/...` link. It automatically handles the first supported link in the message.

## Linux setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

## Docker setup

Create `.env` as above, then run:

```bash
docker build -t diskwala-telegram-bot .
docker run -d --name diskwala-telegram-bot --restart unless-stopped --env-file .env diskwala-telegram-bot
```

For production, keep the bot on a server with persistent monitoring and enough free disk. Long polling is used, so no domain, HTTPS certificate, or webhook is required.

## Important Telegram limit

On Telegram's hosted Bot API, bots currently upload files up to 50 MB; this starter sets a conservative 49 MB limit. Above the limit it returns the direct link if `SEND_DIRECT_LINK_ON_LARGE=true`. If you need uploads up to 2 GB, deploy Telegram's official local Bot API server and adjust the bot's Telegram base URL separately; do not merely increase `MAX_UPLOAD_MB` on the hosted API.

## BotFather and group notes

- In a private chat, press **Start** before sending links.
- For group use, add the bot to the group. If it should see normal messages (not only commands), disable privacy mode with BotFather's `/setprivacy` command, or make it an administrator if appropriate.
- Never send the bot token or Diskwala API key in a Telegram group or commit them to Git.

## Troubleshooting

- **Authentication failed:** verify `DISKWALA_AUTH_HEADER` and `DISKWALA_AUTH_PREFIX` against your API provider's docs.
- **Unsupported response/direct URL missing:** compare the provider's real JSON response with `first_file_object()` and `DIRECT_URL_FIELDS` in `bot.py`; add the direct-URL field name used by that provider.
- **Only one API file is returned:** this starter intentionally downloads the first file of a playlist. Add an inline choice menu before production playlist support.
- **Video is too large:** use the direct link reply, reduce quality at the API source, or use a local Telegram Bot API server.
- **No response in a group:** check BotFather privacy mode and whether the source domain is included in `ALLOWED_DOMAINS`.
