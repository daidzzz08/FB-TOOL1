import asyncio
import html
from typing import Optional, Dict, Any, Tuple
from curl_cffi import requests
import database as db
from core.utils import get_vn_time_str

class TelegramNotifier:
    """Service gửi thông báo và quản lý còi báo động lặp liên tục qua Telegram Bot."""

    def __init__(self):
        self.is_alarming: bool = False
        self.alarm_task: Optional[asyncio.Task] = None
        self.current_alarm_info: Dict[str, Any] = {}

    def is_enabled(self) -> bool:
        """Kiểm tra xem cảnh báo Telegram có đang được kích hoạt hay không."""
        return db.get_setting("telegram_enabled", "1") == "1"

    def get_credentials(self) -> Tuple[str, str, int]:
        """Lấy token, chat_id và interval từ CSDL."""
        token = db.get_setting("telegram_bot_token", "7644748535:AAF538KrGP7VO8-VAZhyKO3Jj_YB7pzViwQ").strip()
        chat_id = db.get_setting("telegram_chat_id", "1922773264").strip()
        try:
            interval = int(db.get_setting("telegram_alarm_interval", "30"))
            interval = max(5, interval)
        except Exception:
            interval = 30
        return token, chat_id, interval

    def send_message(
        self,
        text: str,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Gửi tin nhắn đồng bộ tới Telegram Bot sử dụng curl_cffi (impersonate chrome124)."""
        token, default_chat_id, _ = self.get_credentials()
        target_token = (bot_token or token).strip()
        target_chat_id = (chat_id or default_chat_id).strip()

        if not target_token or not target_chat_id:
            return False, "Thiếu Bot Token hoặc Chat ID"

        url = f"https://api.telegram.org/bot{target_token}/sendMessage"
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": False
        }

        try:
            with requests.Session(impersonate="chrome124") as session:
                resp = session.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        return True, "Gửi tin nhắn Telegram thành công"
                    return False, f"Telegram API Error: {data.get('description')}"
                return False, f"HTTP Error {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, f"Lỗi kết nối Telegram: {str(e)}"

    async def send_message_async(
        self,
        text: str,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Gửi tin nhắn bất đồng bộ qua threadpool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.send_message, text, bot_token, chat_id)

    async def _alarm_loop(self, reason: str, details: Dict[str, Any], interval: int) -> None:
        """Vòng lặp reo còi cảnh báo gửi tin nhắn liên tục tới khi được tắt."""
        account_uid = str(details.get("account_uid") or details.get("actor_uid") or "Không xác định")
        group_id = str(details.get("group_id") or "--")
        error_type = details.get("error_type", "")
        error_code = str(details.get("error_code") or "1675004")
        summary = str(details.get("summary") or reason)
        duration_str = str(details.get("duration_str") or "1h 00m")
        unlock_time_str = str(details.get("unlock_time_str") or "Chưa xác định")
        detail_msg = str(details.get("detail") or reason)

        while self.is_alarming:
            current_time = get_vn_time_str()
            if error_type == "RATE_LIMIT" or "rate limit" in reason.lower():
                msg = (
                    f"🚨 <b>[PHÁT HIỆN GIỚI HẠN FACEBOOK (RATE LIMIT)]</b> 🚨\n\n"
                    f"👤 <b>Tài khoản UID:</b> <code>{html.escape(account_uid)}</code>\n"
                    f"⚠️ <b>Mã lỗi:</b> <code>{html.escape(error_code)}</code> - {html.escape(summary)}\n"
                    f"⏳ <b>Thời gian dính giới hạn:</b> {html.escape(duration_str)}\n"
                    f"🔓 <b>Dự kiến mở khóa lại:</b> <code>{html.escape(unlock_time_str)}</code>\n"
                    f"📝 <b>Chi tiết từ Meta:</b> <i>{html.escape(detail_msg)}</i>\n"
                    f"⏰ <b>Thời gian phát hiện:</b> {current_time}\n\n"
                    f"🛑 <i>Hệ thống đã tự động TẠM DỪNG chiến dịch. Cảnh báo sẽ lặp lại mỗi {interval}s cho đến khi bạn xử lý!</i>"
                )
            else:
                msg = (
                    f"🚨 <b>[CẢNH BÁO KHẨN CẤP - FB TOOL]</b> 🚨\n\n"
                    f"⚠️ <b>Nguyên nhân:</b> {html.escape(reason)}\n"
                    f"👤 <b>Tài khoản UID:</b> <code>{html.escape(account_uid)}</code>\n"
                    f"🎯 <b>Nhóm lỗi:</b> <code>{html.escape(group_id)}</code>\n"
                    f"⏰ <b>Thời gian:</b> {current_time}\n\n"
                    f"🛑 <i>Hệ thống đã tự động TẠM DỪNG chiến dịch. Cảnh báo sẽ lặp lại mỗi {interval}s cho đến khi bạn xử lý!</i>"
                )
            success, err = await self.send_message_async(msg)
            if not success:
                print(f"[TELEGRAM] Lỗi gửi còi báo động: {err}")

            # Nghỉ theo interval hoặc thoát nếu còi bị tắt
            for _ in range(interval):
                if not self.is_alarming:
                    break
                await asyncio.sleep(1)

    def start_alarm(self, reason: str, details: Optional[Dict[str, Any]] = None) -> bool:
        """Kích hoạt còi báo động lặp lại liên tục."""
        if not self.is_enabled():
            print("[TELEGRAM] Cảnh báo Telegram đang bị tắt trong cài đặt.")
            return False

        if self.is_alarming:
            # Đang có còi báo động chạy
            return True

        _, _, interval = self.get_credentials()
        details_dict = details or {}
        self.is_alarming = True
        self.current_alarm_info = {
            "reason": reason,
            "details": details_dict,
            "started_at": get_vn_time_str(),
            "interval": interval
        }

        try:
            loop = asyncio.get_running_loop()
            self.alarm_task = loop.create_task(self._alarm_loop(reason, details_dict, interval))
            print(f"[ALARM] 🚨 Đã kích hoạt còi báo động Telegram (Lặp lại mỗi {interval}s): {reason}")
            return True
        except RuntimeError:
            print("[TELEGRAM] Không tìm thấy event loop đang chạy để tạo alarm task.")
            return False

    def stop_alarm(self, send_stop_msg: bool = True) -> bool:
        """Tắt còi báo động và gửi tin nhắn xác nhận."""
        if not self.is_alarming:
            return False

        self.is_alarming = False
        if self.alarm_task and not self.alarm_task.done():
            self.alarm_task.cancel()
        self.alarm_task = None
        self.current_alarm_info = {}

        print("[ALARM] 🔕 Đã tắt còi báo động Telegram.")

        if send_stop_msg and self.is_enabled():
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.send_message_async("✅ <b>Còi báo động đã được tắt!</b>"))
            except RuntimeError:
                self.send_message("✅ <b>Còi báo động đã được tắt!</b>")

        return True


# Singleton instance
telegram_notifier = TelegramNotifier()
