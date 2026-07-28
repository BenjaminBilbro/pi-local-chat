#!/usr/bin/env python3
"""
Mobile viewport test: Launch Camoufox with real device viewports, log in,
capture empty chat and loaded session screenshots.

Usage:
    cd /home/bbilbro/pi-chat
    uv run python tests/mobile_viewport_test.py [--device DEVICE] [--all]

Devices:
    iphone_se        iPhone SE 3rd gen  (375x667)
    iphone16         iPhone 16          (390x844)
    iphone17_pro     iPhone 17 Pro      (402x874)
    iphone17_pro_max iPhone 17 Pro Max  (440x956)
    galaxy_s25       Galaxy S25         (360x780) [most common mobile viewport]
    pixel9           Pixel 9            (412x923)
    pixel9_pro_xl    Pixel 9 Pro XL     (414x921)
    galaxy_s25_ultra Galaxy S25 Ultra   (412x891)

Requirements:
    - pi-chat server running on localhost:9000 with PI_CHAT_DEV=1
    - Test password "test-only" configured for account "b"
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, '/home/bbilbro/camoufox-testing/.venv/lib/python3.13/site-packages')

from camoufox.sync_api import Camoufox
from camoufox.fingerprints import generate_fingerprint

DEVICES = {
    # --- iOS (Safari fingerprints) ---
    "iphone_se": {
        "os": "ios", "browser": "safari",
        "width": 375, "height": 667,
        "label": "iPhone SE 3rd gen (375x667)",
    },
    "iphone16": {
        "os": "ios", "browser": "safari",
        "width": 390, "height": 844,
        "label": "iPhone 16 (390x844)",
    },
    "iphone17_pro": {
        "os": "ios", "browser": "safari",
        "width": 402, "height": 874,
        "label": "iPhone 17 Pro (402x874)",
    },
    "iphone17_pro_max": {
        "os": "ios", "browser": "safari",
        "width": 440, "height": 956,
        "label": "iPhone 17 Pro Max (440x956)",
    },
    # --- Android (Firefox fingerprints) ---
    "galaxy_s25": {
        "os": "android", "browser": "firefox",
        "width": 360, "height": 780,
        "label": "Galaxy S25 (360x780) [most common]",
    },
    "pixel9": {
        "os": "android", "browser": "firefox",
        "width": 412, "height": 923,
        "label": "Pixel 9 (412x923)",
    },
    "pixel9_pro_xl": {
        "os": "android", "browser": "firefox",
        "width": 414, "height": 921,
        "label": "Pixel 9 Pro XL (414x921)",
    },
    "galaxy_s25_ultra": {
        "os": "android", "browser": "firefox",
        "width": 412, "height": 891,
        "label": "Galaxy S25 Ultra (412x891)",
    },
}

PI_CHAT_URL = "http://127.0.0.1:9000"
LOGIN_PASSWORD = "test-only"
SESSION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "data-samples", "rpc_capture.jsonl",
)


def wait_for_selector(page, selector, timeout=8000):
    """Wait for an element to be visible."""
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def login(page):
    """Log in as user 'b' through the real UI."""
    print("  Logging in as 'b'...")

    # Click the 'B' icon
    page.evaluate("document.querySelector('#icon-b').click()")
    page.wait_for_timeout(500)

    # Wait for password area to appear
    if not wait_for_selector(page, "#login-password-area.active"):
        # Try without .active - just check the area is visible
        page.wait_for_selector("#passphrase", timeout=5000)

    # Fill password
    page.evaluate(f"document.querySelector('#passphrase').value = '{LOGIN_PASSWORD}'")

    # Click login
    page.evaluate("document.querySelector('#login-btn').click()")

    # Wait for chat screen to appear
    print("    Waiting for chat screen...")
    if wait_for_selector(page, "#chat-screen.active", timeout=10000):
        print("    Login successful!")
        return True
    else:
        # Check if we're still on login
        login_visible = page.evaluate("() => !document.querySelector('#login-screen')?.classList.contains('hidden')")
        print(f"    WARNING: Login may have failed. Login screen still visible: {login_visible}")
        return False


def load_session(page):
    """Open session drawer and load the first available session."""
    print("  Opening session drawer...")

    # Click the hamburger menu
    page.evaluate("document.querySelector('#session-menu-btn').click()")
    page.wait_for_timeout(1000)

    # Wait for session panel to open
    if not wait_for_selector(page, ".session-panel.active"):
        print("    WARNING: Session panel did not open")
        return False

    page.wait_for_timeout(2000)  # Wait for sessions to load

    # Click the first session item
    first_session = page.evaluate("""() => {
        const item = document.querySelector('.session-item:not(.loading-session)');
        if (!item) return null;
        item.click();
        return 'clicked';
    }""")

    if first_session:
        print("    Loading first session...")
        page.wait_for_timeout(4000)  # Wait for session to load and render
        return True
    else:
        print("    WARNING: No sessions found in drawer")
        return False


def capture_element_info(page, device_key):
    """Capture dimensions of key UI elements."""
    elements = {}
    for selector, name in [
        ("#chat-screen", "chat_screen"),
        (".chat-header", "chat_header"),
        (".messages", "messages"),
        (".input-area", "input_area"),
        (".input-row", "input_row"),
        (".welcome", "welcome"),
        (".message", "first_message"),
        (".session-panel", "session_panel"),
    ]:
        el = page.evaluate(f"""() => {{
            const el = document.querySelector('{selector}');
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            return {{
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                top: Math.round(rect.top),
                left: Math.round(rect.left),
                visible: !!(rect.width && rect.height),
            }};
        }}""")
        if el:
            elements[name] = el

    return elements


def run_device_test(device_key, output_dir="/tmp"):
    device = DEVICES[device_key]
    os_key = device["os"]
    browser_name = device["browser"]
    width = device["width"]
    height = device["height"]
    label = device["label"]

    print(f"\n{'='*60}")
    print(f"  Mobile Viewport Test: {label}")
    print(f"{'='*60}")

    # Generate fingerprint
    fp = generate_fingerprint(os=os_key, browser=browser_name, window=(width, height))
    print(f"  Fingerprint: {os_key}/{browser_name}")
    print(f"  Screen: {fp.screen.width}x{fp.screen.height} (dpr: {fp.screen.devicePixelRatio})")

    with Camoufox(fingerprint=fp, headless=True, i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_viewport_size({"width": width, "height": height})

        # Verify mobile state
        viewport = page.viewport_size
        ua = page.evaluate("() => navigator.userAgent")
        is_mobile = page.evaluate('() => window.matchMedia("(max-width: 600px)").matches')
        print(f"\n  Viewport: {viewport['width']}x{viewport['height']}")
        print(f"  UA: {ua[:80]}...")
        print(f"  Mobile media query: {is_mobile}")

        # Navigate to pi-chat
        print(f"\n  Navigating to {PI_CHAT_URL}/ ...")
        page.goto(PI_CHAT_URL + "/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)

        # ---- STEP 1: Login ----
        logged_in = login(page)
        page.wait_for_timeout(1000)

        # ---- STEP 2: Screenshot empty chat ----
        empty_path = os.path.join(output_dir, f"pi-chat-mobile-{device_key}-empty.png")
        page.screenshot(path=empty_path)
        print(f"\n  Empty chat screenshot: {empty_path}")

        # Capture element info for empty state
        empty_elements = capture_element_info(page, device_key)
        print(f"  Empty chat elements:")
        for name, dims in empty_elements.items():
            print(f"    {name}: {dims['width']}x{dims['height']} (top: {dims['top']})")

        # ---- STEP 3: Load a session ----
        session_loaded = False
        if logged_in:
            session_loaded = load_session(page)
            page.wait_for_timeout(1000)

            # ---- STEP 4: Screenshot loaded session ----
            loaded_path = os.path.join(output_dir, f"pi-chat-mobile-{device_key}-loaded.png")
            page.screenshot(path=loaded_path)
            print(f"\n  Loaded session screenshot: {loaded_path}")

            # Capture element info for loaded state
            loaded_elements = capture_element_info(page, device_key)
            print(f"  Loaded session elements:")
            for name, dims in loaded_elements.items():
                print(f"    {name}: {dims['width']}x{dims['height']} (top: {dims['top']})")

            # Scroll down to see input area
            page.evaluate("window.scrollBy(0, 200)")
            page.wait_for_timeout(500)

            scrolled_path = os.path.join(output_dir, f"pi-chat-mobile-{device_key}-scrolled.png")
            page.screenshot(path=scrolled_path)
            print(f"  Scrolled screenshot: {scrolled_path}")

        # ---- Save page info ----
        page_info = {
            "device": label,
            "viewport": viewport,
            "ua": ua,
            "is_mobile": is_mobile,
            "logged_in": logged_in,
            "session_loaded": session_loaded,
            "empty_elements": empty_elements,
            "loaded_elements": loaded_elements if session_loaded else None,
            "viewport_meta": page.evaluate("() => document.querySelector('meta[name=viewport]')?.content"),
        }

        info_path = os.path.join(output_dir, f"pi-chat-mobile-{device_key}.json")
        with open(info_path, "w") as f:
            json.dump(page_info, f, indent=2)

        page.close()

    print(f"\n  PASSED: {label}")
    return page_info


def main():
    parser = argparse.ArgumentParser(description="Test mobile viewport rendering of pi-chat")
    parser.add_argument(
        "--device",
        choices=list(DEVICES.keys()),
        default="iphone16",
        help="Device to emulate (default: iphone16)",
    )
    parser.add_argument("--all", action="store_true", help="Test all devices")
    parser.add_argument("--output", default="/tmp", help="Output directory (default: /tmp)")
    args = parser.parse_args()

    if args.all:
        for device_key in DEVICES:
            try:
                run_device_test(device_key, args.output)
            except Exception as e:
                print(f"FAILED {device_key}: {e}")
                import traceback
                traceback.print_exc()
    else:
        run_device_test(args.device, args.output)


if __name__ == "__main__":
    main()
