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

parser = argparse.ArgumentParser(description="Steam Deck stock watcher")
parser.add_argument("--interval", type=int, help="Check interval in seconds (overrides .env)")
parser.add_argument("--verbose", action="store_true", help="Send Telegram message on every check, not just when in stock")
parser.add_argument("--debug", action="store_true", help="Send a fake in-stock alert every 10s to test Telegram integration")
args = parser.parse_args()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = args.interval or int(os.getenv("CHECK_INTERVAL_SECONDS", 300))

TARGETS = [
    {"name": "Steam Deck Refurbished", "url": "https://store.steampowered.com/sale/steamdeckrefurbished/"},
    {"name": "Steam Deck OLED 512GB",  "url": "https://store.steampowered.com/steamdeck", "sku_label": "512GB OLED"},
    {"name": "Steam Deck OLED 1TB",    "url": "https://store.steampowered.com/steamdeck", "sku_label": "1TB OLED"},
]

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


def check_availability(page, url: str, sku_label: str = None) -> bool:
    print(f"[sniffer] Fetching {url} ...")
    response = page.goto(url, wait_until="networkidle", timeout=60_000)

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

    # SKU-specific check: find the reservation_ctn for this model and check its button
    if sku_label:
        containers = page.locator(".reservation_ctn").all()
        for ctn in containers:
            try:
                if sku_label.lower() in ctn.inner_text().lower():
                    disabled = ctn.locator(".Disabled").count()
                    out_of_stock = "out of stock" in ctn.inner_text().lower()
                    available = not disabled and not out_of_stock
                    print(f"[sniffer] {sku_label}: {'AVAILABLE' if available else 'out of stock'}")
                    return available
            except Exception:
                continue
        raise RuntimeError(f"Could not find SKU container for {sku_label!r} — page structure may have changed.")

    # Generic check: look for any "Add to Cart" button
    for selector in ADD_TO_CART_SELECTORS:
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=3_000):
                print(f"[sniffer] AVAILABLE — matched selector: {selector!r}")
                return True
        except Exception:
            continue

    # Fallback: check page text
    if "add to cart" in content:
        print("[sniffer] AVAILABLE — 'add to cart' found in page source.")
        return True

    print("[sniffer] Out of stock — no purchase option found.")
    return False


def main():
    target_names = " & ".join(t["name"] for t in TARGETS)
    print(f"[sniffer] Starting watcher for: {target_names} (interval: {CHECK_INTERVAL}s)")
    send_telegram(
        f"SniffDeck is online. Watching:\n"
        + "\n".join(f"• {t['name']}" for t in TARGETS)
        + "\n\nSend /check to trigger an immediate check."
    )

    # Start command listener in background
    t = threading.Thread(target=poll_commands, daemon=True)
    t.start()

    BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

    # Per-target state (persists across browser restarts)
    state = {t["name"]: {"already_notified": False, "last_error": None} for t in TARGETS}

    with sync_playwright() as p:

        def make_browser():
            b = p.chromium.launch(headless=True, args=BROWSER_ARGS)
            ctx = b.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ))
            logging.info("[browser] New browser context created.")
            return b, ctx

        browser, context = make_browser()

        while True:
            try:
                if args.debug:
                    for target in TARGETS:
                        send_telegram(
                            f"*[DEBUG] {target['name']} is IN STOCK!*\n"
                            f"[Buy now →]({target['url']})"
                        )
                    print(f"[debug] Sent test alerts. Next in 10s ...\n")
                    time.sleep(10)
                    continue

                is_manual = manual_check.is_set()
                manual_check.clear()
                checked_at = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%y-%m-%d %Hh%M'%S\"")
                manual_results = []

                for target in TARGETS:
                    name, url = target["name"], target["url"]
                    s = state[name]
                    page = None
                    try:
                        page = context.new_page()
                        available = check_availability(page, url, target.get("sku_label"))

                        if s["last_error"] is not None:
                            send_telegram(f"✅ *{name}* monitor recovered — back to watching.")
                        s["last_error"] = None

                        if available and not s["already_notified"]:
                            send_telegram(
                                f"*{name} is IN STOCK!*\n"
                                f"[Buy now →]({url})\n"
                                f"_Checked: {checked_at}_"
                            )
                            s["already_notified"] = True
                        elif not available:
                            s["already_notified"] = False
                            if is_manual or args.verbose:
                                manual_results.append(f"• {name}: out of stock")

                    except Exception as e:
                        err_msg = str(e)
                        logging.error("[sniffer] Error checking %s: %s", name, err_msg)
                        if err_msg != s["last_error"]:
                            send_telegram(f"⚠️ *{name} error:*\n`{err_msg}`\nI'll keep retrying.")
                            s["last_error"] = err_msg
                        else:
                            logging.info("[sniffer] Same error as last check for %s, skipping repeat alert.", name)
                        # If browser is dead, restart it entirely
                        try:
                            browser.version()  # lightweight liveness check
                        except Exception:
                            logging.warning("[browser] Browser unreachable — restarting.")
                            try:
                                browser.close()
                            except Exception:
                                pass
                            browser, context = make_browser()
                    finally:
                        if page is not None:
                            try:
                                page.close()
                            except Exception:
                                pass

                if manual_results:
                    send_telegram(
                        "Still out of stock. I'll keep watching.\n"
                        + "\n".join(manual_results)
                        + f"\n_Checked: {checked_at}_"
                    )

            except Exception as e:
                logging.error("[sniffer] Unexpected error: %s", e)

            # Wait for interval OR an early /check trigger
            triggered = check_now.wait(timeout=CHECK_INTERVAL)
            if triggered:
                check_now.clear()
                print(f"[sniffer] Early check triggered by /check command.\n")
            else:
                print(f"[sniffer] Next check in {CHECK_INTERVAL}s ...\n")


if __name__ == "__main__":
    main()
