import argparse
import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

load_dotenv()

parser = argparse.ArgumentParser(description="Steam Deck Refurbished stock watcher")
parser.add_argument("--interval", type=int, help="Check interval in seconds (overrides .env)")
parser.add_argument("--verbose", action="store_true", help="Send Telegram message on every check, not just when in stock")
parser.add_argument("--debug", action="store_true", help="Send the in-stock alert every 10s regardless of actual stock status")
args = parser.parse_args()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = args.interval or int(os.getenv("CHECK_INTERVAL_SECONDS", 300))
URL = "https://store.steampowered.com/sale/steamdeckrefurbished/"

# Selectors that indicate the item is purchasable
ADD_TO_CART_SELECTORS = [
    "text=Add to Cart",
    "text=Add to cart",
    "[class*='addtocart']",
    "input[value='Add to Cart']",
]

# Event that wakes up the main loop early for an immediate check
check_now = threading.Event()
manual_check = threading.Event()  # tracks if the check was user-triggered


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[telegram] Alert sent.")
    except Exception as e:
        print(f"[telegram] Failed to send message: {e}")


def poll_commands():
    """Background thread: listens for /check commands from Telegram."""
    last_update_id = 0
    print("[commands] Listening for Telegram commands...")

    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 30},
                timeout=35,
            )
            for update in resp.json().get("result", []):
                last_update_id = update["update_id"]
                message = update.get("message", {})
                chat = message.get("chat", {})
                sender_id = str(chat.get("id", ""))
                chat_type = chat.get("type", "unknown")
                text = message.get("text", "").strip()
                authorized = sender_id == TELEGRAM_CHAT_ID
                logging.info("incoming chat_id=%s type=%s text=%r %s", sender_id, chat_type, text, "✅" if authorized else "⛔")
                if not authorized:
                    logging.warning("ignored message from unauthorized chat_id=%s", sender_id)
                    continue
                if text.lower() == "/check":
                    print("[commands] /check received — triggering immediate check.")
                    send_telegram("Got it! Checking now...")
                    manual_check.set()
                    check_now.set()
        except Exception as e:
            print(f"[commands] Poll error: {e}")
            time.sleep(5)


def check_availability(page) -> bool:
    print(f"[sniffer] Fetching {URL} ...")
    response = page.goto(URL, wait_until="networkidle", timeout=60_000)

    # Check HTTP status
    if response and response.status == 404:
        raise RuntimeError(f"Page returned 404 — URL may have changed.")
    if response and response.status >= 400:
        raise RuntimeError(f"Page returned HTTP {response.status}.")

    # Detect Cloudflare block / CAPTCHA
    content = page.content().lower()
    if "just a moment" in content or "cf-browser-verification" in content or "enable javascript" in content:
        raise RuntimeError("Blocked by Cloudflare — bot detection triggered.")

    # Steam age-gate: dismiss if present
    try:
        page.click("text=I am over 18", timeout=3_000)
        page.wait_for_load_state("networkidle", timeout=10_000)
        content = page.content().lower()
    except Exception:
        pass  # No age gate, carry on

    # Check for "Add to Cart" button
    for selector in ADD_TO_CART_SELECTORS:
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=3_000):
                print(f"[sniffer] AVAILABLE — matched selector: {selector!r}")
                return True
        except Exception:
            continue

    # Fallback: check page text for stock-related phrases
    if "add to cart" in content:
        print("[sniffer] AVAILABLE — 'add to cart' found in page source.")
        return True

    print("[sniffer] Out of stock — no purchase option found.")
    return False


def main():
    print(f"[sniffer] Starting Steam Deck Refurbished watcher (interval: {CHECK_INTERVAL}s)")
    send_telegram("SniffDeck is online. Watching for Steam Deck Refurbished stock...\nSend /check to trigger an immediate check.")

    # Start command listener in background
    t = threading.Thread(target=poll_commands, daemon=True)
    t.start()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        already_notified = False
        last_error = None  # track last error to avoid spamming repeats

        while True:
            try:
                if args.debug:
                    send_telegram(
                        "*[DEBUG] Steam Deck Refurbished is IN STOCK!*\n"
                        f"[Buy now →]({URL})"
                    )
                    print(f"[debug] Sent test alert. Next in 10s ...\n")
                    time.sleep(10)
                    continue

                available = check_availability(page)
                if last_error is not None:
                    send_telegram("✅ SniffDeck recovered — back to watching.")
                last_error = None  # clear error state on successful check

                is_manual = manual_check.is_set()
                manual_check.clear()

                checked_at = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%y-%m-%d %Hh%M'%S\"")

                if available and not already_notified:
                    send_telegram(
                        "*Steam Deck Refurbished is IN STOCK!*\n"
                        f"[Buy now →]({URL})\n"
                        f"_Checked: {checked_at}_"
                    )
                    already_notified = True
                elif not available:
                    if is_manual or args.verbose:
                        send_telegram(
                            f"Still out of stock. I'll keep watching.\n"
                            f"_Checked: {checked_at}_"
                        )
                    # Reset so we alert again if it comes back in stock
                    already_notified = False

            except Exception as e:
                err_msg = str(e)
                logging.error("[sniffer] Error during check: %s", err_msg)
                if err_msg != last_error:
                    send_telegram(f"⚠️ *SniffDeck error:*\n`{err_msg}`\nI'll keep retrying.")
                    last_error = err_msg
                else:
                    logging.info("[sniffer] Same error as last check, skipping repeat alert.")
                # Reload the page context on errors
                try:
                    page = context.new_page()
                except Exception:
                    pass

            # Wait for interval OR an early /check trigger
            triggered = check_now.wait(timeout=CHECK_INTERVAL)
            if triggered:
                check_now.clear()
                print(f"[sniffer] Early check triggered by /check command.\n")
            else:
                print(f"[sniffer] Next check in {CHECK_INTERVAL}s ...\n")


if __name__ == "__main__":
    main()
