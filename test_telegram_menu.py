import asyncio
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from core.telegram_bot import BOT_TOKEN, ADMIN_CHAT_ID, TelegramRemoteDaemon
import database as db


async def test_send_menu():
    print(f"[TEST] 🤖 Đang kết nối gửi tin nhắn Menu tới Admin Chat ID: {ADMIN_CHAT_ID}...")
    
    daemon = TelegramRemoteDaemon({
        "radar_status": {"is_running": False, "message": "Sẵn sàng"},
        "poster_status": {"is_running": False, "current_status_msg": "Đang rảnh"}
    })
    
    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"[TEST] Bot xác thực thành công: @{me.username} ({me.first_name}) [ID: {me.id}]")
    
    # 1. Gửi Dashboard Menu mẫu
    dashboard_text = daemon.get_dashboard_text()
    keyboard = daemon.build_main_menu_keyboard()
    
    msg1 = await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"🧪 <b>[KIỂM THỬ GIAO DIỆN ĐIỀU KHIỂN TỪ XA TELEGRAM]</b>\n\n{dashboard_text}",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    print(f"[TEST] ✅ Đã gửi thành công tin nhắn Menu Dashboard (Message ID: {msg1.message_id})")

    # 2. Gửi thử 1 thông báo Lead alert mẫu kèm nút bấm
    test_lead = {
        "post_id": "test_post_sample_123456",
        "author_name": "Nguyễn Văn Thử Nghiệm",
        "author_url": "https://facebook.com",
        "group_name": "Hội Thuê Nhà Trọ & Căn Hộ Hà Nội",
        "content": "Mình cần tìm gấp phòng trọ/chung cư mini khu vực Cầu Giấy hoặc Đống Đa, tài chính tầm 4 - 5.5 triệu/tháng. Yêu cầu có thang máy, ban công thoáng và máy giặt riêng. Ai có phòng gửi ảnh và video qua zalo giúp mình nhé!",
        "phone": "0987654321",
        "matched_keywords": "cần tìm gấp, phòng trọ, tài chính",
        "created_time": "Vừa xong",
        "client_type": "VIETNAMESE"
    }
    
    daemon.application = type("DummyApp", (), {"bot": bot})()
    await daemon.send_lead_alert(test_lead)
    print("[TEST] ✅ Đã gửi thành công tin nhắn thông báo Khách Hàng mới (Lead Alert) kèm 2 nút bấm thao tác!")


if __name__ == "__main__":
    asyncio.run(test_send_menu())
