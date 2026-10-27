import os
import sys
import psutil
import asyncio
import html
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

import database as db
from core.utils import get_vn_now, VN_TZ

BOT_TOKEN = "7644748535:AAF538KrGP7VO8-VAZhyKO3Jj_YB7pzViwQ"
ADMIN_CHAT_ID = 1922773264


def check_admin(update: Update) -> bool:
    user = update.effective_user
    is_admin = bool(user and user.id == ADMIN_CHAT_ID)
    if not is_admin and user:
        print(f"[TELEGRAM_BOT] ⚠️ [SECURITY ALERT] Từ chối truy cập từ user_id={user.id} (@{user.username or 'none'}, {user.full_name})")
    return is_admin


class TelegramRemoteDaemon:
    def __init__(self, app_state_ref: Any):
        self.app_state = app_state_ref  # Tham chiếu đến poster_controller, radar_controller, main state
        self.application: Optional[Application] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def get_radar_status(self) -> Dict[str, Any]:
        st = self.app_state.get("radar_status", {})
        if callable(st):
            return st()
        return st if isinstance(st, dict) else {}

    def get_poster_status(self) -> Dict[str, Any]:
        st = self.app_state.get("poster_status", {})
        if callable(st):
            return st()
        return st if isinstance(st, dict) else {}

    def build_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        # Lấy trạng thái hiện tại
        radar_st = self.get_radar_status()
        is_radar_running = radar_st.get("is_running", False)

        poster_st = self.get_poster_status()
        is_poster_running = poster_st.get("is_running", False)

        radar_btn_text = "🟢 Radar: Đang chạy" if is_radar_running else "⚪ Radar: Đang tắt"
        poster_btn_text = "🟢 Poster: Đang chạy" if is_poster_running else "⚪ Poster: Đang tắt"

        keyboard = [
            [
                InlineKeyboardButton(poster_btn_text, callback_data="toggle_poster"),
                InlineKeyboardButton(radar_btn_text, callback_data="toggle_radar")
            ],
            [
                InlineKeyboardButton("🎯 Menu Radar Khách", callback_data="menu_radar"),
                InlineKeyboardButton("👥 Trạng Thái Clones", callback_data="menu_clones")
            ],
            [
                InlineKeyboardButton("📊 Báo Cáo Nhanh", callback_data="menu_report"),
                InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="refresh_dashboard")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_dashboard_text(self) -> str:
        now_str = get_vn_now().strftime("%H:%M:%S %d/%m/%Y")
        try:
            cpu_usage = psutil.cpu_percent(interval=None)
            ram_usage = psutil.virtual_memory().percent
        except Exception:
            cpu_usage = 0.0
            ram_usage = 0.0

        accounts = db.get_all_accounts()
        live_accs = sum(1 for a in accounts if a.get("status") == "LIVE")
        sentry_accs = sum(1 for a in accounts if a.get("status") == "SENTRY_COOLDOWN")

        # Đếm leads hôm nay
        today_start = get_vn_now().replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        all_leads = db.get_leads(limit=200)
        today_leads = sum(1 for l in all_leads if str(l.get("created_time") or "") >= today_start)

        # Lấy trạng thái Radar & Poster
        radar_st = self.get_radar_status()
        is_radar_running = radar_st.get("is_running", False)
        radar_msg = radar_st.get("message", "Sẵn sàng")

        poster_st = self.get_poster_status()
        is_poster_running = poster_st.get("is_running", False)
        poster_msg = poster_st.get("current_status_msg", "Đang rảnh")

        target_profile = db.get_setting("radar_target_profile", "ALL")
        auto_radar = "BẬT" if db.get_setting("lead_radar_enabled", "0") == "1" else "TẮT"

        msg = (
            f"🤖 <b>[FB AUTO POSTER & RADAR CONTROLLER]</b>\n"
            f"⏰ <b>Thời gian:</b> <code>{now_str}</code>\n"
            f"💻 <b>Hệ thống:</b> CPU: {cpu_usage}% | RAM: {ram_usage}%\n"
            f"👥 <b>Tài khoản:</b> {live_accs}/{len(accounts)} LIVE ({sentry_accs} Sentry Cooldown)\n"
            f"🎯 <b>Khách tìm hôm nay:</b> <b>{today_leads}</b> leads\n"
            f"📡 <b>Radar:</b> {'🟢 Đang chạy' if is_radar_running else '⚪ Đang tắt'} (Auto: {auto_radar} | Profile: {target_profile})\n"
            f"📢 <b>Poster:</b> {'🟢 Đang chạy' if is_poster_running else '⚪ Đang dừng'}\n\n"
            f"<i>👉 Bấm các nút bên dưới để điều khiển trực tiếp máy ở nhà:</i>"
        )
        return msg

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_admin(update):
            if update.message:
                await update.message.reply_text("⛔ Bạn không có quyền truy cập hệ thống này.")
            return
        await update.message.reply_text(
            self.get_dashboard_text(),
            reply_markup=self.build_main_menu_keyboard(),
            parse_mode="HTML"
        )

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not check_admin(update):
            await query.answer("⛔ Bạn không có quyền truy cập!", show_alert=True)
            return

        data = query.data

        if data == "noop":
            await query.answer("Đã ghi nhận trạng thái trước đó.", show_alert=False)
            return

        if data == "refresh_dashboard":
            await query.answer("🔄 Đã làm mới Dashboard!")
            await query.edit_message_text(
                self.get_dashboard_text(),
                reply_markup=self.build_main_menu_keyboard(),
                parse_mode="HTML"
            )

        elif data == "toggle_poster":
            toggle_poster_fn = self.app_state.get("trigger_poster_toggle")
            if toggle_poster_fn:
                try:
                    res = toggle_poster_fn()
                    if asyncio.iscoroutine(res):
                        await res
                    await query.answer("Đã chuyển trạng thái Poster!", show_alert=False)
                except Exception as e:
                    await query.answer(f"Lỗi: {e}", show_alert=True)
            else:
                await query.answer("Chức năng Poster chưa được cấu hình.", show_alert=True)

            await query.edit_message_text(
                self.get_dashboard_text(),
                reply_markup=self.build_main_menu_keyboard(),
                parse_mode="HTML"
            )

        elif data == "toggle_radar":
            toggle_radar_fn = self.app_state.get("trigger_radar_toggle")
            if toggle_radar_fn:
                try:
                    res = toggle_radar_fn()
                    if asyncio.iscoroutine(res):
                        await res
                    await query.answer("Đã chuyển trạng thái Radar!", show_alert=False)
                except Exception as e:
                    await query.answer(f"Lỗi: {e}", show_alert=True)
            else:
                # Fallback: toggle auto radar setting
                current_enabled = db.get_setting("lead_radar_enabled", "0") == "1"
                db.set_setting("lead_radar_enabled", "0" if current_enabled else "1")
                await query.answer(f"Đã {'TẮT' if current_enabled else 'BẬT'} Radar tự động!", show_alert=False)

            await query.edit_message_text(
                self.get_dashboard_text(),
                reply_markup=self.build_main_menu_keyboard(),
                parse_mode="HTML"
            )

        elif data == "menu_radar":
            await query.answer()
            cur_profile = db.get_setting("radar_target_profile", "ALL")
            cur_hours = db.get_setting("lead_radar_max_hours", "24")
            auto_enabled = db.get_setting("lead_radar_enabled", "0") == "1"

            auto_btn_text = "🟢 Auto Radar: ĐANG BẬT" if auto_enabled else "⚪ Auto Radar: ĐANG TẮT"

            radar_kb = [
                [
                    InlineKeyboardButton("⚡ QUÉT TẤT CẢ NGAY", callback_data="radar_scan_now"),
                    InlineKeyboardButton(auto_btn_text, callback_data="toggle_auto_radar")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='VIETNAMESE' else ''}🇻🇳 Khách Việt", callback_data="profile_vn"),
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='EXPAT' else ''}🌍 Khách Tây", callback_data="profile_expat"),
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='ALL' else ''}⚡ Cả Hai", callback_data="profile_all")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='12' else ''}⏳ 12h qua", callback_data="time_12h"),
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='24' else ''}⏳ 24h qua", callback_data="time_24h"),
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='48' else ''}⏳ 48h qua", callback_data="time_48h")
                ],
                [
                    InlineKeyboardButton("⬅️ Quay lại Menu Chính", callback_data="refresh_dashboard")
                ]
            ]
            await query.edit_message_text(
                f"🎯 <b>[QUẢN LÝ RADAR KHÁCH HÀNG]</b>\n"
                f"• <b>Mục tiêu hiện tại:</b> <code>{cur_profile}</code>\n"
                f"• <b>Mốc thời gian:</b> <code>{cur_hours} giờ qua</code>\n"
                f"• <b>Tự động quét định kỳ:</b> <code>{'BẬT' if auto_enabled else 'TẮT'}</code>\n\n"
                f"<i>Chọn thao tác bên dưới:</i>",
                reply_markup=InlineKeyboardMarkup(radar_kb),
                parse_mode="HTML"
            )

        elif data == "toggle_auto_radar":
            current_enabled = db.get_setting("lead_radar_enabled", "0") == "1"
            new_val = "0" if current_enabled else "1"
            db.set_setting("lead_radar_enabled", new_val)
            await query.answer(f"Đã {'TẮT' if current_enabled else 'BẬT'} Radar tự động!", show_alert=False)

            # Re-render menu_radar
            cur_profile = db.get_setting("radar_target_profile", "ALL")
            cur_hours = db.get_setting("lead_radar_max_hours", "24")
            auto_btn_text = "🟢 Auto Radar: ĐANG BẬT" if new_val == "1" else "⚪ Auto Radar: ĐANG TẮT"

            radar_kb = [
                [
                    InlineKeyboardButton("⚡ QUÉT TẤT CẢ NGAY", callback_data="radar_scan_now"),
                    InlineKeyboardButton(auto_btn_text, callback_data="toggle_auto_radar")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='VIETNAMESE' else ''}🇻🇳 Khách Việt", callback_data="profile_vn"),
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='EXPAT' else ''}🌍 Khách Tây", callback_data="profile_expat"),
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='ALL' else ''}⚡ Cả Hai", callback_data="profile_all")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='12' else ''}⏳ 12h qua", callback_data="time_12h"),
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='24' else ''}⏳ 24h qua", callback_data="time_24h"),
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='48' else ''}⏳ 48h qua", callback_data="time_48h")
                ],
                [
                    InlineKeyboardButton("⬅️ Quay lại Menu Chính", callback_data="refresh_dashboard")
                ]
            ]
            await query.edit_message_text(
                f"🎯 <b>[QUẢN LÝ RADAR KHÁCH HÀNG]</b>\n"
                f"• <b>Mục tiêu hiện tại:</b> <code>{cur_profile}</code>\n"
                f"• <b>Mốc thời gian:</b> <code>{cur_hours} giờ qua</code>\n"
                f"• <b>Tự động quét định kỳ:</b> <code>{'BẬT' if new_val=='1' else 'TẮT'}</code>\n\n"
                f"<i>Chọn thao tác bên dưới:</i>",
                reply_markup=InlineKeyboardMarkup(radar_kb),
                parse_mode="HTML"
            )

        elif data in ["profile_vn", "profile_expat", "profile_all"]:
            val_map = {"profile_vn": "VIETNAMESE", "profile_expat": "EXPAT", "profile_all": "ALL"}
            sel = val_map[data]
            db.set_setting("radar_target_profile", sel)
            await query.answer(f"Đã đặt mục tiêu quét: {sel}", show_alert=False)

            # Re-render menu_radar
            cur_hours = db.get_setting("lead_radar_max_hours", "24")
            auto_enabled = db.get_setting("lead_radar_enabled", "0") == "1"
            auto_btn_text = "🟢 Auto Radar: ĐANG BẬT" if auto_enabled else "⚪ Auto Radar: ĐANG TẮT"

            radar_kb = [
                [
                    InlineKeyboardButton("⚡ QUÉT TẤT CẢ NGAY", callback_data="radar_scan_now"),
                    InlineKeyboardButton(auto_btn_text, callback_data="toggle_auto_radar")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if sel=='VIETNAMESE' else ''}🇻🇳 Khách Việt", callback_data="profile_vn"),
                    InlineKeyboardButton(f"{'✅ ' if sel=='EXPAT' else ''}🌍 Khách Tây", callback_data="profile_expat"),
                    InlineKeyboardButton(f"{'✅ ' if sel=='ALL' else ''}⚡ Cả Hai", callback_data="profile_all")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='12' else ''}⏳ 12h qua", callback_data="time_12h"),
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='24' else ''}⏳ 24h qua", callback_data="time_24h"),
                    InlineKeyboardButton(f"{'✅ ' if cur_hours=='48' else ''}⏳ 48h qua", callback_data="time_48h")
                ],
                [
                    InlineKeyboardButton("⬅️ Quay lại Menu Chính", callback_data="refresh_dashboard")
                ]
            ]
            await query.edit_message_text(
                f"🎯 <b>[QUẢN LÝ RADAR KHÁCH HÀNG]</b>\n"
                f"• <b>Mục tiêu hiện tại:</b> <code>{sel}</code>\n"
                f"• <b>Mốc thời gian:</b> <code>{cur_hours} giờ qua</code>\n"
                f"• <b>Tự động quét định kỳ:</b> <code>{'BẬT' if auto_enabled else 'TẮT'}</code>\n\n"
                f"<i>Chọn thao tác bên dưới:</i>",
                reply_markup=InlineKeyboardMarkup(radar_kb),
                parse_mode="HTML"
            )

        elif data in ["time_12h", "time_24h", "time_48h"]:
            val_map = {"time_12h": "12", "time_24h": "24", "time_48h": "48"}
            sel_hours = val_map[data]
            db.set_setting("lead_radar_max_hours", sel_hours)
            await query.answer(f"Đã đặt mốc thời gian: {sel_hours}h qua", show_alert=False)

            # Re-render menu_radar
            cur_profile = db.get_setting("radar_target_profile", "ALL")
            auto_enabled = db.get_setting("lead_radar_enabled", "0") == "1"
            auto_btn_text = "🟢 Auto Radar: ĐANG BẬT" if auto_enabled else "⚪ Auto Radar: ĐANG TẮT"

            radar_kb = [
                [
                    InlineKeyboardButton("⚡ QUÉT TẤT CẢ NGAY", callback_data="radar_scan_now"),
                    InlineKeyboardButton(auto_btn_text, callback_data="toggle_auto_radar")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='VIETNAMESE' else ''}🇻🇳 Khách Việt", callback_data="profile_vn"),
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='EXPAT' else ''}🌍 Khách Tây", callback_data="profile_expat"),
                    InlineKeyboardButton(f"{'✅ ' if cur_profile=='ALL' else ''}⚡ Cả Hai", callback_data="profile_all")
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if sel_hours=='12' else ''}⏳ 12h qua", callback_data="time_12h"),
                    InlineKeyboardButton(f"{'✅ ' if sel_hours=='24' else ''}⏳ 24h qua", callback_data="time_24h"),
                    InlineKeyboardButton(f"{'✅ ' if sel_hours=='48' else ''}⏳ 48h qua", callback_data="time_48h")
                ],
                [
                    InlineKeyboardButton("⬅️ Quay lại Menu Chính", callback_data="refresh_dashboard")
                ]
            ]
            await query.edit_message_text(
                f"🎯 <b>[QUẢN LÝ RADAR KHÁCH HÀNG]</b>\n"
                f"• <b>Mục tiêu hiện tại:</b> <code>{cur_profile}</code>\n"
                f"• <b>Mốc thời gian:</b> <code>{sel_hours} giờ qua</code>\n"
                f"• <b>Tự động quét định kỳ:</b> <code>{'BẬT' if auto_enabled else 'TẮT'}</code>\n\n"
                f"<i>Chọn thao tác bên dưới:</i>",
                reply_markup=InlineKeyboardMarkup(radar_kb),
                parse_mode="HTML"
            )

        elif data == "radar_scan_now":
            await query.answer("🚀 Đang khởi động quét Radar...", show_alert=False)
            scan_fn = self.app_state.get("trigger_radar_scan")
            if scan_fn:
                try:
                    res = scan_fn()
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                    await query.edit_message_text(
                        "🚀 <b>Đã kích hoạt quét Radar toàn bộ nhóm!</b>\n"
                        "Tiến trình đang chạy ngầm trên máy. Khi tìm thấy bài viết của khách hàng mới, bot sẽ lập tức gửi thông báo kèm nút bấm về đây.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu Chính", callback_data="refresh_dashboard")]]),
                        parse_mode="HTML"
                    )
                except Exception as e:
                    await query.edit_message_text(
                        f"❌ Lỗi khởi chạy Radar: {e}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu Chính", callback_data="refresh_dashboard")]])
                    )
            else:
                await query.edit_message_text(
                    "❌ Không tìm thấy callback kích hoạt Radar.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu Chính", callback_data="refresh_dashboard")]])
                )

        elif data.startswith("lead_contacted_"):
            post_id = data.replace("lead_contacted_", "")
            db.update_lead_status(post_id, "CONTACTED")
            await query.answer("✅ Đã cập nhật: ĐÃ LIÊN HỆ!", show_alert=False)
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ ĐÃ LIÊN HỆ THÀNH CÔNG", callback_data="noop")]
                ])
            )

        elif data.startswith("lead_ignored_"):
            post_id = data.replace("lead_ignored_", "")
            db.update_lead_status(post_id, "IGNORED")
            await query.answer("❌ Đã cập nhật: BỎ QUA!", show_alert=False)
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ ĐÃ BỎ QUA LEAD NÀY", callback_data="noop")]
                ])
            )

        elif data == "menu_clones":
            await query.answer()
            accounts = db.get_all_accounts()
            lines = ["👥 <b>[DANH SÁCH TÀI KHOẢN CLONE]</b>\n"]
            if not accounts:
                lines.append("<i>Chưa có tài khoản clone nào trong cơ sở dữ liệu.</i>")
            else:
                for a in accounts:
                    status = a.get("status", "LIVE")
                    if status == "LIVE":
                        icon = "🟢"
                    elif status == "SENTRY_COOLDOWN":
                        icon = "🛡️"
                    else:
                        icon = "🔴"

                    uid = a.get("uid") or a.get("id") or "?"
                    name = html.escape(str(a.get("name") or "Clone"))
                    cooldown = f" (Mở lại: <code>{a.get('rate_limited_until')}</code>)" if a.get("rate_limited_until") else ""
                    lines.append(f"{icon} <b>{name}</b> (<code>{uid}</code>){cooldown}")

            clones_kb = [
                [InlineKeyboardButton("🔄 Làm mới", callback_data="menu_clones")],
                [InlineKeyboardButton("⬅️ Quay lại", callback_data="refresh_dashboard")]
            ]
            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(clones_kb),
                parse_mode="HTML"
            )

        elif data == "menu_report":
            await query.answer()
            accounts = db.get_all_accounts()
            live_accs = sum(1 for a in accounts if a.get("status") == "LIVE")
            sentry_accs = sum(1 for a in accounts if a.get("status") == "SENTRY_COOLDOWN")
            dead_accs = sum(1 for a in accounts if a.get("status") not in ["LIVE", "SENTRY_COOLDOWN"])

            lead_stats = db.get_lead_stats()
            today_start = get_vn_now().replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
            all_leads = db.get_leads(limit=500)
            today_leads = sum(1 for l in all_leads if str(l.get("created_time") or "") >= today_start)

            history = db.get_post_history(limit=500)
            today_posts = sum(1 for h in history if str(h.get("created_time") or "") >= today_start)
            today_success = sum(1 for h in history if str(h.get("created_time") or "") >= today_start and h.get("status") == "SUCCESS")

            try:
                cpu_usage = psutil.cpu_percent(interval=None)
                ram = psutil.virtual_memory()
                ram_str = f"{ram.percent}% ({round(ram.used / 1024 / 1024 / 1024, 1)}/{round(ram.total / 1024 / 1024 / 1024, 1)} GB)"
            except Exception:
                cpu_usage = 0.0
                ram_str = "N/A"

            report_msg = (
                f"📊 <b>[BÁO CÁO NHANH HỆ THỐNG]</b>\n"
                f"⏰ <i>{get_vn_now().strftime('%H:%M:%S %d/%m/%Y')}</i>\n\n"
                f"💻 <b>Tài nguyên máy tính:</b>\n"
                f"• CPU: <code>{cpu_usage}%</code>\n"
                f"• RAM: <code>{ram_str}</code>\n\n"
                f"👥 <b>Tài khoản Clone Facebook:</b>\n"
                f"• 🟢 Live: <b>{live_accs}</b> | 🛡️ Sentry: <b>{sentry_accs}</b> | 🔴 Dead: <b>{dead_accs}</b>\n\n"
                f"🎯 <b>Lead Radar (Khách tìm phòng):</b>\n"
                f"• Hôm nay: <b>{today_leads}</b> khách mới\n"
                f"• Tổng lead lưu trữ: <b>{lead_stats.get('total', 0)}</b> leads\n"
                f"• Khách có SĐT: <b>{lead_stats.get('has_phone', 0)}</b>\n"
                f"• Phân loại: 🇻🇳 {lead_stats.get('vietnamese', 0)} Việt | 🌍 {lead_stats.get('expat', 0)} Expat\n\n"
                f"📢 <b>Đăng bài hôm nay:</b>\n"
                f"• Đã đăng: <b>{today_posts}</b> bài (Thành công: <b>{today_success}</b>)"
            )

            report_kb = [
                [InlineKeyboardButton("🔄 Cập nhật số liệu", callback_data="menu_report")],
                [InlineKeyboardButton("⬅️ Quay lại Menu Chính", callback_data="refresh_dashboard")]
            ]
            await query.edit_message_text(
                report_msg,
                reply_markup=InlineKeyboardMarkup(report_kb),
                parse_mode="HTML"
            )

    async def send_lead_alert(self, lead: dict):
        """Gửi thông báo có khách mới kèm nút thao tác xử lý trực tiếp"""
        if not self.application:
            return

        author_name = html.escape(str(lead.get("author_name") or "Người dùng Facebook"))
        author_url = lead.get("author_url")
        if author_url and str(author_url).strip():
            author_display = f'<a href="{html.escape(str(author_url))}">{author_name}</a>'
        else:
            author_display = f'<b>{author_name}</b> (Ẩn danh)'

        post_url = lead.get("post_url") or "https://facebook.com"
        group_name = html.escape(str(lead.get("group_name") or "Hội nhóm"))
        content = html.escape(str(lead.get("content") or ""))[:350]
        phone = lead.get("phone")
        phone_line = f"\n📞 <b>Số điện thoại:</b> <code>{html.escape(str(phone))}</code>" if phone else ""
        matched_kw = html.escape(str(lead.get("matched_keywords") or ""))
        created_time = html.escape(str(lead.get("created_time") or "Mới đây"))
        post_id = str(lead.get("post_id") or lead.get("id") or "")

        client_type = str(lead.get("client_type") or "VIETNAMESE").upper()
        if client_type == "EXPAT":
            type_tag = "\n🌍 <b>Loại khách:</b> 🇬🇧 KHÁCH NƯỚC NGOÀI (EXPAT)"
        else:
            type_tag = "\n👤 <b>Loại khách:</b> 🇻🇳 Khách Việt Nam"

        msg = (
            f"🎯 <b>[CÓ KHÁCH HÀNG TÌM THUÊ MỚI!]</b>\n"
            f"👤 <b>Khách:</b> {author_display}{type_tag}\n"
            f"🏢 <b>Nhóm:</b> {group_name}\n"
            f"⏰ <b>Đăng lúc:</b> {created_time}\n"
            f"🔑 <b>Từ khóa:</b> <code>{matched_kw}</code>{phone_line}\n\n"
            f"📝 <b>Nội dung:</b>\n<i>{content}...</i>"
        )

        buttons = [
            [InlineKeyboardButton("🔗 Mở Bài Viết Facebook", url=post_url)],
            [
                InlineKeyboardButton("✅ Đã Liên Hệ", callback_data=f"lead_contacted_{post_id}"),
                InlineKeyboardButton("❌ Bỏ Qua", callback_data=f"lead_ignored_{post_id}")
            ]
        ]

        try:
            await self.application.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=msg,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML",
                disable_web_page_preview=False
            )
        except Exception as e:
            print(f"[TELEGRAM_BOT] ⚠️ Lỗi gửi tin nhắn lead: {e}")

    def run_polling(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            self.application = Application.builder().token(BOT_TOKEN).build()
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("menu", self.start_command))
            self.application.add_handler(CallbackQueryHandler(self.callback_handler))

            print(f"[TELEGRAM_BOT] 🤖 Telegram Remote Controller đã kích hoạt Long-Polling (Admin ID: {ADMIN_CHAT_ID}).")
            self.application.run_polling(close_loop=False, stop_signals=None)
        except Exception as e:
            print(f"[TELEGRAM_BOT] ❌ Lỗi khởi chạy polling bot: {e}")

    def start_in_background(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_polling, daemon=True, name="TelegramBotDaemon")
        self._thread.start()
