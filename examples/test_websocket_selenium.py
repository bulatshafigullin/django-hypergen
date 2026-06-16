"""End-to-end browser test for the gevent websocket backend.

Drives real Chrome against a running examples server (gunicorn + the
GeventWebSocketWorker) and verifies the full websocket round-trip:

  1. group fan-out    - a message typed in one tab appears in *both* tabs.
  2. per-message render - the "#counter" target is updated by the consumer.
  3. backend push     - hitting send_message_from_backend/ over plain HTTP
                        pushes a message into an already-open chat tab via
                        the module-level group_send().

Run (server must already be listening on BASE):

    HYPERGEN_WS_BACKEND=gevent ... gunicorn ... wsgi:application   # in another shell
    python -m pytest test_websocket_selenium.py -q
    # or just: python test_websocket_selenium.py
"""
import os
import time
import urllib.request

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

BASE = os.environ.get("HG_BASE_URL", "http://127.0.0.1:8002")
CHAT_URL = BASE + "/websockets/chat/"
BACKEND_PUSH_URL = BASE + "/websockets/send_message_from_backend/"


def _make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1200,900")
    # Selenium Manager defers to any chromedriver on PATH even when it is the
    # wrong major version, so allow pinning an explicit driver binary.
    driver_path = os.environ.get("HG_CHROMEDRIVER")
    service = Service(executable_path=driver_path) if driver_path else None
    return webdriver.Chrome(options=opts, service=service)


def _wait_for(fn, timeout=15, interval=0.25):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # element not present yet, etc.
            last = e
        time.sleep(interval)
    raise AssertionError("Timed out waiting for condition. Last=%r" % (last,))


def _messages_text(driver):
    return driver.find_element(By.ID, "messages").text


def _counter_text(driver):
    return driver.find_element(By.ID, "counter").text


def test_gevent_websocket_chat_and_backend_push():
    a = _make_driver()
    b = _make_driver()
    try:
        a.get(CHAT_URL)
        b.get(CHAT_URL)
        # Give both sockets time to open (Sockette connects on load).
        _wait_for(lambda: a.find_element(By.ID, "message").is_displayed())
        _wait_for(lambda: b.find_element(By.ID, "message").is_displayed())
        time.sleep(1.5)

        # 1 + 2: send from tab A, assert it lands in BOTH tabs (group fan-out)
        # and that the counter target was rendered by the consumer.
        msg = "hello-gevent-ws"
        box = a.find_element(By.ID, "message")
        box.click()
        box.send_keys(msg)
        box.send_keys(Keys.ENTER)

        _wait_for(lambda: msg in _messages_text(a))
        _wait_for(lambda: msg in _messages_text(b))
        _wait_for(lambda: "Length of last message is: %d" % len(msg) in _counter_text(a))
        print("OK: group fan-out + counter render")

        # 3: backend push over plain HTTP -> appears in the open tab A.
        urllib.request.urlopen(BACKEND_PUSH_URL, timeout=10).read()
        _wait_for(lambda: "Server message!" in _messages_text(a))
        print("OK: backend push via group_send()")
    finally:
        a.quit()
        b.quit()


if __name__ == "__main__":
    test_gevent_websocket_chat_and_backend_push()
    print("\nALL BROWSER TESTS PASSED")
