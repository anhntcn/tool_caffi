import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import ssl
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timedelta

# Ép stdout/stderr dùng UTF-8 để print tiếng Việt không crash trên Windows console
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

PROFILE_DIR = r"C:\SeleniumChromeProfile"
TARGET_URL = "https://app.caffiliate.vn/rewards"

CHECKIN_BUTTON_ID = "btnCheckIn"
CHECKIN_XPATH = "//a[contains(text(), 'NHẬN QUÀ')] | //button[contains(text(), 'NHẬN QUÀ')]"

PRELOAD_AT = (23, 59, 50)
MIDNIGHT_REFRESH_AT = (0, 0, 0)
RESULT_WAIT_SECONDS = 60
JS_POLL_INTERVAL_MS = 10

API_HOST = "app.caffiliate.vn"
API_PATH = "/api/v2/xeng/check-in-secure"
CHECKIN_API_URL = f"https://{API_HOST}{API_PATH}"
XENG_SECRET_FALLBACK = "caffi_xeng_secure_2026_x82"

# Fire trước midnight ~500ms: đêm Top 3 fire 23:59:59.526 với latency 493ms thành công.
# Server queue và xử lý request quanh midnight, mình vào queue sớm.
API_FIRE_OFFSET_MS = -500
API_BURST_COUNT = 4
API_BURST_SPACING_MS = 100

def _load_dotenv():
    """Load key=value pairs từ .env vào os.environ (chỉ set nếu chưa có)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram chưa cấu hình] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram limit 4096 char, cắt để tránh fail khi exception trace quá dài
    text = f"[Caffiliate] {message}"[:3900]
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            json.loads(resp.read())
    except Exception as e:
        print(f"Gửi Telegram thất bại: {e}")


def setup_driver():
    options = Options()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)
    # Tránh treo vô hạn nếu Chrome/server không phản hồi
    driver.set_page_load_timeout(15)
    driver.set_script_timeout(10)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def wait_until(hms):
    while True:
        now = datetime.now()
        if (now.hour, now.minute, now.second) >= hms:
            return
        time.sleep(0.2)


def find_button(driver):
    try:
        return driver.find_element(By.ID, CHECKIN_BUTTON_ID)
    except NoSuchElementException:
        return driver.find_element(By.XPATH, CHECKIN_XPATH)


def is_disabled(el):
    if not el.is_enabled():
        return True
    # get_attribute("disabled") trả về "" khi có attribute nhưng giá trị rỗng,
    # None khi không có attribute. Phải check is not None thay vì truthy.
    if el.get_attribute("disabled") is not None:
        return True
    cls = el.get_attribute("class") or ""
    if "pointer-events-none" in cls or "cursor-not-allowed" in cls:
        return True
    return False


def is_logged_out(driver):
    url = driver.current_url.lower()
    if "login" in url or "signin" in url or "sign-in" in url:
        return True
    try:
        driver.find_element(By.ID, CHECKIN_BUTTON_ID)
        return False
    except NoSuchElementException:
        pass
    try:
        driver.find_element(By.XPATH, CHECKIN_XPATH)
        return False
    except NoSuchElementException:
        pass
    return True


def healthcheck_mode():
    print("Health check: kiểm tra trạng thái đăng nhập...")
    driver = None
    try:
        driver = setup_driver()
        driver.get(TARGET_URL)
        time.sleep(3)
        if is_logged_out(driver):
            url = driver.current_url
            msg = f"🚨 BỊ ĐĂNG XUẤT! Hãy đăng nhập lại trước 23:59. URL hiện tại: {url}"
            print(msg)
            send_telegram(msg)
        else:
            print("Vẫn đang đăng nhập, OK.")
            # send_telegram(f"✓ Health check OK lúc {datetime.now().strftime('%H:%M')} — vẫn đang đăng nhập.")
    except Exception as e:
        send_telegram(f"❌ Health check lỗi: {e}")
        raise
    finally:
        if driver is not None:
            driver.quit()


def now_mode():
    print("Chạy điểm danh NGAY (không chờ midnight)...")
    driver = None
    try:
        driver = setup_driver()
        driver.get(TARGET_URL)

        try:
            WebDriverWait(driver, 15).until(
                EC.any_of(
                    EC.presence_of_element_located((By.ID, CHECKIN_BUTTON_ID)),
                    EC.presence_of_element_located((By.XPATH, CHECKIN_XPATH)),
                )
            )
        except Exception as e:
            msg = f"🚨 Không tìm thấy nút checkin. logged_out={is_logged_out(driver)}, URL={driver.current_url}, err={type(e).__name__}"
            print(msg)
            send_telegram(msg)
            return

        # Chờ JS fetch xong và set trạng thái cuối cùng (race condition: initial render enable, rồi disabled sau API call)
        time.sleep(2)

        button = find_button(driver)
        if is_disabled(button):
            msg = "⚠️ Nút đang disabled — có thể đã điểm danh hôm nay rồi."
            print(msg)
            send_telegram(msg)
            return

        # Click an toàn: chờ thực sự clickable trong 3s, nếu không thì coi như disabled
        try:
            button = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.ID, CHECKIN_BUTTON_ID))
            )
        except Exception:
            msg = "⚠️ Nút không clickable (state chuyển sang disabled) — đã điểm danh hôm nay."
            print(msg)
            send_telegram(msg)
            return

        button.click()
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"Click điểm danh thành công lúc {ts}!")
        send_telegram(f"✅ Điểm danh tay (now mode) thành công lúc {ts}")
        time.sleep(5)
    except Exception as e:
        send_telegram(f"❌ Lỗi now mode: {type(e).__name__}: {str(e)[:500]}")
        raise
    finally:
        if driver is not None:
            driver.quit()


def extract_api_credentials(driver):
    """Lấy csrfToken, xengSecret, googleId từ HTML source (chúng là const trong IIFE,
    không expose ra window). Cookies + UA lấy từ Selenium."""
    html = driver.page_source

    def find(key):
        m = re.search(rf"{key}\s*:\s*'([^']+)'", html)
        return m.group(1) if m else None

    csrf = find("csrfToken")
    secret = find("xengSecret") or XENG_SECRET_FALLBACK
    user_id = find("googleId") or find("userEmail")

    if not csrf or not user_id:
        raise RuntimeError(f"Thiếu csrfToken hoặc userId trong HTML (csrf={csrf}, user={user_id})")

    cookies = "; ".join(f"{c['name']}={c['value']}" for c in driver.get_cookies())
    user_agent = driver.execute_script("return navigator.userAgent;")
    return {
        "csrf_token": csrf,
        "xeng_secret": secret,
        "user_id": user_id,
        "cookies": cookies,
        "user_agent": user_agent,
    }


def generate_nonce():
    # Khớp format JS: Math.random().toString(36).substring(2,15) — chuỗi base36, ~12 ký tự
    alphabet = string.digits + string.ascii_lowercase
    return "".join(secrets.choice(alphabet) for _ in range(12))


def sign_request(secret, timestamp_ms, nonce, user_id):
    base = f"{timestamp_ms}.{nonce}.{user_id}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def build_headers(creds):
    timestamp = str(int(time.time() * 1000))
    nonce = generate_nonce()
    signature = sign_request(creds["xeng_secret"], timestamp, nonce, creds["user_id"])
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://app.caffiliate.vn",
        "Referer": "https://app.caffiliate.vn/rewards",
        "User-Agent": creds["user_agent"],
        "Cookie": creds["cookies"],
        "x-csrf-token": creds["csrf_token"],
        "x-signature": signature,
        "x-timestamp": timestamp,
        "x-nonce": nonce,
    }


def post_checkin(creds):
    headers = build_headers(creds)
    req = urllib.request.Request(CHECKIN_API_URL, data=b"{}", headers=headers, method="POST")
    sent_at = datetime.now()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:300]
        return sent_at, {"success": False, "httpError": e.code, "body": body_text}
    except Exception as e:
        return sent_at, {"success": False, "error": str(e)[:300]}
    return sent_at, payload


def open_warm_connection():
    """Mở TLS connection sẵn để TLS handshake không tính vào latency POST."""
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(API_HOST, timeout=10, context=ctx)
    conn.connect()  # Force TLS handshake ngay
    return conn


def post_checkin_via_conn(conn, creds):
    """POST qua connection đã warm. Trả về (sent_at, payload, recv_at)."""
    headers = build_headers(creds)
    sent_at = datetime.now()
    try:
        conn.request("POST", API_PATH, body=b"{}", headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        recv_at = datetime.now()
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"success": False, "rawBody": body.decode(errors="replace")[:300], "status": resp.status}
    except Exception as e:
        return sent_at, {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}, datetime.now()
    return sent_at, payload, recv_at


def wait_until_next_midnight(offset_ms=0):
    """Chờ tới midnight gần nhất + offset. offset âm = chờ tới trước midnight, dương = sau."""
    now = datetime.now()
    next_midnight = datetime.combine(now.date() + timedelta(days=1), dt_time())
    target = next_midnight + timedelta(milliseconds=offset_ms)
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        # Sleep gradually shorter as we approach target
        if remaining > 5:
            time.sleep(min(remaining - 1, 30))
        elif remaining > 0.1:
            time.sleep(0.01)
        else:
            time.sleep(0.0001)


def api_mode():
    try:
        _api_mode_inner()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()[-800:]
        send_telegram(f"💥 api_mode CRASH: {type(e).__name__}: {str(e)[:200]}\n{tb}")
        raise


def _api_mode_inner():
    print(f"API mode: preload {PRELOAD_AT}, fire midnight{API_FIRE_OFFSET_MS:+d}ms, burst x{API_BURST_COUNT} mỗi {API_BURST_SPACING_MS}ms")
    wait_until(PRELOAD_AT)

    driver = None
    creds = None
    try:
        driver = setup_driver()
        driver.get(TARGET_URL)
        send_telegram(f"Bắt đầu phiên API điểm danh ngày {datetime.now().strftime('%Y-%m-%d')}")

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.ID, CHECKIN_BUTTON_ID))
            )
        except Exception as e:
            send_telegram(f"🚨 Page không load button: {type(e).__name__}. logged_out={is_logged_out(driver)}")
            return

        creds = extract_api_credentials(driver)
        send_telegram(f"🔑 Extract credentials OK (csrf {creds['csrf_token'][:8]}..., user {creds['user_id']})")
    finally:
        if driver is not None:
            driver.quit()

    if not creds:
        return

    # Chờ tới fire time
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Wait until midnight{API_FIRE_OFFSET_MS:+d}ms...", flush=True)
    wait_until_next_midnight(offset_ms=API_FIRE_OFFSET_MS)
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Start burst", flush=True)
    send_telegram(f"🎯 Bắt đầu burst {API_BURST_COUNT} request lúc {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

    # Burst N requests qua urllib (mỗi request tự tạo connection, không tái dùng).
    # Timeout 3s mỗi request để tránh hang vô hạn nếu server không trả lời.
    results = []
    success_payload = None
    success_idx = -1
    success_sent_at = None
    success_recv_at = None
    import socket as _socket
    _socket.setdefaulttimeout(3)
    for i in range(API_BURST_COUNT):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] burst #{i+1}", flush=True)
        sent_at, payload = post_checkin(creds)
        recv_at = datetime.now()
        latency_ms = (recv_at - sent_at).total_seconds() * 1000
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] burst #{i+1} done ({latency_ms:.0f}ms): {str(payload)[:120]}", flush=True)
        results.append((i, sent_at, payload, latency_ms))
        if payload.get("success") and success_payload is None:
            success_payload = payload
            success_idx = i
            success_sent_at = sent_at
            success_recv_at = recv_at
            break
        if i < API_BURST_COUNT - 1:
            time.sleep(API_BURST_SPACING_MS / 1000.0)

    if success_payload and success_sent_at and success_recv_at:
        data = success_payload.get("data", {})
        latency_ms = (success_recv_at - success_sent_at).total_seconds() * 1000
        fire = success_sent_at.strftime('%H:%M:%S.%f')[:-3]
        msg = f"✅ streak={data.get('streakDay')} +{data.get('rewardValue')} | #{success_idx + 1} | fire {fire} | {latency_ms:.0f}ms"
        print(msg)
        send_telegram(msg)
    else:
        first_err = results[0][2].get('message') or results[0][2].get('error') or 'fail'
        msg = f"❌ Burst x{API_BURST_COUNT} fail: {first_err}"
        print(msg)
        send_telegram(msg)


def login_mode():
    driver = setup_driver()
    try:
        driver.get(TARGET_URL)
        input("Đăng nhập xong, nhấn Enter để đóng trình duyệt và lưu session...")
    finally:
        driver.quit()


JS_ARM_POLLER = """
// Cài poller chạy 100% trong browser context — không qua chromedriver mỗi vòng.
// Poll mỗi N ms, click button ngay khi enable, ghi kết quả vào window.__checkin.
window.__checkin = {clicked: false, clickedAt: null, attempts: 0, error: null};
window.__checkinInterval = setInterval(function() {
    try {
        window.__checkin.attempts++;
        var btn = document.getElementById('btnCheckIn');
        if (!btn) return;
        if (btn.disabled) return;
        if (btn.classList && btn.classList.contains('pointer-events-none')) return;
        btn.click();
        window.__checkin.clicked = true;
        window.__checkin.clickedAt = new Date().toISOString();
        clearInterval(window.__checkinInterval);
    } catch (e) {
        window.__checkin.error = String(e);
    }
}, POLL_MS);
return 'armed';
""".strip()


def checkin_mode():
    print(f"Đang canh giờ preload trang ({PRELOAD_AT[0]:02d}:{PRELOAD_AT[1]:02d}:{PRELOAD_AT[2]:02d})...")
    wait_until(PRELOAD_AT)

    driver = None
    try:
        driver = setup_driver()
        driver.get(TARGET_URL)
        send_telegram(f"Bắt đầu phiên điểm danh ngày {datetime.now().strftime('%Y-%m-%d')}")

        try:
            WebDriverWait(driver, 15).until(
                EC.any_of(
                    EC.presence_of_element_located((By.ID, CHECKIN_BUTTON_ID)),
                    EC.presence_of_element_located((By.XPATH, CHECKIN_XPATH)),
                )
            )
        except Exception as e:
            try:
                logged_out = is_logged_out(driver)
                current_url = driver.current_url
            except Exception:
                logged_out = "unknown"
                current_url = "unknown"
            msg = f"🚨 Không tìm thấy nút checkin lúc preload. logged_out={logged_out}, URL={current_url}, err={type(e).__name__}"
            print(msg)
            send_telegram(msg)
            return

        time.sleep(2)  # Chờ JS settle trạng thái cuối cùng
        button_enabled_at_preload = not is_disabled(find_button(driver))
        send_telegram(f"📍 Preload xong, nút {'đang enable (chưa điểm hôm nay)' if button_enabled_at_preload else 'disabled (đã điểm hôm nay)'}. Chờ tới midnight...")

        # Nếu nút đang enable ở 23:59:50 = chưa điểm hôm nay → click LUÔN để cứu điểm hôm nay (kẻo lỡ).
        # Việc này tiêu điểm hôm nay, nhưng đảm bảo không miss streak.
        if button_enabled_at_preload:
            try:
                btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.ID, CHECKIN_BUTTON_ID)))
                btn.click()
                send_telegram(f"🛟 Click cứu vớt điểm danh hôm nay lúc {datetime.now().strftime('%H:%M:%S.%f')[:-3]} (để không lỡ streak)")
                time.sleep(2)
            except Exception as e:
                send_telegram(f"⚠️ Cứu vớt fail: {type(e).__name__}")

        # Chờ qua midnight rồi REFRESH page để server gửi state ngày mới (button enable cho hôm sau)
        wait_until(MIDNIGHT_REFRESH_AT)
        refresh_ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        try:
            driver.refresh()
        except WebDriverException as e:
            send_telegram(f"❌ Refresh sau midnight fail: {type(e).__name__}: {str(e)[:200]}")
            return
        send_telegram(f"🔄 Đã refresh page lúc {refresh_ts}. Arm JS poller cho ngày mới...")

        # Đợi DOM mới có button rồi arm poller
        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, CHECKIN_BUTTON_ID)))
        except Exception as e:
            send_telegram(f"❌ Sau refresh không thấy button: {type(e).__name__}")
            return

        js = JS_ARM_POLLER.replace("POLL_MS", str(JS_POLL_INTERVAL_MS))
        driver.execute_script(js)
        send_telegram(f"⏱ Đã arm JS poller (mỗi {JS_POLL_INTERVAL_MS}ms) lúc {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

        # Chờ JS click thành công. Poll state qua chromedriver mỗi 500ms (đủ nhanh để biết kết quả).
        deadline = time.time() + RESULT_WAIT_SECONDS
        last_heartbeat = time.time()
        while time.time() < deadline:
            try:
                state = driver.execute_script("return window.__checkin;")
            except WebDriverException as e:
                send_telegram(f"❌ Mất kết nối Chrome khi poll state: {type(e).__name__}")
                return

            if state and state.get("clicked"):
                ts = state.get("clickedAt") or datetime.now().isoformat()
                attempts = state.get("attempts")
                send_telegram(f"✅ Điểm danh thành công! clickedAt={ts}, attempts JS={attempts}")
                time.sleep(5)
                return

            if state and state.get("error"):
                send_telegram(f"⚠️ JS poller lỗi: {state['error']}")
                state["error"] = None  # reset để báo lỗi mới

            if time.time() - last_heartbeat > 15:
                send_telegram(f"⏳ Đợi JS click... attempts JS={state.get('attempts') if state else '?'}")
                last_heartbeat = time.time()

            time.sleep(0.5)

        # Hết thời gian
        try:
            final_state = driver.execute_script("return window.__checkin;")
        except Exception:
            final_state = None
        msg = f"❌ Hết {RESULT_WAIT_SECONDS}s mà JS không click được sau refresh midnight. final={final_state}"
        print(msg)
        send_telegram(msg)
    except Exception as e:
        send_telegram(f"❌ Lỗi điểm danh: {type(e).__name__}: {str(e)[:500]}")
        raise
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "checkin"
    if mode == "login":
        login_mode()
    elif mode == "healthcheck":
        healthcheck_mode()
    elif mode == "now":
        now_mode()
    elif mode == "api":
        api_mode()
    elif mode == "api-now":
        # Test API mode ngay không chờ midnight: lấy creds rồi gửi POST luôn
        d = setup_driver()
        try:
            d.get(TARGET_URL)
            WebDriverWait(d, 15).until(EC.presence_of_element_located((By.ID, CHECKIN_BUTTON_ID)))
            creds = extract_api_credentials(d)
        finally:
            d.quit()
        print(f"Creds OK: csrf={creds['csrf_token'][:8]}..., user={creds['user_id']}, secret={creds['xeng_secret']}")
        print("Fire POST...")
        _, payload = post_checkin(creds)
        print("Response:", payload)
    else:
        checkin_mode()
