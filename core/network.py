import time
import random
import asyncio
from typing import Callable, Any
from curl_cffi import requests


def check_internet_connection(timeout: float = 3.0) -> bool:
    """Kiểm tra xem thiết bị có kết nối Internet thực tế hay không."""
    test_urls = [
        "https://1.1.1.1",
        "https://8.8.8.8",
        "https://www.google.com"
    ]
    for url in test_urls:
        try:
            with requests.Session(impersonate="chrome124") as s:
                r = s.get(url, timeout=timeout)
                if r.status_code in [200, 204, 301, 302]:
                    return True
        except Exception:
            continue
    return False


def wait_for_network_restoration_sync(check_interval: float = 5.0, max_wait: float = 600.0) -> bool:
    """Phiên bản đồng bộ: Tạm dừng tiến trình và chờ đến khi có mạng trở lại."""
    if check_internet_connection():
        return True

    print("[NETWORK] 🚨 Mất kết nối Internet! Tiến trình đăng bài tạm dừng và chờ mạng...")
    start_time = time.time()

    while time.time() - start_time < max_wait:
        time.sleep(check_interval)
        if check_internet_connection():
            print("[NETWORK] 🟢 Internet đã phục hồi! Tiếp tục thực thi nhiệm vụ...")
            return True

    print("[NETWORK] ❌ Mạng không phục hồi sau thời gian chờ tối đa.")
    return False


async def wait_for_network_restoration(check_interval: float = 5.0, max_wait: float = 600.0) -> bool:
    """Nếu rớt mạng, tạm dừng tiến trình async và chờ đến khi có mạng trở lại."""
    if check_internet_connection():
        return True

    print("[NETWORK] 🚨 Mất kết nối Internet! Tiến trình đăng bài tạm dừng và chờ mạng...")
    start_time = time.time()

    while time.time() - start_time < max_wait:
        await asyncio.sleep(check_interval)
        if check_internet_connection():
            print("[NETWORK] 🟢 Internet đã phục hồi! Tiếp tục thực thi nhiệm vụ...")
            return True

    print("[NETWORK] ❌ Mạng không phục hồi sau thời gian chờ tối đa.")
    return False


def execute_with_smart_retry_sync(
    func: Callable[[], Any],
    max_retries: int = 4,
    base_delay: float = 3.0
) -> Any:
    """Phiên bản đồng bộ: Thực thi hàm với Exponential Backoff + Jitter và Canh gác mạng."""
    attempt = 0
    while attempt < max_retries:
        try:
            network_ok = wait_for_network_restoration_sync()
            if not network_ok:
                raise ConnectionError("Không thể kết nối Internet")

            return func()
        except Exception as e:
            attempt += 1
            err_msg = str(e).lower()
            is_net_err = any(k in err_msg for k in ["connection", "timeout", "timed out", "reset", "proxy", "resolve", "failed to connect"])

            if not is_net_err or attempt >= max_retries:
                raise e

            sleep_time = (base_delay * (2 ** (attempt - 1))) + random.uniform(1.0, 3.0)
            print(f"[RETRY] ⚠️ Gặp lỗi kết nối ({e}). Thử lại lần {attempt}/{max_retries} sau {sleep_time:.1f}s...")
            time.sleep(sleep_time)


async def execute_with_smart_retry(
    async_func: Callable[[], Any],
    max_retries: int = 4,
    base_delay: float = 3.0
) -> Any:
    """Thực thi hàm mạng async với cơ chế Exponential Backoff + Jitter và Canh gác phục hồi mạng."""
    attempt = 0
    while attempt < max_retries:
        try:
            # Kiểm tra trạng thái mạng trước khi bắn request
            network_ok = await wait_for_network_restoration()
            if not network_ok:
                raise ConnectionError("Không thể kết nối Internet")

            if asyncio.iscoroutinefunction(async_func):
                return await async_func()
            else:
                res = async_func()
                if asyncio.iscoroutine(res):
                    return await res
                return res
        except Exception as e:
            attempt += 1
            err_msg = str(e).lower()
            is_net_err = any(k in err_msg for k in ["connection", "timeout", "timed out", "reset", "proxy", "resolve", "failed to connect"])

            if not is_net_err or attempt >= max_retries:
                raise e

            sleep_time = (base_delay * (2 ** (attempt - 1))) + random.uniform(1.0, 3.0)
            print(f"[RETRY] ⚠️ Gặp lỗi kết nối ({e}). Thử lại lần {attempt}/{max_retries} sau {sleep_time:.1f}s...")
            await asyncio.sleep(sleep_time)
