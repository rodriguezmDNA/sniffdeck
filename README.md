# SniffDeck

A Telegram bot that watches the [Steam Deck Refurbished](https://store.steampowered.com/sale/steamdeckrefurbished/) page and alerts you the moment it comes back in stock.

## Features

- Headless Chromium scraper (handles JavaScript-rendered pages)
- Instant Telegram alert when stock appears
- `/check` command to trigger an immediate check on demand
- Verbose and debug modes
- Error alerts (404, Cloudflare blocks, timeouts) with auto-recovery notification
- Unauthorized message filtering (only responds to your chat ID)
- Runs as a systemd service — survives reboots and auto-restarts on crash

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps chromium
```

### 2. Create your Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Message [@userinfobot](https://t.me/userinfobot) → copy your chat ID

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your token and chat ID
```

### 4. Run

```bash
# Foreground (for testing)
.venv/bin/python sniffdeck.py

# With options
.venv/bin/python sniffdeck.py --interval 60 --verbose
.venv/bin/python sniffdeck.py --debug
```

## CLI Options

| Flag | Description |
|---|---|
| `--interval N` | Check every N seconds (overrides `.env`) |
| `--verbose` | Send a Telegram message on every check, not just when in stock |
| `--debug` | Send a fake in-stock alert every 10s to test Telegram integration |

## Telegram Commands

| Command | Description |
|---|---|
| `/check` | Trigger an immediate stock check |

## systemd Service

Install and enable as a service so it runs on boot and restarts on crash:

```bash
sudo cp sniffdeck.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sniffdeck
sudo systemctl start sniffdeck
```

Useful commands:

```bash
systemctl status sniffdeck          # check if running
journalctl -u sniffdeck -f          # live logs
systemctl stop sniffdeck            # stop the bot
systemctl disable sniffdeck         # don't start on next reboot
```

## Project Structure

```
sniffdeck/
├── sniffdeck.py        # main bot
├── requirements.txt    # dependencies
├── .env                # secrets (not committed)
├── .env.example        # config template
├── .gitignore
└── sniffdeck.service   # systemd unit file
```
