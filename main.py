import os
import sys
import json
import asyncio
import functools
import random
import uuid
import shutil
import traceback
import atexit
import datetime
import html
import threading
import time
import webbrowser
import csv
import io
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Query, Response, Body
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
from core.utils import parse_spintax, VN_TZ, get_vn_now, get_vn_time_str
from core.poster import publish_post_via_cookie, check_cookie_live, DEFAULT_STORY_CREATE_DOC_ID
from core.scanner import stream_search_groups_by_keywords, fetch_joined_groups_by_cookie
from core.dispatcher import generate_posting_matrix, CampaignState, CampaignController
from core.crosspost import get_eligible_crosspost_groups, chunk_groups_for_crossposting
from core.power import power_manager
from core.network import check_internet_connection
from core.telegram import telegram_notifier
from core.telegram_bot import TelegramRemoteDaemon
from core.lead_scanner import (
    LeadScanner, GroupLeadScanner, RadarController,
    DEFAULT_WHITELIST, DEFAULT_BLACKLIST,
    DEFAULT_WHITELIST_VN, DEFAULT_BLACKLIST_VN,
    DEFAULT_WHITELIST_EN, DEFAULT_BLACKLIST_EN
)

# Auto-cleanup on exit
atexit.register(power_manager.stop)
atexit.register(telegram_notifier.stop_alarm)

# ANSI Colors for Terminal
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_BLUE = "\033[94m"
C_CYAN = "\033[96m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_MAGENTA = "\033[95m"

# Cấu hình đường dẫn tài nguyên: hỗ trợ cả khi chạy script thường và khi đóng gói PyInstaller (_MEIPASS)
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    APP_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = Path(__file__).resolve().parent

# Thư mục giao diện static lấy từ gói bundle
STATIC_DIR = BUNDLE_DIR / "static"

# Thư mục dữ liệu uploads & CSDL lưu bên ngoài máy người dùng (bền vững)
UPLOADS_DIR = APP_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = str(UPLOADS_DIR)  # Giữ tương thích ngược với code nội bộ main.py

STATIC_DIR.mkdir(parents=True, exist_ok=True)


def resolve_image_paths(paths: Optional[List[str]], upload_dir: str = UPLOAD_DIR) -> List[str]:
    """Resolves a list of relative or absolute image file paths to valid existing files on disk."""
    if not paths:
        return []
    resolved = []
    for p in paths:
        if not p or not isinstance(p, str):
            continue
        p = p.strip()
        if os.path.isabs(p) and os.path.exists(p):
            resolved.append(p)
        else:
            cand = os.path.join(upload_dir, os.path.basename(p))
            if os.path.exists(cand):
                resolved.append(cand)
            elif os.path.exists(p):
                resolved.append(os.path.abspath(p))
    return resolved


global_radar_controller = RadarController()
radar_status = {
    "is_running": False,
    "state": "IDLE",  # IDLE, RUNNING, PAUSED, STOPPED
    "total_groups": 0,
    "scanned_groups": 0,
    "matched_leads": 0,
    "new_leads": 0,
    "current_group": "",
    "message": "Sẵn sàng"
}
radar_lock = threading.Lock()


async def run_lead_radar_iteration(
    group_ids: Optional[List[str]] = None,
    max_hours: Optional[int] = None,
    account_id: Optional[int] = None,
    target_profile: Optional[str] = None,
    custom_whitelist_vn: Optional[List[str]] = None,
    custom_blacklist_vn: Optional[List[str]] = None,
    custom_whitelist_en: Optional[List[str]] = None,
    custom_blacklist_en: Optional[List[str]] = None,
    custom_whitelist: Optional[List[str]] = None,
    custom_blacklist: Optional[List[str]] = None,
    send_telegram: bool = True,
    max_posts: Optional[int] = None,
    max_workers: int = 4
) -> Dict[str, Any]:
    """Runs a single pass of lead radar scan across target groups and alerts for new leads."""
    cookie_str = ""
    proxy = None

    if account_id:
        acc = db.get_account_by_id(account_id)
        if acc and acc.get("cookie"):
            cookie_str = acc["cookie"]
            proxy = acc.get("proxy")

    if not cookie_str:
        accounts = db.get_accounts(status="LIVE")
        available_accs = [a for a in accounts if not a.get("is_rate_limited") and a.get("has_cookie")]
        if not available_accs:
            available_accs = [a for a in accounts if a.get("has_cookie")]
        if not available_accs:
            return {
                "success": False,
                "message": "Không có tài khoản có cookie nào khả dụng để quét nhóm",
                "new_count": 0,
                "scanned_groups": 0,
                "total_leads_matched": 0,
                "new_leads_saved": 0,
                "leads": []
            }
        chosen_acc = random.choice(available_accs)
        cookie_str = chosen_acc.get("cookie") or ""
        proxy = chosen_acc.get("proxy")

    # Xác định danh sách group_ids mục tiêu
    target_group_ids: List[str] = []
    if group_ids and isinstance(group_ids, list):
        target_group_ids = [str(g).strip() for g in group_ids if str(g).strip()]

    if not target_group_ids:
        # 1. Thử lấy từ setting radar_group_ids
        raw_radar_gids = db.get_setting("radar_group_ids", "")
        if raw_radar_gids:
            try:
                parsed_gids = json.loads(raw_radar_gids)
                if isinstance(parsed_gids, list):
                    target_group_ids = [str(g).strip() for g in parsed_gids if str(g).strip()]
            except Exception:
                target_group_ids = [g.strip() for g in raw_radar_gids.split("\n") if g.strip()]

    if not target_group_ids:
        # 2. Thử lấy từ tất cả presets trong DB
        presets = db.get_presets()
        for p in presets:
            g_list = p.get("group_ids", [])
            if isinstance(g_list, list):
                for g in g_list:
                    if str(g).strip() and str(g).strip() not in target_group_ids:
                        target_group_ids.append(str(g).strip())

    if not target_group_ids:
        # 3. Thử lấy từ group_profiles
        profiles = db.get_group_profiles()
        for prof in profiles:
            gid = str(prof.get("group_id", "")).strip()
            if gid and gid not in target_group_ids:
                target_group_ids.append(gid)

    if not target_group_ids:
        target_group_ids = ["1090221531934443", "248622851277141", "682623845225623"]

    if max_hours is None:
        try:
            max_hours = int(db.get_setting("lead_radar_max_hours", "24"))
        except Exception:
            max_hours = 24

    if not target_profile:
        target_profile = db.get_setting("radar_target_profile", "ALL")

    if custom_whitelist_vn is not None:
        whitelist_vn = custom_whitelist_vn
    elif custom_whitelist is not None:
        whitelist_vn = custom_whitelist
    else:
        raw_wl_vn = db.get_setting("radar_whitelist_vn", "") or db.get_setting("lead_radar_whitelist", "")
        try:
            whitelist_vn = json.loads(raw_wl_vn) if raw_wl_vn else DEFAULT_WHITELIST_VN
        except Exception:
            whitelist_vn = DEFAULT_WHITELIST_VN

    if custom_blacklist_vn is not None:
        blacklist_vn = custom_blacklist_vn
    elif custom_blacklist is not None:
        blacklist_vn = custom_blacklist
    else:
        raw_bl_vn = db.get_setting("radar_blacklist_vn", "") or db.get_setting("lead_radar_blacklist", "")
        try:
            blacklist_vn = json.loads(raw_bl_vn) if raw_bl_vn else DEFAULT_BLACKLIST_VN
        except Exception:
            blacklist_vn = DEFAULT_BLACKLIST_VN

    if custom_whitelist_en is not None:
        whitelist_en = custom_whitelist_en
    else:
        raw_wl_en = db.get_setting("radar_whitelist_en", "")
        try:
            whitelist_en = json.loads(raw_wl_en) if raw_wl_en else DEFAULT_WHITELIST_EN
        except Exception:
            whitelist_en = DEFAULT_WHITELIST_EN

    if custom_blacklist_en is not None:
        blacklist_en = custom_blacklist_en
    else:
        raw_bl_en = db.get_setting("radar_blacklist_en", "")
        try:
            blacklist_en = json.loads(raw_bl_en) if raw_bl_en else DEFAULT_BLACKLIST_EN
        except Exception:
            blacklist_en = DEFAULT_BLACKLIST_EN

    tg_enabled = db.get_setting("telegram_enabled", "1") == "1"
    main_loop = asyncio.get_running_loop()

    global_radar_controller.reset()
    with radar_lock:
        radar_status["is_running"] = True
        radar_status["state"] = "RUNNING"
        radar_status["total_groups"] = len(target_group_ids)
        radar_status["scanned_groups"] = 0
        radar_status["matched_leads"] = 0
        radar_status["new_leads"] = 0
        radar_status["current_group"] = ""
        radar_status["message"] = f"Đang quét song song {len(target_group_ids)} nhóm [Profile: {target_profile}]..."

    def _on_lead_found(lead: Dict[str, Any]):
        try:
            inserted = db.insert_lead(lead)
            with radar_lock:
                radar_status["matched_leads"] += 1
                if inserted:
                    radar_status["new_leads"] += 1

            if inserted and 'telegram_daemon' in globals() and telegram_daemon and getattr(telegram_daemon, "_loop", None) and telegram_daemon._loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(
                        telegram_daemon.send_lead_alert(lead),
                        telegram_daemon._loop
                    )
                except Exception as tg_daemon_err:
                    print(f"[RADAR] ⚠️ Lỗi gửi Telegram bot daemon lead: {tg_daemon_err}")

            if inserted and send_telegram and tg_enabled:
                author_name = html.escape(str(lead.get("author_name") or "Người dùng Facebook"))
                author_url = lead.get("author_url")
                if author_url and str(author_url).strip():
                    author_display = f'<a href="{html.escape(str(author_url))}">{author_name}</a>'
                else:
                    author_display = f'<b>{author_name}</b> (Ẩn danh)'

                post_url = html.escape(str(lead.get("post_url") or "#"))
                group_name = html.escape(str(lead.get("group_name") or "Hội nhóm"))
                content = html.escape(str(lead.get("content") or ""))[:350]
                matched_kw = html.escape(str(lead.get("matched_keywords") or ""))
                created_time = html.escape(str(lead.get("created_time") or "Mới đây"))
                phone = lead.get("phone")
                phone_display = f"\n📞 <b>Số điện thoại:</b> <code>{html.escape(str(phone))}</code>" if phone else ""

                client_type = str(lead.get("client_type") or "VIETNAMESE").upper()
                if client_type == "EXPAT":
                    client_type_display = "\n🌍 <b>Loại khách:</b> 🇬🇧 KHÁCH NƯỚC NGOÀI (EXPAT)"
                else:
                    client_type_display = "\n👤 <b>Loại khách:</b> 🇻🇳 Khách Việt Nam"

                tg_msg = (
                    f"🎯 <b>[CÓ KHÁCH HÀNG TÌM THUÊ MỚI!]</b>\n"
                    f"👤 <b>Khách:</b> {author_display}{client_type_display}\n"
                    f"🏢 <b>Nhóm:</b> {group_name}\n"
                    f"⏰ <b>Đăng lúc:</b> {created_time}\n"
                    f"🔑 <b>Từ khóa khớp:</b> <code>{matched_kw}</code>{phone_display}\n\n"
                    f"📝 <b>Nội dung:</b>\n<i>{content}...</i>\n\n"
                    f"🔗 <a href=\"{post_url}\">👉 BẤM ĐỂ MỞ BÀI VIẾT VÀ BÌNH LUẬN</a>"
                )
                try:
                    if main_loop and main_loop.is_running():
                        main_loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(telegram_notifier.send_message_async(tg_msg))
                        )
                    else:
                        telegram_notifier.send_message(tg_msg)
                except Exception as tg_err:
                    print(f"[RADAR] ⚠️ Lỗi gửi Telegram lead: {tg_err}")
        except Exception as e:
            print(f"[RADAR] ⚠️ Lỗi xử lý callback lead: {e}")

    def _on_group_completed(gid: str, completed: int, total: int, g_leads: list):
        with radar_lock:
            radar_status["scanned_groups"] = completed
            radar_status["current_group"] = str(gid)
            if global_radar_controller.is_paused():
                radar_status["message"] = f"⏸️ Đang tạm dừng quét [{completed}/{total} nhóm]..."
            elif global_radar_controller.is_stopped():
                radar_status["message"] = f"⏹️ Đã dừng quét [{completed}/{total} nhóm]."
            else:
                radar_status["message"] = f"Đang quét song song... [{completed}/{total}] nhóm hoàn tất ({radar_status['new_leads']} khách mới)"

    try:
        scanner = GroupLeadScanner(cookie_str=cookie_str, proxy=proxy)
        leads = await asyncio.to_thread(
            scanner.scan_group_list,
            group_ids=target_group_ids,
            max_hours=max_hours,
            target_profile=target_profile,
            whitelist_vn=whitelist_vn,
            blacklist_vn=blacklist_vn,
            whitelist_en=whitelist_en,
            blacklist_en=blacklist_en,
            max_workers=max_workers,
            on_lead_found=_on_lead_found,
            on_group_completed=_on_group_completed,
            controller=global_radar_controller
        )
    finally:
        with radar_lock:
            radar_status["is_running"] = False
            if global_radar_controller.is_stopped():
                radar_status["state"] = "STOPPED"
                radar_status["message"] = f"Đã dừng quét: {radar_status['scanned_groups']}/{len(target_group_ids)} nhóm ({radar_status['matched_leads']} bài phù hợp, {radar_status['new_leads']} khách mới)."
            else:
                radar_status["state"] = "IDLE"
                radar_status["scanned_groups"] = len(target_group_ids)
                radar_status["message"] = f"Quét hoàn tất {len(target_group_ids)} nhóm: Tìm thấy {radar_status['matched_leads']} bài phù hợp ({radar_status['new_leads']} khách mới)."

    new_count = radar_status["new_leads"]
    if new_count > 0:
        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}🎯 [RADAR] Đã tìm thấy {new_count} khách tìm thuê mới từ {len(target_group_ids)} nhóm!{C_RESET}")

    return {
        "success": True,
        "scanned_groups": len(target_group_ids),
        "total_leads_matched": len(leads),
        "new_leads_saved": new_count,
        "new_count": new_count,
        "max_hours": max_hours,
        "leads": leads,
        "message": f"Quét hoàn tất {len(target_group_ids)} nhóm: Tìm thấy {len(leads)} bài phù hợp ({new_count} khách mới)."
    }


async def lead_radar_loop():
    """Background scheduler loop for rental seeker radar."""
    await asyncio.sleep(5)
    while True:
        try:
            enabled = db.get_setting("lead_radar_enabled", "0") == "1"
            if enabled:
                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_CYAN}🔍 [RADAR] Đang chạy chu kỳ quét khách tìm thuê định kỳ...{C_RESET}")
                await run_lead_radar_iteration(send_telegram=True)

            try:
                interval_min = int(db.get_setting("lead_radar_interval_minutes", "30"))
            except Exception:
                interval_min = 30
            interval_sec = max(60, interval_min * 60)

            for _ in range(interval_sec):
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[RADAR] Ngoại lệ vòng lặp radar: {e}")
            await asyncio.sleep(60)


lead_radar_task: Optional[asyncio.Task] = None
_browser_opened = False


def open_browser_delayed(url: str = "http://127.0.0.1:8000", delay: float = 1.5):
    global _browser_opened
    if _browser_opened:
        return
    _browser_opened = True
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global lead_radar_task
    db.init_db()
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}{C_BOLD}[SYSTEM] FB-TOOL1 Pure Cookie Engine đã khởi động thành công.{C_RESET}")
    lead_radar_task = asyncio.create_task(lead_radar_loop())
    threading.Thread(target=open_browser_delayed, daemon=True).start()
    telegram_daemon.start_in_background()
    yield
    if lead_radar_task and not lead_radar_task.done():
        lead_radar_task.cancel()
    power_manager.stop()


app = FastAPI(title="Facebook Group Poster Pro - Pure Cookie", lifespan=lifespan)

# Mount static and uploads
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.on_event("startup")
async def on_startup():
    threading.Thread(target=open_browser_delayed, daemon=True).start()
    telegram_daemon.start_in_background()


# ==================== CAMPAIGN CONTROLLER & WORKER ====================

campaign_controller = CampaignController()
campaign_state = campaign_controller  # Backward compatible alias


# ==================== TELEGRAM REMOTE DAEMON CONTROLLER ====================

def get_poster_status_dict() -> Dict[str, Any]:
    return {
        "is_running": campaign_controller.state in [CampaignState.RUNNING, CampaignState.PAUSED],
        "state": campaign_controller.state,
        "current_status_msg": campaign_controller.current_status_msg,
        "completed_tasks": campaign_controller.completed_tasks,
        "total_tasks": campaign_controller.total_tasks,
        "success_count": campaign_controller.success_count,
        "failed_count": campaign_controller.failed_count
    }


def toggle_poster_state():
    if campaign_controller.state == CampaignState.RUNNING:
        campaign_controller.pause()
        return "paused"
    elif campaign_controller.state == CampaignState.PAUSED:
        campaign_controller.resume()
        return "resumed"
    elif campaign_controller.state == CampaignState.STOPPED:
        campaign_controller.finish()
        return "stopped"
    return "idle"


def toggle_radar_state():
    if radar_status.get("is_running"):
        global_radar_controller.stop()
        with radar_lock:
            radar_status["is_running"] = False
            radar_status["state"] = "STOPPED"
        return "stopped"
    else:
        asyncio.create_task(run_lead_radar_iteration())
        return "started"


async def trigger_async_radar_scan():
    await run_lead_radar_iteration()


app_shared_state = {
    "radar_status": lambda: radar_status,
    "poster_status": get_poster_status_dict,
    "trigger_radar_scan": trigger_async_radar_scan,
    "trigger_poster_toggle": toggle_poster_state,
    "trigger_radar_toggle": toggle_radar_state,
}
telegram_daemon = TelegramRemoteDaemon(app_shared_state)


async def run_campaign_worker(
    apartment_ids: List[int],
    group_ids: List[str],
    account_ids: Optional[List[int]] = None,
    rotation_mode: str = "round_robin",
    distribution_mode: str = "split_groups",
    delay_min: int = 15,
    delay_max: int = 45,
    max_posts_per_account: int = 10,
    retry_count: int = 1,
    watermark_phone: Optional[str] = None,
    watermark_opacity: float = 0.35,
    enable_crosspost: bool = False,
    shuffle_groups: bool = True
):
    # 1. Fetch apartments
    all_apts = db.get_apartments()
    selected_apts = [a for a in all_apts if a["id"] in apartment_ids]
    if not selected_apts:
        campaign_controller.current_status_msg = "Không tìm thấy mẫu căn hộ hợp lệ nào."
        campaign_controller.finish()
        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}[ERROR] Không tìm thấy mẫu căn hộ được chọn.{C_RESET}")
        return

    # 2. Fetch target accounts
    all_accounts = db.get_accounts()
    if account_ids and len(account_ids) > 0:
        active_accounts = [a for a in all_accounts if a["id"] in account_ids and a["status"] == "LIVE"]
    else:
        active_accounts = [a for a in all_accounts if a["status"] == "LIVE"]

    if not active_accounts:
        campaign_controller.current_status_msg = "Không có tài khoản LIVE nào sẵn sàng để đăng."
        campaign_controller.finish()
        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}[ERROR] Không có tài khoản LIVE nào sẵn sàng.{C_RESET}")
        return

    # Tự động xáo trộn ngẫu nhiên thứ tự nhóm nếu được bật
    target_groups = list(group_ids)
    if shuffle_groups and len(target_groups) > 1:
        random.shuffle(target_groups)
        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}🎲 Đã xáo trộn ngẫu nhiên thứ tự {len(target_groups)} nhóm mục tiêu.{C_RESET}")

    # 3. Build Task List based on distribution mode & crosspost setting
    tasks: List[Dict[str, Any]] = []
    if enable_crosspost:
        group_chunks = chunk_groups_for_crossposting(target_groups, chunk_size=10)
        if distribution_mode == "all_to_all":
            for ch in group_chunks:
                for _ in active_accounts:
                    tasks.append({
                        "group_id": ch["primary_group_id"],
                        "crosspost_group_ids": ch["crosspost_group_ids"],
                        "is_crosspost": len(ch["crosspost_group_ids"]) > 0,
                        "total_groups_in_batch": ch["total_in_batch"]
                    })
        else:  # split_groups
            for ch in group_chunks:
                tasks.append({
                    "group_id": ch["primary_group_id"],
                    "crosspost_group_ids": ch["crosspost_group_ids"],
                    "is_crosspost": len(ch["crosspost_group_ids"]) > 0,
                    "total_groups_in_batch": ch["total_in_batch"]
                })
    else:
        if distribution_mode == "all_to_all":
            for g_id in target_groups:
                for _ in active_accounts:
                    tasks.append({"group_id": g_id, "crosspost_group_ids": [], "is_crosspost": False, "total_groups_in_batch": 1})
        else:  # split_groups
            for g_id in target_groups:
                tasks.append({"group_id": g_id, "crosspost_group_ids": [], "is_crosspost": False, "total_groups_in_batch": 1})

    acc_id_strs = [str(a["id"]) for a in active_accounts]
    batch_id = f"CAMP-{uuid.uuid4().hex[:8].upper()}"
    campaign_controller.reset(
        total=len(tasks),
        account_ids=acc_id_strs,
        max_quota_per_acc=max_posts_per_account,
        batch_id=batch_id
    )

    power_manager.start()

    print(f"\n{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}")
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_MAGENTA}{C_BOLD}🚀 BẮT ĐẦU CHIẾN DỊCH ĐĂNG BÀI FACEBOOK BẰNG COOKIE GRAPHQL (BATCH: {batch_id}){C_RESET}")
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_CYAN}Căn hộ: {len(selected_apts)} mẫu ({rotation_mode}) | Tài khoản: {len(active_accounts)} LIVE{C_RESET}")
    crosspost_tag = f" [CROSSPOST: 1 chính + 9 phụ]" if enable_crosspost else ""
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_CYAN}Nhóm mục tiêu: {len(group_ids)} nhóm | Tổng đợt đăng: {len(tasks)} ({distribution_mode}){crosspost_tag}{C_RESET}")
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_CYAN}Max/clone: {max_posts_per_account} | Retry: {retry_count} | Delay: {delay_min}-{delay_max}s{C_RESET}")
    print(f"{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}\n")

    try:
        for task_index, task in enumerate(tasks, 1):
            campaign_controller.current_task_index = task_index

            # 1. Handle Pause / Resume / Stop check
            while True:
                if campaign_controller.should_stop:
                    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}[WARNING] Người dùng đã dừng chiến dịch.{C_RESET}")
                    return
                if campaign_controller.is_paused:
                    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}[DISPATCHER] ⏸️ Tiến trình đang tạm dừng. Chờ người dùng tiếp tục...{C_RESET}")
                    campaign_controller.current_status_msg = "Chiến dịch đang tạm dừng. Chờ tiếp tục..."
                    await campaign_controller.resume_event.wait()
                    if campaign_controller.should_stop:
                        return
                else:
                    break

            group_id = task["group_id"]
            crosspost_group_ids = task.get("crosspost_group_ids", [])

            # 2. Dynamic Account Resolution (Hot-swap aware)
            live_db_accounts = db.get_accounts()
            chosen_acc = campaign_controller.get_next_available_account(live_db_accounts)

            if not chosen_acc:
                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}[WARNING] ⚠️ Toàn bộ tài khoản đã đạt hạn mức ({campaign_controller.max_quota_per_acc} bài) hoặc DIE. Tự động TẠM DỪNG...{C_RESET}")
                campaign_controller.pause()
                campaign_controller.current_status_msg = f"⚠️ Hết tài khoản khả dụng (Hạn mức {campaign_controller.max_quota_per_acc} bài)! Vui lòng chọn tài khoản mới rồi nhấn 'Tiếp tục'."
                db.add_post_log(
                    token_preview="SYSTEM",
                    group_id=group_id,
                    apartment_title="--",
                    status="PAUSED",
                    approval_status="PAUSED",
                    message=f"Tất cả tài khoản đã đạt hạn mức ({campaign_controller.max_quota_per_acc} bài) hoặc DIE. Tự động tạm dừng để bổ sung tài khoản.",
                    debug_detail={"task_index": task_index, "active_accounts": campaign_controller.active_account_ids}
                )
                await campaign_controller.resume_event.wait()
                if campaign_controller.should_stop:
                    return
                live_db_accounts = db.get_accounts()
                chosen_acc = campaign_controller.get_next_available_account(live_db_accounts)
                if not chosen_acc:
                    continue

            acc_id = chosen_acc["id"]
            acc_str_id = str(acc_id)
            actor_uid = chosen_acc.get("uid") or f"Acc #{acc_id}"
            cookie_str = chosen_acc["cookie"]
            proxy = chosen_acc.get("proxy")

            # 3. Select apartment
            if rotation_mode == "random":
                chosen_apt = random.choice(selected_apts)
            else:  # round_robin
                chosen_apt = selected_apts[(task_index - 1) % len(selected_apts)]

            campaign_controller.current_group = group_id
            campaign_controller.current_token_preview = actor_uid
            campaign_controller.current_apt_title = chosen_apt["title"]
            crosspost_info = f" (+{len(crosspost_group_ids)} nhóm phụ)" if crosspost_group_ids else ""
            campaign_controller.current_status_msg = f"Đang đăng [{task_index}/{len(tasks)}]: Group {group_id}{crosspost_info} qua Clone {actor_uid}..."

            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_BLUE}{C_BOLD}[TASK {task_index}/{len(tasks)}]{C_RESET} Group: {C_YELLOW}{group_id}{crosspost_info}{C_RESET} | Căn hộ: {C_GREEN}{chosen_apt['title'][:25]}...{C_RESET} | Clone: {C_CYAN}{actor_uid}{C_RESET}")

            # 1. Parse Spintax & Resolve Image Paths
            parsed_message = parse_spintax(chosen_apt["spintax_content"])
            raw_images = chosen_apt.get("image_paths", [])
            if isinstance(raw_images, str):
                try:
                    raw_images = json.loads(raw_images)
                except Exception:
                    raw_images = [raw_images]
            image_paths = resolve_image_paths(raw_images, UPLOAD_DIR)
            if image_paths:
                print(f"[{get_vn_time_str('%H:%M:%S')}]   [POSTER] Nạp {len(image_paths)} ảnh từ căn hộ '{chosen_apt['title']}' -> Bắt đầu upload...")

            try:
                # 2. Run post in threadpool with GraphQL mutation story_create
                loop = asyncio.get_running_loop()
                post_func = functools.partial(
                    publish_post_via_cookie,
                    group_id=group_id,
                    message=parsed_message,
                    cookie_str=cookie_str,
                    photo_paths=image_paths,
                    proxy=proxy,
                    retry_count=retry_count,
                    crosspost_group_ids=crosspost_group_ids
                )
                res = await loop.run_in_executor(None, post_func)

                # 3. Process result & post status
                status_str = "SUCCESS" if res["success"] else "FAILED"
                approval_status = res.get("approval_status", "FAILED" if not res["success"] else "AUTO_APPROVED")
                msg_str = res["message"]
                post_id = res.get("post_id")
                post_url = res.get("post_url")
                debug_detail = res.get("debug_detail", {})
                is_cookie_dead = res.get("is_cookie_dead", False)

                # Upsert group profile classification for primary & secondary groups
                db.upsert_group_status(group_id, approval_status)
                for sec_gid in crosspost_group_ids:
                    db.upsert_group_status(sec_gid, approval_status)

                if res["success"]:
                    campaign_controller.success_count += 1
                    campaign_controller.auto_approved_count += 1
                    campaign_controller.acc_usage[acc_str_id] = campaign_controller.acc_usage.get(acc_str_id, 0) + 1
                    db.update_account_last_used(acc_id)
                    if crosspost_group_ids:
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}{C_BOLD}[POSTER] 🚀 Đăng chéo thành công!{C_RESET}")
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}  ├─ Nhóm chính: {group_id} -> {post_url}{C_RESET}")
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}  └─ {len(crosspost_group_ids)} Nhóm phụ: {', '.join(crosspost_group_ids)}{C_RESET}")
                    else:
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}{C_BOLD}  🟢 [THÀNH CÔNG] Đăng bài lên Group {group_id} thành công! Link: {post_url or ('Post ID: ' + str(post_id))}{C_RESET}")
                else:
                    campaign_controller.failed_count += 1
                    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  🔴 [THẤT BẠI] Group {group_id}: {msg_str}{C_RESET}")

                    err_type = res.get("error_type")
                    parsed_err = res.get("parsed_error") or {}
                    raw_resp = res.get("raw_response") or ""

                    is_sentry_block = (
                        err_type == "SENTRY_SHADOW_BLOCK"
                        or parsed_err.get("error_type") == "SENTRY_SHADOW_BLOCK"
                        or parsed_err.get("error_code") == 3809006
                        or "sentry_block" in raw_resp
                    )

                    if is_sentry_block:
                        unlock_time = parsed_err.get("unlock_time_utc7") or (get_vn_now() + datetime.timedelta(hours=48))
                        unlock_time_str = parsed_err.get("unlock_time_str") or ((get_vn_now() + datetime.timedelta(hours=48)).strftime("%H:%M:%S %d/%m/%Y"))
                        summary_text = parsed_err.get("summary") or "Meta Sentry chặn tính năng đăng bài (Shadow Action Block)"
                        db.record_account_rate_limit(
                            account_id=acc_id,
                            unlock_time=unlock_time,
                            summary=summary_text,
                            raw_resp=raw_resp,
                            status="SENTRY_COOLDOWN"
                        )

                        acc_info = db.get_account_by_id(acc_id)
                        account_name = html.escape((acc_info.get("name") if acc_info else None) or "Facebook User")
                        account_uid = html.escape(str(actor_uid))

                        tg_sentry_msg = (
                            "🛡️ <b>[CẢNH BÁO: META SENTRY SHADOW BLOCK]</b> 🛡️\n"
                            f"👤 <b>Tài khoản UID:</b> <code>{account_uid}</code> ({account_name})\n"
                            "⚠️ <b>Trạng thái:</b> Bị Meta khóa tính năng đăng bài ngầm (Mã 3809006)\n"
                            "⏳ <b>Thời gian cách ly an toàn:</b> 48 giờ\n"
                            f"🔓 <b>Dự kiến hoạt động lại:</b> <code>{html.escape(unlock_time_str)}</code>\n"
                            "🛑 <i>Tool đã tự động cách ly tài khoản để tránh bị khóa vĩnh viễn tính năng!</i>"
                        )

                        other_live_available = any(
                            db.is_account_available(other_id)[0]
                            for other_id in campaign_controller.active_account_ids
                            if other_id != acc_id
                        ) if len(campaign_controller.active_account_ids) > 1 else False

                        if other_live_available:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}{C_BOLD}  🛡️ [SENTRY_SHADOW_BLOCK] Nick {actor_uid} dính Meta Sentry Shadow Block! Đã cách ly 48h (Mở lại: {unlock_time_str}). Tự động đổi sang Clone LIVE khác...{C_RESET}")
                            if db.get_setting("telegram_enabled", "1") == "1":
                                asyncio.create_task(telegram_notifier.send_message_async(tg_sentry_msg))
                        else:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  [ALERT] Tất cả Clone đã bị Sentry Block / Rate Limit hoặc không còn Clone LIVE khả dụng! Tạm dừng chiến dịch...{C_RESET}")
                            campaign_controller.pause()
                            alarm_reason = f"Tài khoản {actor_uid} bị META SENTRY SHADOW BLOCK và không còn Clone LIVE nào khả dụng: Mã 3809006"
                            campaign_controller.current_status_msg = f"🛡️ TẠM DỪNG: {alarm_reason}"
                            if db.get_setting("telegram_enabled", "1") == "1":
                                telegram_notifier.start_alarm(
                                    reason=alarm_reason,
                                    details={
                                        "account_uid": actor_uid,
                                        "group_id": group_id,
                                        "error_type": "SENTRY_SHADOW_BLOCK",
                                        "error_code": parsed_err.get("error_code") or "3809006",
                                        "summary": summary_text,
                                        "duration_str": "48h 00m",
                                        "unlock_time_str": unlock_time_str,
                                        "detail": parsed_err.get("detail") or msg_str
                                    }
                                )

                    elif (
                        err_type == "RATE_LIMIT"
                        or parsed_err.get("is_rate_limit")
                        or "rate limit" in msg_str.lower()
                        or "chặn đăng" in msg_str.lower()
                    ):
                        unlock_time = parsed_err.get("unlock_time_utc7") or (get_vn_now() + datetime.timedelta(hours=1))
                        summary_text = parsed_err.get("summary") or msg_str
                        db.record_account_rate_limit(
                            account_id=acc_id,
                            unlock_time=unlock_time,
                            summary=summary_text,
                            raw_resp=raw_resp
                        )

                        tg_details = {
                            "account_uid": actor_uid,
                            "group_id": group_id,
                            "error_type": "RATE_LIMIT",
                            "error_code": parsed_err.get("error_code") or "1675004",
                            "summary": summary_text,
                            "duration_str": parsed_err.get("duration_str") or "1h 00m",
                            "unlock_time_str": parsed_err.get("unlock_time_str") or (unlock_time.strftime("%H:%M:%S %d/%m/%Y") if hasattr(unlock_time, "strftime") else str(unlock_time)),
                            "detail": parsed_err.get("detail") or msg_str
                        }

                        # Kiểm tra xem còn Clone LIVE nào khác khả dụng không
                        other_live_available = any(
                            db.is_account_available(other_id)[0]
                            for other_id in campaign_controller.active_account_ids
                            if other_id != acc_id
                        ) if len(campaign_controller.active_account_ids) > 1 else False

                        if other_live_available:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}  ⚠️ [RATE_LIMIT] Nick {actor_uid} dính Rate Limit (Mở lại: {tg_details['unlock_time_str']}). Tự động đổi sang Clone LIVE khác...{C_RESET}")
                            if db.get_setting("telegram_enabled", "1") == "1":
                                tg_msg = (
                                    f"🚨 <b>[PHÁT HIỆN GIỚI HẠN FACEBOOK (RATE LIMIT)]</b> 🚨\n\n"
                                    f"👤 <b>Tài khoản UID:</b> <code>{html.escape(actor_uid)}</code>\n"
                                    f"⚠️ <b>Mã lỗi:</b> <code>{html.escape(str(tg_details['error_code']))}</code> - {html.escape(summary_text)}\n"
                                    f"⏳ <b>Thời gian dính giới hạn:</b> {html.escape(tg_details['duration_str'])}\n"
                                    f"🔓 <b>Dự kiến mở khóa lại:</b> <code>{html.escape(tg_details['unlock_time_str'])}</code>\n"
                                    f"📝 <b>Chi tiết từ Meta:</b> <i>{html.escape(str(tg_details['detail']))}</i>\n\n"
                                    f"🔄 <i>Hệ thống tự động chuyển sang Clone tiếp theo để tiếp tục chiến dịch!</i>"
                                )
                                asyncio.create_task(telegram_notifier.send_message_async(tg_msg))
                        else:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  [ALERT] Tất cả Clone đã bị Rate Limit hoặc không còn Clone LIVE khả dụng!{C_RESET}")
                            campaign_controller.pause()
                            alarm_reason = f"Tài khoản {actor_uid} bị RATE LIMIT và không còn Clone LIVE nào khả dụng: {msg_str}"
                            campaign_controller.current_status_msg = f"🚨 TẠM DỪNG: {alarm_reason}"
                            if db.get_setting("telegram_enabled", "1") == "1":
                                telegram_notifier.start_alarm(
                                    reason=alarm_reason,
                                    details=tg_details
                                )
                                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}[ALARM] 🚨 Đang kích hoạt còi báo Telegram liên tục...{C_RESET}")

                    elif (
                        is_cookie_dead
                        or err_type == "COOKIE_DIE"
                        or any(k in msg_str for k in ["Checkpoint", "Cookie hết hạn", "1357004", "DIE"])
                    ):
                        db.update_account_status(acc_id, "CHECKPOINT")
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  [ALERT] Cookie Clone {actor_uid} đã Hết hạn/CHECKPOINT!{C_RESET}")
                        
                        other_live_available = any(
                            db.is_account_available(other_id)[0]
                            for other_id in campaign_controller.active_account_ids
                            if other_id != acc_id
                        ) if len(campaign_controller.active_account_ids) > 1 else False

                        if other_live_available:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}  ⚠️ [COOKIE_DIE] Nick {actor_uid} bị văng/checkpoint. Tự động đổi sang Clone LIVE khác...{C_RESET}")
                            if db.get_setting("telegram_enabled", "1") == "1":
                                tg_msg = (
                                    f"⚠️ <b>[CẢNH BÁO TÀI KHOẢN VĂNG / CHECKPOINT]</b> ⚠️\n\n"
                                    f"👤 <b>Tài khoản UID:</b> <code>{html.escape(actor_uid)}</code>\n"
                                    f"🎯 <b>Nhóm lỗi:</b> <code>{html.escape(group_id)}</code>\n"
                                    f"📝 <b>Chi tiết:</b> <i>{html.escape(msg_str)}</i>\n\n"
                                    f"🔄 <i>Hệ thống tự động chuyển sang Clone LIVE khác để tiếp tục chiến dịch!</i>"
                                )
                                asyncio.create_task(telegram_notifier.send_message_async(tg_msg))
                        else:
                            campaign_controller.pause()
                            alarm_reason = f"Cookie Clone {actor_uid} hết hạn / Checkpoint và không còn Clone LIVE khác"
                            campaign_controller.current_status_msg = f"🚨 TẠM DỪNG: {alarm_reason}"
                            if db.get_setting("telegram_enabled", "1") == "1":
                                telegram_notifier.start_alarm(
                                    reason=alarm_reason,
                                    details={"account_uid": actor_uid, "group_id": group_id, "error_type": "COOKIE_DIE"}
                                )
                                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}[ALARM] 🚨 Đã phát hiện lỗi COOKIE_DIE! Đang kích hoạt còi báo Telegram liên tục...{C_RESET}")

                campaign_controller.completed_tasks += 1
                db.record_post_history(
                    account_id=acc_str_id,
                    apartment_id=chosen_apt["id"],
                    group_id=group_id,
                    status=status_str,
                    post_url=post_url
                )
                db.add_post_log(
                    token_preview=actor_uid,
                    group_id=group_id,
                    apartment_title=chosen_apt["title"],
                    status=status_str,
                    approval_status=approval_status,
                    message=msg_str if not crosspost_group_ids else f"[Đăng chéo {len(crosspost_group_ids)+1} nhóm] {msg_str}",
                    debug_detail=debug_detail,
                    raw_response=res.get("raw_response"),
                    post_id=post_id,
                    post_url=post_url
                )
                for sec_gid in crosspost_group_ids:
                    sec_url = f"https://facebook.com/groups/{sec_gid}"
                    db.record_post_history(
                        account_id=acc_str_id,
                        apartment_id=chosen_apt["id"],
                        group_id=sec_gid,
                        status=status_str,
                        post_url=sec_url
                    )
                    db.add_post_log(
                        token_preview=actor_uid,
                        group_id=sec_gid,
                        apartment_title=chosen_apt["title"],
                        status=status_str,
                        approval_status=approval_status,
                        message=f"Đăng chéo theo nhóm chính {group_id} (Post ID: {post_id or '--'})",
                        debug_detail=debug_detail,
                        post_id=post_id,
                        post_url=sec_url
                    )

            except Exception as e:
                campaign_controller.failed_count += 1
                campaign_controller.completed_tasks += 1
                tb_str = traceback.format_exc()
                err_msg = f"Ngoại lệ worker: {str(e)}"
                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}  [EXCEPTION] {err_msg}\n{tb_str}{C_RESET}")
                db.add_post_log(
                    token_preview=actor_uid,
                    group_id=group_id,
                    apartment_title=chosen_apt["title"],
                    status="FAILED",
                    approval_status="FAILED",
                    message=err_msg,
                    debug_detail={"exception": str(e), "trace": tb_str}
                )

            # Delay before next task (Pause-aware)
            if task_index < len(tasks) and not campaign_controller.should_stop:
                wait_sec = random.randint(max(1, delay_min), max(delay_min, delay_max))
                print(f"[{get_vn_time_str('%H:%M:%S')}]   ⏳ Đang nghỉ an toàn {wait_sec}s trước bài đăng tiếp theo...")
                for s in range(wait_sec, 0, -1):
                    if campaign_controller.should_stop or campaign_controller.is_paused:
                        break
                    campaign_controller.current_status_msg = f"Giãn cách an toàn ({s}s)..."
                    await asyncio.sleep(1)

    finally:
        if campaign_controller.state != CampaignState.STOPPED:
            campaign_controller.finish()
        power_manager.stop()
        print(f"\n{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}")
        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_MAGENTA}{C_BOLD}🏁 CHIẾN DỊCH ĐÃ KẾT THÚC! (Thành công: {campaign_controller.success_count}/{campaign_controller.completed_tasks}){C_RESET}")
        print(f"{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}\n")


async def run_matrix_posting_worker(
    apartment_ids: List[int],
    group_ids: List[str],
    account_ids: Optional[List[int]] = None,
    max_quota_per_acc: int = 50,
    delay_min: int = 30,
    delay_max: int = 60,
    allow_repeat_group: bool = True,
    retry_count: int = 1,
    enable_crosspost: bool = False,
    shuffle_groups: bool = True
):
    """Background worker executing Shifted Round-Robin Posting Matrix across apartments, groups and accounts."""
    # 1. Fetch apartments
    all_apts = db.get_apartments()
    selected_apts = {a["id"]: a for a in all_apts if a["id"] in apartment_ids}
    if not selected_apts:
        campaign_controller.current_status_msg = "Không tìm thấy mẫu căn hộ hợp lệ nào."
        campaign_controller.finish()
        return

    # 2. Fetch active accounts
    all_accounts = db.get_accounts()
    if account_ids and len(account_ids) > 0:
        active_accounts = [a for a in all_accounts if a["id"] in account_ids and a["status"] == "LIVE"]
    else:
        active_accounts = [a for a in all_accounts if a["status"] == "LIVE"]

    if not active_accounts:
        campaign_controller.current_status_msg = "Không có tài khoản LIVE nào sẵn sàng."
        campaign_controller.finish()
        return

    # Tự động xáo trộn ngẫu nhiên thứ tự nhóm nếu được bật
    target_groups = list(group_ids)
    if shuffle_groups and len(target_groups) > 1:
        random.shuffle(target_groups)
        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}🎲 Đã xáo trộn ngẫu nhiên thứ tự {len(target_groups)} nhóm mục tiêu trước khi lập ma trận.{C_RESET}")

    # 3. Generate Shifted Round-Robin Posting Matrix
    acc_id_strs = [str(a["id"]) for a in active_accounts]
    tasks = generate_posting_matrix(
        apartment_ids=list(selected_apts.keys()),
        group_ids=target_groups,
        account_ids=acc_id_strs,
        max_quota_per_acc=max_quota_per_acc,
        allow_repeat_group=allow_repeat_group,
        enable_crosspost=enable_crosspost,
        shuffle_groups=False  # Đã xáo trộn ở trên
    )

    if not tasks:
        campaign_controller.current_status_msg = "Không sinh được nhiệm vụ đăng bài nào từ ma trận."
        campaign_controller.finish()
        return

    batch_id = f"BATCH-{uuid.uuid4().hex[:8].upper()}"
    db.create_post_queue_batch(batch_id, tasks)

    campaign_controller.reset(
        total=len(tasks),
        account_ids=acc_id_strs,
        max_quota_per_acc=max_quota_per_acc,
        batch_id=batch_id
    )

    power_manager.start()

    print(f"\n{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}")
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_MAGENTA}{C_BOLD}🚀 BẮT ĐẦU ĐĂNG BÀI MA TRẬN XOAY VÒNG TỊNH TIẾN (BATCH: {batch_id}){C_RESET}")
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_CYAN}Căn hộ: {len(selected_apts)} | Nhóm: {len(group_ids)} | Tài khoản: {len(active_accounts)} LIVE{C_RESET}")
    crosspost_tag = f" [CROSSPOST: 1 chính + 9 phụ]" if enable_crosspost else ""
    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_CYAN}Hạn mức: {max_quota_per_acc} bài/nick | Tổng tasks: {len(tasks)}{crosspost_tag} | Delay: {delay_min}-{delay_max}s{C_RESET}")
    print(f"{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}\n")

    try:
        for task_index, task in enumerate(tasks, 1):
            campaign_controller.current_task_index = task_index

            # 1. Handle Pause / Resume / Stop check
            while True:
                if campaign_controller.should_stop:
                    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}[WARNING] Người dùng đã dừng ma trận đăng bài.{C_RESET}")
                    return
                if campaign_controller.is_paused:
                    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}[DISPATCHER] ⏸️ Tiến trình ma trận đang tạm dừng. Chờ tiếp tục...{C_RESET}")
                    campaign_controller.current_status_msg = "Ma trận đăng bài đang tạm dừng. Chờ tiếp tục..."
                    await campaign_controller.resume_event.wait()
                    if campaign_controller.should_stop:
                        return
                else:
                    break

            grp_id = str(task["group_id"])
            crosspost_group_ids = task.get("crosspost_group_ids", [])
            apt_id = int(task["apartment_id"])
            pass_idx = task.get("pass_index", 0)

            apt_info = selected_apts.get(apt_id)
            if not apt_info:
                continue

            # 2. Dynamic Account Selection (Hot-swap aware)
            live_db_accounts = db.get_accounts()
            chosen_acc = campaign_controller.get_next_available_account(live_db_accounts)

            if not chosen_acc:
                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}[WARNING] ⚠️ Toàn bộ tài khoản đã đạt hạn mức ({campaign_controller.max_quota_per_acc} bài) hoặc DIE. Tự động TẠM DỪNG...{C_RESET}")
                campaign_controller.pause()
                campaign_controller.current_status_msg = f"⚠️ Hết tài khoản khả dụng (Hạn mức {campaign_controller.max_quota_per_acc} bài)! Vui lòng chọn tài khoản mới rồi nhấn 'Tiếp tục'."
                db.add_post_log(
                    token_preview="SYSTEM",
                    group_id=grp_id,
                    apartment_title=apt_info["title"],
                    status="PAUSED",
                    approval_status="PAUSED",
                    message=f"Tất cả tài khoản đã đạt hạn mức ({campaign_controller.max_quota_per_acc} bài) hoặc DIE. Tự động tạm dừng để bổ sung tài khoản.",
                    debug_detail={"task_index": task_index, "active_accounts": campaign_controller.active_account_ids}
                )
                await campaign_controller.resume_event.wait()
                if campaign_controller.should_stop:
                    return
                live_db_accounts = db.get_accounts()
                chosen_acc = campaign_controller.get_next_available_account(live_db_accounts)
                if not chosen_acc:
                    continue

            acc_str_id = str(chosen_acc["id"])
            actor_uid = chosen_acc.get("uid") or f"Acc #{acc_str_id}"
            cookie_str = chosen_acc["cookie"]
            proxy = chosen_acc.get("proxy")

            campaign_controller.current_group = grp_id
            campaign_controller.current_token_preview = actor_uid
            campaign_controller.current_apt_title = apt_info["title"]
            crosspost_info = f" (+{len(crosspost_group_ids)} phụ)" if crosspost_group_ids else ""
            campaign_controller.current_status_msg = f"Đang đăng [{task_index}/{len(tasks)}] Vòng {pass_idx+1}: Group {grp_id}{crosspost_info} qua {actor_uid}..."

            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_BLUE}[TASK {task_index}/{len(tasks)} | Vòng {pass_idx+1}]{C_RESET} Group: {C_YELLOW}{grp_id}{crosspost_info}{C_RESET} | Căn hộ: {C_GREEN}#{apt_id} {apt_info['title'][:20]}...{C_RESET} | Nick: {C_CYAN}{actor_uid}{C_RESET}")

            # 1. Parse Spintax & Resolve Image Paths
            parsed_message = parse_spintax(apt_info["spintax_content"])
            raw_images = apt_info.get("image_paths", [])
            if isinstance(raw_images, str):
                try:
                    raw_images = json.loads(raw_images)
                except Exception:
                    raw_images = [raw_images]
            image_paths = resolve_image_paths(raw_images, UPLOAD_DIR)
            if image_paths:
                print(f"[{get_vn_time_str('%H:%M:%S')}]   [POSTER] Nạp {len(image_paths)} ảnh từ căn hộ '{apt_info['title']}' -> Bắt đầu upload...")

            try:
                loop = asyncio.get_running_loop()
                post_func = functools.partial(
                    publish_post_via_cookie,
                    group_id=grp_id,
                    message=parsed_message,
                    cookie_str=cookie_str,
                    photo_paths=image_paths,
                    proxy=proxy,
                    retry_count=retry_count,
                    crosspost_group_ids=crosspost_group_ids
                )
                res = await loop.run_in_executor(None, post_func)

                status_str = "SUCCESS" if res["success"] else "FAILED"
                approval_status = res.get("approval_status", "FAILED" if not res["success"] else "AUTO_APPROVED")
                msg_str = res["message"]
                post_id = res.get("post_id")
                post_url = res.get("post_url")
                debug_detail = res.get("debug_detail", {})
                is_cookie_dead = res.get("is_cookie_dead", False)

                # Upsert group profile classification
                db.upsert_group_status(grp_id, approval_status)
                for sec_gid in crosspost_group_ids:
                    db.upsert_group_status(sec_gid, approval_status)

                # Record history and logs for primary group
                db.record_post_history(
                    account_id=acc_str_id,
                    apartment_id=apt_id,
                    group_id=grp_id,
                    status=status_str,
                    post_url=post_url
                )

                db.add_post_log(
                    token_preview=actor_uid,
                    group_id=grp_id,
                    apartment_title=apt_info["title"],
                    status=status_str,
                    approval_status=approval_status,
                    message=msg_str if not crosspost_group_ids else f"[Đăng chéo {len(crosspost_group_ids)+1} nhóm] {msg_str}",
                    debug_detail=debug_detail,
                    post_id=post_id,
                    post_url=post_url
                )

                # Record history and logs for secondary crosspost groups
                for sec_gid in crosspost_group_ids:
                    sec_url = f"https://facebook.com/groups/{sec_gid}"
                    db.record_post_history(
                        account_id=acc_str_id,
                        apartment_id=apt_id,
                        group_id=sec_gid,
                        status=status_str,
                        post_url=sec_url
                    )
                    db.add_post_log(
                        token_preview=actor_uid,
                        group_id=sec_gid,
                        apartment_title=apt_info["title"],
                        status=status_str,
                        approval_status=approval_status,
                        message=f"Đăng chéo theo nhóm chính {grp_id} (Post ID: {post_id or '--'})",
                        debug_detail=debug_detail,
                        raw_response=res.get("raw_response"),
                        post_id=post_id,
                        post_url=sec_url
                    )

                if res["success"]:
                    campaign_controller.success_count += 1
                    campaign_controller.auto_approved_count += 1
                    campaign_controller.acc_usage[acc_str_id] = campaign_controller.acc_usage.get(acc_str_id, 0) + 1
                    db.update_account_last_used(int(acc_str_id))
                    if crosspost_group_ids:
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}{C_BOLD}[POSTER] 🚀 Đăng chéo thành công!{C_RESET}")
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}  ├─ Nhóm chính: {grp_id} -> {post_url}{C_RESET}")
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}  └─ {len(crosspost_group_ids)} Nhóm phụ: {', '.join(crosspost_group_ids)}{C_RESET}")
                    else:
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_GREEN}  🟢 [THÀNH CÔNG] Đăng bài thành công! Link: {post_url}{C_RESET}")
                else:
                    campaign_controller.failed_count += 1
                    print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  🔴 [THẤT BẠI] Group {grp_id}: {msg_str}{C_RESET}")

                    err_type = res.get("error_type")
                    parsed_err = res.get("parsed_error") or {}
                    raw_resp = res.get("raw_response") or ""
                    acc_num_id = int(acc_str_id) if str(acc_str_id).isdigit() else 0

                    is_sentry_block = (
                        err_type == "SENTRY_SHADOW_BLOCK"
                        or parsed_err.get("error_type") == "SENTRY_SHADOW_BLOCK"
                        or parsed_err.get("error_code") == 3809006
                        or "sentry_block" in raw_resp
                    )

                    if is_sentry_block:
                        unlock_time = parsed_err.get("unlock_time_utc7") or (get_vn_now() + datetime.timedelta(hours=48))
                        unlock_time_str = parsed_err.get("unlock_time_str") or ((get_vn_now() + datetime.timedelta(hours=48)).strftime("%H:%M:%S %d/%m/%Y"))
                        summary_text = parsed_err.get("summary") or "Meta Sentry chặn tính năng đăng bài (Shadow Action Block)"
                        if acc_num_id:
                            db.record_account_rate_limit(
                                account_id=acc_num_id,
                                unlock_time=unlock_time,
                                summary=summary_text,
                                raw_resp=raw_resp,
                                status="SENTRY_COOLDOWN"
                            )

                        acc_info = db.get_account_by_id(acc_num_id) if acc_num_id else None
                        account_name = html.escape((acc_info.get("name") if acc_info else None) or "Facebook User")
                        account_uid = html.escape(str(actor_uid))

                        tg_sentry_msg = (
                            "🛡️ <b>[CẢNH BÁO: META SENTRY SHADOW BLOCK]</b> 🛡️\n"
                            f"👤 <b>Tài khoản UID:</b> <code>{account_uid}</code> ({account_name})\n"
                            "⚠️ <b>Trạng thái:</b> Bị Meta khóa tính năng đăng bài ngầm (Mã 3809006)\n"
                            "⏳ <b>Thời gian cách ly an toàn:</b> 48 giờ\n"
                            f"🔓 <b>Dự kiến hoạt động lại:</b> <code>{html.escape(unlock_time_str)}</code>\n"
                            "🛑 <i>Tool đã tự động cách ly tài khoản để tránh bị khóa vĩnh viễn tính năng!</i>"
                        )

                        other_live_available = any(
                            db.is_account_available(other_id)[0]
                            for other_id in campaign_controller.active_account_ids
                            if other_id != acc_num_id
                        ) if len(campaign_controller.active_account_ids) > 1 else False

                        if other_live_available:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}{C_BOLD}  🛡️ [SENTRY_SHADOW_BLOCK] Nick {actor_uid} dính Meta Sentry Shadow Block! Đã cách ly 48h (Mở lại: {unlock_time_str}). Tự động đổi sang Clone LIVE khác...{C_RESET}")
                            if db.get_setting("telegram_enabled", "1") == "1":
                                asyncio.create_task(telegram_notifier.send_message_async(tg_sentry_msg))
                        else:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  [ALERT] Tất cả Clone đã bị Sentry Block / Rate Limit hoặc không còn Clone LIVE khả dụng! Tạm dừng chiến dịch...{C_RESET}")
                            campaign_controller.pause()
                            alarm_reason = f"Tài khoản {actor_uid} bị META SENTRY SHADOW BLOCK và không còn Clone LIVE nào khả dụng: Mã 3809006"
                            campaign_controller.current_status_msg = f"🛡️ TẠM DỪNG: {alarm_reason}"
                            if db.get_setting("telegram_enabled", "1") == "1":
                                telegram_notifier.start_alarm(
                                    reason=alarm_reason,
                                    details={
                                        "account_uid": actor_uid,
                                        "group_id": grp_id,
                                        "error_type": "SENTRY_SHADOW_BLOCK",
                                        "error_code": parsed_err.get("error_code") or "3809006",
                                        "summary": summary_text,
                                        "duration_str": "48h 00m",
                                        "unlock_time_str": unlock_time_str,
                                        "detail": parsed_err.get("detail") or msg_str
                                    }
                                )

                    elif (
                        err_type == "RATE_LIMIT"
                        or parsed_err.get("is_rate_limit")
                        or "rate limit" in msg_str.lower()
                        or "chặn đăng" in msg_str.lower()
                    ):
                        unlock_time = parsed_err.get("unlock_time_utc7") or (get_vn_now() + datetime.timedelta(hours=1))
                        summary_text = parsed_err.get("summary") or msg_str
                        if acc_num_id:
                            db.record_account_rate_limit(
                                account_id=acc_num_id,
                                unlock_time=unlock_time,
                                summary=summary_text,
                                raw_resp=raw_resp
                            )

                        tg_details = {
                            "account_uid": actor_uid,
                            "group_id": grp_id,
                            "error_type": "RATE_LIMIT",
                            "error_code": parsed_err.get("error_code") or "1675004",
                            "summary": summary_text,
                            "duration_str": parsed_err.get("duration_str") or "1h 00m",
                            "unlock_time_str": parsed_err.get("unlock_time_str") or (unlock_time.strftime("%H:%M:%S %d/%m/%Y") if hasattr(unlock_time, "strftime") else str(unlock_time)),
                            "detail": parsed_err.get("detail") or msg_str
                        }

                        # Kiểm tra xem còn Clone LIVE nào khác khả dụng không
                        other_live_available = any(
                            db.is_account_available(other_id)[0]
                            for other_id in campaign_controller.active_account_ids
                            if other_id != acc_num_id
                        ) if len(campaign_controller.active_account_ids) > 1 else False

                        if other_live_available:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}  ⚠️ [RATE_LIMIT] Nick {actor_uid} dính Rate Limit (Mở lại: {tg_details['unlock_time_str']}). Tự động đổi sang Clone LIVE khác...{C_RESET}")
                            if db.get_setting("telegram_enabled", "1") == "1":
                                tg_msg = (
                                    f"🚨 <b>[PHÁT HIỆN GIỚI HẠN FACEBOOK (RATE LIMIT)]</b> 🚨\n\n"
                                    f"👤 <b>Tài khoản UID:</b> <code>{html.escape(actor_uid)}</code>\n"
                                    f"⚠️ <b>Mã lỗi:</b> <code>{html.escape(str(tg_details['error_code']))}</code> - {html.escape(summary_text)}\n"
                                    f"⏳ <b>Thời gian dính giới hạn:</b> {html.escape(tg_details['duration_str'])}\n"
                                    f"🔓 <b>Dự kiến mở khóa lại:</b> <code>{html.escape(tg_details['unlock_time_str'])}</code>\n"
                                    f"📝 <b>Chi tiết từ Meta:</b> <i>{html.escape(str(tg_details['detail']))}</i>\n\n"
                                    f"🔄 <i>Hệ thống tự động chuyển sang Clone tiếp theo để tiếp tục chiến dịch!</i>"
                                )
                                asyncio.create_task(telegram_notifier.send_message_async(tg_msg))
                        else:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  [ALERT] Tất cả Clone đã bị Rate Limit hoặc không còn Clone LIVE khả dụng!{C_RESET}")
                            campaign_controller.pause()
                            alarm_reason = f"Tài khoản {actor_uid} bị RATE LIMIT và không còn Clone LIVE nào khả dụng: {msg_str}"
                            campaign_controller.current_status_msg = f"🚨 TẠM DỪNG: {alarm_reason}"
                            if db.get_setting("telegram_enabled", "1") == "1":
                                telegram_notifier.start_alarm(
                                    reason=alarm_reason,
                                    details=tg_details
                                )
                                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}[ALARM] 🚨 Đang kích hoạt còi báo Telegram liên tục...{C_RESET}")

                    elif (
                        is_cookie_dead
                        or err_type == "COOKIE_DIE"
                        or any(k in msg_str for k in ["Checkpoint", "Cookie hết hạn", "1357004", "DIE"])
                    ):
                        if acc_num_id:
                            db.update_account_status(acc_num_id, "CHECKPOINT")
                        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}  [ALERT] Cookie Clone {actor_uid} đã Hết hạn/CHECKPOINT!{C_RESET}")
                        
                        other_live_available = any(
                            db.is_account_available(other_id)[0]
                            for other_id in campaign_controller.active_account_ids
                            if other_id != acc_num_id
                        ) if len(campaign_controller.active_account_ids) > 1 else False

                        if other_live_available:
                            print(f"[{get_vn_time_str('%H:%M:%S')}] {C_YELLOW}  ⚠️ [COOKIE_DIE] Nick {actor_uid} bị văng/checkpoint. Tự động đổi sang Clone LIVE khác...{C_RESET}")
                            if db.get_setting("telegram_enabled", "1") == "1":
                                tg_msg = (
                                    f"⚠️ <b>[CẢNH BÁO TÀI KHOẢN VĂNG / CHECKPOINT]</b> ⚠️\n\n"
                                    f"👤 <b>Tài khoản UID:</b> <code>{html.escape(actor_uid)}</code>\n"
                                    f"🎯 <b>Nhóm lỗi:</b> <code>{html.escape(grp_id)}</code>\n"
                                    f"📝 <b>Chi tiết:</b> <i>{html.escape(msg_str)}</i>\n\n"
                                    f"🔄 <i>Hệ thống tự động chuyển sang Clone LIVE khác để tiếp tục chiến dịch!</i>"
                                )
                                asyncio.create_task(telegram_notifier.send_message_async(tg_msg))
                        else:
                            campaign_controller.pause()
                            alarm_reason = f"Cookie Clone {actor_uid} hết hạn / Checkpoint và không còn Clone LIVE khác"
                            campaign_controller.current_status_msg = f"🚨 TẠM DỪNG: {alarm_reason}"
                            if db.get_setting("telegram_enabled", "1") == "1":
                                telegram_notifier.start_alarm(
                                    reason=alarm_reason,
                                    details={"account_uid": actor_uid, "group_id": grp_id, "error_type": "COOKIE_DIE"}
                                )
                                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}{C_BOLD}[ALARM] 🚨 Đã phát hiện lỗi COOKIE_DIE! Đang kích hoạt còi báo Telegram liên tục...{C_RESET}")

                campaign_controller.completed_tasks += 1

            except Exception as e:
                campaign_controller.failed_count += 1
                campaign_controller.completed_tasks += 1
                tb_str = traceback.format_exc()
                err_msg = f"Lỗi ngoại lệ: {str(e)}"
                print(f"[{get_vn_time_str('%H:%M:%S')}] {C_RED}  [EXCEPTION] {err_msg}\n{tb_str}{C_RESET}")
                db.add_post_log(
                    token_preview=actor_uid,
                    group_id=grp_id,
                    apartment_title=apt_info["title"],
                    status="FAILED",
                    approval_status="FAILED",
                    message=err_msg,
                    debug_detail={"exception": str(e), "trace": tb_str}
                )

            # Delay (Pause-aware)
            if task_index < len(tasks) and not campaign_controller.should_stop:
                wait_sec = random.randint(max(1, delay_min), max(delay_min, delay_max))
                print(f"[{get_vn_time_str('%H:%M:%S')}]   ⏳ Nghỉ an toàn {wait_sec}s trước bài đăng tiếp theo...")
                for s in range(wait_sec, 0, -1):
                    if campaign_controller.should_stop or campaign_controller.is_paused:
                        break
                    campaign_controller.current_status_msg = f"Nghỉ an toàn ({s}s) trước bài tiếp theo..."
                    await asyncio.sleep(1)

    finally:
        if campaign_controller.state != CampaignState.STOPPED:
            campaign_controller.finish()
        power_manager.stop()
        print(f"\n{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}")
        print(f"[{get_vn_time_str('%H:%M:%S')}] {C_MAGENTA}{C_BOLD}🏁 MA TRẬN ĐĂNG BÀI ĐÃ HOÀN TẤT! (Thành công: {campaign_controller.success_count}/{campaign_controller.completed_tasks}){C_RESET}")
        print(f"{C_MAGENTA}{C_BOLD}{'='*70}{C_RESET}\n")


# ==================== PYDANTIC SCHEMAS ====================

class ImportAccountsRequest(BaseModel):
    raw_lines: str

class StartCampaignRequest(BaseModel):
    apartment_ids: List[int]
    group_ids: List[str]
    account_ids: Optional[List[int]] = None
    rotation_mode: str = "round_robin"
    distribution_mode: str = "split_groups"
    delay_min: int = 15
    delay_max: int = 45
    max_posts_per_account: int = 10
    retry_count: int = 1
    watermark_phone: Optional[str] = None
    watermark_opacity: float = 0.35
    enable_crosspost: bool = False
    shuffle_groups: bool = True

class StartMatrixRequest(BaseModel):
    apartment_ids: List[int]
    group_ids: List[str]
    account_ids: Optional[List[int]] = None
    max_quota_per_acc: int = 50
    delay_min: int = 30
    delay_max: int = 60
    allow_repeat_group: bool = True
    retry_count: int = 1
    enable_crosspost: bool = False
    shuffle_groups: bool = True

class SavePresetRequest(BaseModel):
    name: str
    group_ids: List[str]

class UpdateAccountsRequest(BaseModel):
    account_ids: List[Any]

class ResumeCampaignRequest(BaseModel):
    account_ids: Optional[List[Any]] = None
    max_quota: Optional[int] = None

class SinglePostRequest(BaseModel):
    group_id: str
    message: str
    account_id: Optional[int] = None
    cookie: Optional[str] = None

class TelegramSettingsRequest(BaseModel):
    enabled: bool
    bot_token: str
    chat_id: str
    interval: int = 30

class TelegramTestRequest(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    message: Optional[str] = None

class RadarScanRequest(BaseModel):
    group_ids: Optional[List[str]] = None
    max_hours: int = 24
    account_id: Optional[int] = None
    target_profile: Optional[str] = "ALL"
    whitelist_vn: Optional[List[str]] = None
    blacklist_vn: Optional[List[str]] = None
    whitelist_en: Optional[List[str]] = None
    blacklist_en: Optional[List[str]] = None
    whitelist: Optional[List[str]] = None
    blacklist: Optional[List[str]] = None
    max_posts: Optional[int] = None
    max_workers: int = 4

class RadarStatusUpdateRequest(BaseModel):
    status: str

class RadarSettingsRequest(BaseModel):
    enabled: bool
    interval_minutes: int = 30
    max_hours: int = 24
    target_profile: Optional[str] = "ALL"
    whitelist_vn: Optional[List[str]] = None
    blacklist_vn: Optional[List[str]] = None
    whitelist_en: Optional[List[str]] = None
    blacklist_en: Optional[List[str]] = None
    whitelist: Optional[List[str]] = None
    blacklist: Optional[List[str]] = None

class DeleteGroupsBatchRequest(BaseModel):
    group_ids: List[str]


# ==================== REST APIS ====================

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


# --- Accounts Endpoints ---

@app.get("/api/accounts")
@app.get("/api/tokens")
async def get_accounts_endpoint(status: Optional[str] = None):
    return db.get_accounts(status=status)


@app.post("/api/accounts/import")
@app.post("/api/tokens/import")
async def import_accounts_endpoint(req: ImportAccountsRequest):
    lines = [line.strip() for line in req.raw_lines.split("\n") if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Vui lòng nhập danh sách Cookie.")
    
    imported, skipped = db.import_accounts(lines)
    return {
        "success": True,
        "message": f"Đã nhập thành công {imported} tài khoản Cookie mới, cập nhật {skipped} tài khoản.",
        "imported": imported,
        "skipped": skipped
    }


@app.post("/api/accounts/{account_id}/check")
@app.post("/api/tokens/{account_id}/check")
async def check_account_endpoint(account_id: int):
    acc = db.get_account_by_id(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
    
    loop = asyncio.get_running_loop()
    is_live, status_msg, data = await loop.run_in_executor(
        None,
        check_cookie_live,
        acc["cookie"],
        acc.get("proxy")
    )
    
    status_str = "LIVE" if is_live else ("CHECKPOINT" if "CHECKPOINT" in status_msg else "DIE")
    db.update_account_status(account_id, status_str)
    
    return {
        "id": account_id,
        "is_live": is_live,
        "status": status_str,
        "message": status_msg,
        "data": data
    }


@app.delete("/api/accounts/{account_id}")
@app.delete("/api/tokens/{account_id}")
async def delete_account_endpoint(account_id: int):
    success = db.delete_account(account_id)
    if not success:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản cần xoá.")
    return {"success": True, "message": f"Đã xoá tài khoản #{account_id}."}


@app.delete("/api/accounts/dead")
@app.delete("/api/tokens/dead")
async def delete_dead_accounts_endpoint():
    count = db.delete_dead_accounts()
    return {"success": True, "message": f"Đã dọn dẹp {count} tài khoản DIE / CHECKPOINT."}


# --- Joined Groups via Cookie Endpoint ---

@app.get("/api/accounts/{account_id}/joined-groups")
async def get_joined_groups_endpoint(account_id: int):
    acc = db.get_account_by_id(account_id)
    if not acc or not acc.get("cookie"):
        raise HTTPException(status_code=404, detail="Tài khoản không tồn tại hoặc không có Cookie.")
    
    loop = asyncio.get_running_loop()
    groups = await loop.run_in_executor(
        None,
        fetch_joined_groups_by_cookie,
        acc["cookie"],
        acc.get("proxy")
    )
    
    return {
        "success": True,
        "account_id": account_id,
        "uid": acc.get("uid"),
        "total": len(groups),
        "groups": groups
    }


# --- Group Presets Endpoints ---

@app.get("/api/presets")
async def get_presets_endpoint():
    return db.get_presets()


@app.post("/api/presets")
async def save_preset_endpoint(req: SavePresetRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Vui lòng nhập tên cấu hình nhóm.")
    if not req.group_ids:
        raise HTTPException(status_code=400, detail="Danh sách Group ID không được để trống.")
    
    preset_id = db.save_preset(req.name, req.group_ids)
    return {
        "success": True,
        "id": preset_id,
        "name": req.name.strip(),
        "total_groups": len(req.group_ids),
        "message": f"Đã lưu cấu hình '{req.name.strip()}' ({len(req.group_ids)} nhóm) thành công!"
    }


@app.delete("/api/presets/{preset_id}")
async def delete_preset_endpoint(preset_id: int):
    success = db.delete_preset(preset_id)
    if not success:
        raise HTTPException(status_code=404, detail="Không tìm thấy cấu hình cần xoá.")
    return {"success": True, "message": "Đã xoá cấu hình nhóm thành công."}


# --- Apartments Endpoints ---

@app.get("/api/apartments")
async def get_apartments_endpoint():
    apts = db.get_apartments()
    for apt in apts:
        raw_paths = apt.get("image_paths") or []
        urls = []
        for p in raw_paths:
            if not p:
                continue
            fname = os.path.basename(str(p).strip())
            if fname:
                urls.append(f"/uploads/{fname}")
        apt["image_urls"] = urls
        apt["images"] = urls
    return apts


@app.post("/api/apartments")
async def create_apartment_endpoint(
    title: str = Form(...),
    spintax_content: str = Form(...),
    images: List[UploadFile] = File(default=[])
):
    saved_image_paths = []
    for img in images:
        if img.filename:
            file_ext = os.path.splitext(img.filename)[1] or ".jpg"
            unique_filename = f"{uuid.uuid4().hex}{file_ext}"
            file_path = os.path.join(UPLOAD_DIR, unique_filename)
            with open(file_path, "wb") as f:
                shutil.copyfileobj(img.file, f)
            saved_image_paths.append(file_path)

    apt_id = db.save_apartment(
        title=title.strip(),
        spintax_content=spintax_content.strip(),
        image_paths=saved_image_paths
    )
    return {"success": True, "id": apt_id, "message": "Đã lưu mẫu căn hộ thành công."}


@app.get("/api/apartments/{apt_id}")
async def get_single_apartment_endpoint(apt_id: int):
    apt = db.get_apartment_by_id(apt_id)
    if not apt:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu căn hộ.")
    urls = []
    for p in apt.get("image_paths", []):
        if not p:
            continue
        fname = os.path.basename(str(p).strip())
        if fname:
            urls.append(f"/uploads/{fname}")
    apt["image_urls"] = urls
    apt["images"] = urls
    return apt


@app.put("/api/apartments/{apt_id}")
async def update_apartment_endpoint(
    apt_id: int,
    title: str = Form(...),
    spintax_content: str = Form(...),
    keep_images: str = Form(default="[]"),
    images: List[UploadFile] = File(default=[])
):
    apt = db.get_apartment_by_id(apt_id)
    if not apt:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu căn hộ.")

    try:
        final_images = json.loads(keep_images)
    except Exception:
        final_images = apt.get("image_paths", [])

    for img in images:
        if img.filename:
            file_ext = os.path.splitext(img.filename)[1] or ".jpg"
            unique_filename = f"{uuid.uuid4().hex}{file_ext}"
            file_path = os.path.join(UPLOAD_DIR, unique_filename)
            with open(file_path, "wb") as f:
                shutil.copyfileobj(img.file, f)
            final_images.append(file_path)

    db.update_apartment(
        apartment_id=apt_id,
        title=title.strip(),
        spintax_content=spintax_content.strip(),
        image_paths=final_images
    )
    return {"success": True, "message": "Đã cập nhật mẫu căn hộ thành công."}


@app.delete("/api/apartments/{apt_id}")
async def delete_apartment_endpoint(apt_id: int):
    success = db.delete_apartment(apt_id)
    if not success:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu căn hộ.")
    return {"success": True, "message": "Đã xoá mẫu căn hộ thành công."}


# --- Scanner Endpoints ---

@app.get("/api/scanner/stream")
async def scanner_stream_endpoint(
    keywords: str = Query(..., description="Danh sách từ khóa phân cách bởi dấu phẩy"),
    cookie: Optional[str] = Query(None),
    account_id: Optional[int] = Query(None),
    limit_per_keyword: int = Query(30),
    min_members: int = Query(1000)
):
    keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not keyword_list:
        raise HTTPException(status_code=400, detail="Vui lòng cung cấp ít nhất 1 từ khóa.")

    cookie_str = (cookie or "").strip()
    proxy_str = None

    if not cookie_str and account_id:
        acc = db.get_account_by_id(account_id)
        if acc and acc.get("cookie"):
            cookie_str = acc["cookie"].strip()
            proxy_str = acc.get("proxy")

    if not cookie_str:
        live_accounts = db.get_accounts(status="LIVE")
        if live_accounts:
            cookie_str = live_accounts[0]["cookie"]
            proxy_str = live_accounts[0].get("proxy")

    if not cookie_str:
        raise HTTPException(status_code=400, detail="Không tìm thấy Cookie Facebook LIVE để quét nhóm.")

    async def event_generator():
        for event in stream_search_groups_by_keywords(
            keywords=keyword_list,
            cookie_str=cookie_str,
            limit_per_keyword=limit_per_keyword,
            min_members=min_members,
            proxy=proxy_str
        ):
            if event.get("type") == "group_found" and "group" in event:
                g = event["group"]
                db.upsert_scanned_group(
                    group_id=g["group_id"],
                    name=g.get("name", ""),
                    status="AUTO_APPROVED",
                    privacy=g.get("privacy", "PUBLIC"),
                    member_count=g.get("member_count", 0),
                    url=g.get("url")
                )
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(0.01)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/groups")
async def get_groups_endpoint(status: str = "ALL"):
    return db.get_groups_by_status(status=status)


@app.get("/api/groups/summary")
async def get_groups_summary_endpoint():
    return db.get_groups_summary()


@app.delete("/api/groups/{group_id}")
async def delete_single_group_endpoint(group_id: str):
    success = db.delete_group(group_id)
    if not success:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm cần xoá.")
    return {"success": True, "message": f"Đã xoá nhóm {group_id} thành công."}


@app.post("/api/groups/delete-batch")
async def delete_groups_batch_endpoint(payload: DeleteGroupsBatchRequest):
    group_ids = payload.group_ids
    if not group_ids:
        raise HTTPException(status_code=400, detail="Vui lòng cung cấp danh sách Group ID cần xoá.")
    count = db.delete_groups_batch(group_ids)
    return {"success": True, "count": count, "message": f"Đã xoá {count} nhóm khỏi CSDL."}


# --- Campaign & Matrix Poster Endpoints ---

@app.post("/api/poster/start-matrix")
@app.post("/api/campaign/start-matrix")
async def start_matrix_poster_endpoint(req: StartMatrixRequest, background_tasks: BackgroundTasks):
    if campaign_state.is_running:
        raise HTTPException(status_code=400, detail="Một chiến dịch đăng bài đang chạy.")

    cleaned_groups = [g.strip() for g in req.group_ids if g and g.strip()]
    if not cleaned_groups:
        raise HTTPException(status_code=400, detail="Vui lòng nhập ít nhất 1 Group ID.")

    if not req.apartment_ids:
        raise HTTPException(status_code=400, detail="Vui lòng chọn ít nhất 1 mẫu căn hộ.")

    if req.account_ids:
        all_accounts = [db.get_account_by_id(acc_id) for acc_id in req.account_ids]
        live_accounts = [a for a in all_accounts if a and a.get("status") == "LIVE"]
        if not live_accounts:
            raise HTTPException(status_code=400, detail="Tất cả tài khoản được chọn đều không LIVE.")
    else:
        live_accounts = db.get_accounts(status="LIVE")
        if not live_accounts:
            raise HTTPException(status_code=400, detail="Không có tài khoản LIVE nào trong hệ thống.")

    background_tasks.add_task(
        run_matrix_posting_worker,
        apartment_ids=req.apartment_ids,
        group_ids=cleaned_groups,
        account_ids=req.account_ids,
        max_quota_per_acc=max(1, req.max_quota_per_acc),
        delay_min=max(1, req.delay_min),
        delay_max=max(1, req.delay_max),
        allow_repeat_group=req.allow_repeat_group,
        retry_count=max(0, req.retry_count),
        enable_crosspost=req.enable_crosspost,
        shuffle_groups=req.shuffle_groups
    )

    crosspost_note = " (Chế độ Đăng chéo 10 nhóm/lần)" if req.enable_crosspost else ""
    shuffle_note = " [Đã xáo trộn thứ tự nhóm]" if req.shuffle_groups else ""
    return {
        "success": True,
        "message": f"Đã khởi chạy Ma trận Đăng bài: {len(req.apartment_ids)} căn hộ, {len(cleaned_groups)} nhóm, {len(live_accounts)} Clone LIVE.{crosspost_note}{shuffle_note}"
    }


@app.post("/api/campaign/start")
async def start_campaign_endpoint(req: StartCampaignRequest, background_tasks: BackgroundTasks):
    if campaign_state.is_running:
        raise HTTPException(status_code=400, detail="Chiến dịch đăng bài đang chạy.")

    cleaned_groups = [g.strip() for g in req.group_ids if g and g.strip()]
    if not cleaned_groups:
        raise HTTPException(status_code=400, detail="Vui lòng nhập ít nhất 1 Group ID.")

    if not req.apartment_ids:
        raise HTTPException(status_code=400, detail="Vui lòng chọn ít nhất 1 mẫu căn hộ.")

    if req.account_ids:
        all_accounts = [db.get_account_by_id(acc_id) for acc_id in req.account_ids]
        live_accounts = [a for a in all_accounts if a and a.get("status") == "LIVE"]
        if not live_accounts:
            raise HTTPException(status_code=400, detail="Tất cả tài khoản được chọn đều không LIVE.")
    else:
        live_accounts = db.get_accounts(status="LIVE")
        if not live_accounts:
            raise HTTPException(status_code=400, detail="Không có tài khoản LIVE nào trong hệ thống.")

    background_tasks.add_task(
        run_campaign_worker,
        apartment_ids=req.apartment_ids,
        group_ids=cleaned_groups,
        account_ids=req.account_ids,
        rotation_mode=req.rotation_mode,
        distribution_mode=req.distribution_mode,
        delay_min=max(1, req.delay_min),
        delay_max=max(1, req.delay_max),
        max_posts_per_account=max(1, req.max_posts_per_account),
        retry_count=max(0, req.retry_count),
        watermark_phone=req.watermark_phone.strip() if req.watermark_phone and req.watermark_phone.strip() else None,
        watermark_opacity=max(0.1, min(0.9, req.watermark_opacity)),
        enable_crosspost=req.enable_crosspost,
        shuffle_groups=req.shuffle_groups
    )

    crosspost_note = " (Chế độ Đăng chéo 10 nhóm/lần)" if req.enable_crosspost else ""
    shuffle_note = " [Đã xáo trộn thứ tự nhóm]" if req.shuffle_groups else ""
    return {
        "success": True,
        "message": f"Đã kích hoạt chiến dịch: {len(req.apartment_ids)} mẫu căn hộ, {len(cleaned_groups)} nhóm, {len(live_accounts)} Clone LIVE.{crosspost_note}{shuffle_note}"
    }


@app.get("/api/groups/{group_id}/crosspost-eligible")
async def get_crosspost_eligible_groups_endpoint(group_id: str, account_id: Optional[int] = None):
    """Truy vấn danh sách nhóm đủ điều kiện đăng chéo (GroupsCometCrosspostGroupPickerQuery) cho primary_group_id."""
    if account_id:
        acc = db.get_account_by_id(account_id)
    else:
        live_accounts = db.get_accounts(status="LIVE")
        acc = live_accounts[0] if live_accounts else None

    if not acc:
        raise HTTPException(status_code=400, detail="Không có tài khoản LIVE nào để truy vấn nhóm đăng chéo.")

    cookie_str = acc["cookie"]
    proxy = acc.get("proxy")

    loop = asyncio.get_running_loop()
    groups = await loop.run_in_executor(
        None,
        get_eligible_crosspost_groups,
        group_id,
        cookie_str,
        proxy
    )
    return {
        "success": True,
        "primary_group_id": group_id,
        "count": len(groups),
        "groups": groups
    }


@app.post("/api/poster/pause")
@app.post("/api/campaign/pause")
async def pause_campaign_endpoint():
    telegram_notifier.stop_alarm()
    if campaign_controller.state != CampaignState.RUNNING:
        return {"success": False, "message": "Chiến dịch không ở trạng thái đang chạy (RUNNING)."}
    campaign_controller.pause()
    log_msg = f"[SYSTEM] ⏸️ Đã tạm dừng chiến dịch đăng bài tại bài #{campaign_controller.current_task_index + 1}."
    db.add_post_log(token_preview="SYSTEM", group_id="CAMPAIGN", apartment_title="", status="INFO", message=log_msg)
    print(log_msg)
    return {
        "success": True,
        "state": campaign_controller.state,
        "current_index": campaign_controller.current_task_index
    }


@app.post("/api/poster/resume")
@app.post("/api/campaign/resume")
async def resume_campaign_endpoint(req: Optional[ResumeCampaignRequest] = None):
    telegram_notifier.stop_alarm()
    if campaign_controller.state != CampaignState.PAUSED:
        return {"success": False, "message": "Chiến dịch không ở trạng thái tạm dừng (PAUSED)."}
    
    # 1. Nếu payload có truyền danh sách tài khoản mới, cập nhật ngay
    if req and req.account_ids:
        campaign_controller.update_accounts(req.account_ids)
    
    # 2. Cập nhật Quota nếu được truyền
    if req and req.max_quota is not None:
        try:
            campaign_controller.max_quota_per_acc = int(req.max_quota)
        except (ValueError, TypeError):
            pass

    # 3. Nếu active_account_ids vẫn rỗng, tự động lấy tất cả tài khoản LIVE từ database
    if not campaign_controller.active_account_ids:
        live_tokens = db.get_accounts(status="LIVE")
        if live_tokens:
            campaign_controller.update_accounts([t["id"] for t in live_tokens])
    
    # 4. Kiểm tra tài khoản khả dụng
    live_db_accounts = db.get_accounts(status="LIVE")
    available_acc = campaign_controller.get_next_available_account(live_db_accounts)
    if not available_acc:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "message": "Không có tài khoản LIVE nào khả dụng hoặc tất cả đã hết quota. Vui lòng tăng Quota hoặc chọn tài khoản khác trước khi tiếp tục!"
            }
        )
    
    campaign_controller.resume()
    log_msg = f"[SYSTEM] ▶️ Tiếp tục chiến dịch đăng bài ({len(campaign_controller.active_account_ids)} Clone, Quota: {campaign_controller.max_quota_per_acc})."
    db.add_post_log(token_preview="SYSTEM", group_id="CAMPAIGN", apartment_title="", status="INFO", message=log_msg)
    print(log_msg)
    return {
        "success": True,
        "state": campaign_controller.state,
        "active_count": len(campaign_controller.active_account_ids)
    }


@app.post("/api/poster/update-accounts")
@app.post("/api/campaign/update-accounts")
async def update_campaign_accounts_endpoint(req: UpdateAccountsRequest):
    if campaign_controller.state not in (CampaignState.PAUSED, CampaignState.RUNNING):
        raise HTTPException(
            status_code=400,
            detail="Chỉ có thể cập nhật tài khoản khi chiến dịch đang CHẠY (RUNNING) hoặc TẠM DỪNG (PAUSED)."
        )
    
    raw_ids = req.account_ids or []
    if not raw_ids:
        raise HTTPException(status_code=400, detail="Danh sách tài khoản không được để trống.")
    
    campaign_controller.update_accounts(raw_ids)
    log_msg = f"[SYSTEM] 🔄 Đã cập nhật danh sách tài khoản chạy: {len(campaign_controller.active_account_ids)} tài khoản ({raw_ids})."
    db.add_post_log(token_preview="SYSTEM", group_id="CAMPAIGN", apartment_title="", status="INFO", message=log_msg)
    print(log_msg)
    return {
        "success": True,
        "active_accounts_count": len(campaign_controller.active_account_ids),
        "active_account_ids": campaign_controller.active_account_ids
    }


@app.post("/api/poster/stop")
@app.post("/api/campaign/stop")
async def stop_campaign_endpoint():
    power_manager.stop()
    telegram_notifier.stop_alarm()
    if not campaign_controller.is_active:
        return {"success": False, "message": "Hiện không có chiến dịch nào đang hoạt động."}
    campaign_controller.stop()
    log_msg = "[SYSTEM] ⏹️ Đã gửi lệnh dừng chiến dịch."
    db.add_post_log(token_preview="SYSTEM", group_id="CAMPAIGN", apartment_title="", status="INFO", message=log_msg)
    return {"success": True, "message": "Đã gửi lệnh dừng chiến dịch."}


@app.get("/api/poster/status")
@app.get("/api/campaign/status")
async def get_campaign_status():
    status = campaign_controller.to_dict()
    status["is_alarming"] = telegram_notifier.is_alarming
    status["alarm_info"] = telegram_notifier.current_alarm_info
    return status


# --- Telegram Alert Endpoints ---

@app.get("/api/settings/telegram")
async def get_telegram_settings_endpoint():
    token, chat_id, interval = telegram_notifier.get_credentials()
    enabled = telegram_notifier.is_enabled()
    
    # Mask bot token for display security
    masked_token = token
    if len(token) > 12:
        masked_token = f"{token[:6]}...{token[-4:]}"
        
    return {
        "enabled": enabled,
        "bot_token": token,
        "bot_token_masked": masked_token,
        "chat_id": chat_id,
        "interval": interval,
        "is_alarming": telegram_notifier.is_alarming,
        "alarm_info": telegram_notifier.current_alarm_info
    }


@app.post("/api/settings/telegram")
async def save_telegram_settings_endpoint(req: TelegramSettingsRequest):
    db.set_setting("telegram_enabled", "1" if req.enabled else "0")
    if req.bot_token.strip():
        db.set_setting("telegram_bot_token", req.bot_token.strip())
    if req.chat_id.strip():
        db.set_setting("telegram_chat_id", req.chat_id.strip())
    interval_val = max(5, int(req.interval))
    db.set_setting("telegram_alarm_interval", str(interval_val))
    
    return {
        "success": True,
        "message": "Đã lưu cài đặt cảnh báo Telegram thành công!"
    }


@app.post("/api/telegram/test")
async def test_telegram_endpoint(req: Optional[TelegramTestRequest] = None):
    token = req.bot_token if (req and req.bot_token) else None
    chat_id = req.chat_id if (req and req.chat_id) else None
    test_msg = (
        (req and req.message) or
        f"🔔 <b>[FB-TOOL1] Kiểm tra kết nối Telegram thành công!</b>\n⏰ Thời gian: {get_vn_time_str()}\n✅ Hệ thống sẵn sàng gửi cảnh báo khẩn cấp khi phát hiện lỗi Rate Limit / Cookie DIE."
    )
    
    success, msg = await telegram_notifier.send_message_async(test_msg, bot_token=token, chat_id=chat_id)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


@app.post("/api/telegram/stop-alarm")
async def stop_telegram_alarm_endpoint():
    stopped = telegram_notifier.stop_alarm()
    return {
        "success": True,
        "stopped": stopped,
        "message": "Đã tắt còi báo động Telegram thành công."
    }


@app.get("/api/poster/history")
async def get_post_history_endpoint(limit: int = 100):
    return db.get_post_history(limit=limit)


@app.delete("/api/poster/history")
async def clear_post_history_endpoint():
    cleared = db.clear_post_history()
    return {"success": True, "message": f"Đã xoá {cleared} dòng lịch sử đăng bài."}


# --- Logs & Stats Endpoints ---

@app.get("/api/logs")
async def get_logs(limit: int = 50):
    return db.get_recent_logs(limit=limit)


@app.delete("/api/logs")
async def clear_logs_endpoint():
    cleared = db.clear_logs()
    return {"success": True, "message": f"Đã xoá toàn bộ {cleared} dòng lịch sử log."}


@app.get("/api/stats")
async def get_stats():
    accounts = db.get_accounts()
    live_accs = [a for a in accounts if a["status"] == "LIVE"]
    apartments = db.get_apartments()
    group_summary = db.get_groups_summary()
    lead_stats = db.get_lead_stats()
    return {
        "total_accounts": len(accounts),
        "live_accounts": len(live_accs),
        "total_apartments": len(apartments),
        "total_groups": group_summary.get("total", 0),
        "auto_approved_groups": group_summary.get("auto_approved", 0),
        "lead_stats": lead_stats
    }


# ==================== LEAD RADAR ENDPOINTS ====================

@app.post("/api/radar/scan-now")
async def radar_scan_now_endpoint(req: Optional[RadarScanRequest] = None):
    """Triggers an immediate scan for rental seeker leads."""
    group_ids = req.group_ids if req else None
    max_hours = req.max_hours if req else 24
    acc_id = req.account_id if req else None
    target_prof = req.target_profile if req else "ALL"
    wl_vn = req.whitelist_vn if req else None
    bl_vn = req.blacklist_vn if req else None
    wl_en = req.whitelist_en if req else None
    bl_en = req.blacklist_en if req else None
    wl = req.whitelist if req else None
    bl = req.blacklist if req else None
    max_workers = req.max_workers if req else 4

    result = await run_lead_radar_iteration(
        group_ids=group_ids,
        max_hours=max_hours,
        account_id=acc_id,
        target_profile=target_prof,
        custom_whitelist_vn=wl_vn,
        custom_blacklist_vn=bl_vn,
        custom_whitelist_en=wl_en,
        custom_blacklist_en=bl_en,
        custom_whitelist=wl,
        custom_blacklist=bl,
        send_telegram=True,
        max_workers=max_workers
    )

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Lỗi quét nhóm"))

    scanned_groups = result.get("scanned_groups", 0)
    total_leads_matched = result.get("total_leads_matched", len(result.get("leads", [])))
    new_leads_saved = result.get("new_leads_saved", result.get("new_count", 0))

    return {
        "success": True,
        "scanned_groups": scanned_groups,
        "total_leads_matched": total_leads_matched,
        "new_leads_saved": new_leads_saved,
        "leads": result.get("leads", []),
        "message": result.get("message") or f"Quét hoàn tất {scanned_groups} nhóm: Tìm thấy {total_leads_matched} bài phù hợp ({new_leads_saved} khách mới)."
    }


@app.post("/api/radar/pause")
async def radar_pause_endpoint():
    """Tạm dừng quét radar."""
    global_radar_controller.pause()
    with radar_lock:
        radar_status["state"] = "PAUSED"
        scanned = radar_status.get("scanned_groups", 0)
        total = radar_status.get("total_groups", 0)
        radar_status["message"] = f"⏸️ Đang tạm dừng quét [{scanned}/{total} nhóm]..."
    return {"success": True, "state": "PAUSED", "message": "Đã tạm dừng quét radar"}


@app.post("/api/radar/resume")
async def radar_resume_endpoint():
    """Tiếp tục quét radar."""
    global_radar_controller.resume()
    with radar_lock:
        radar_status["state"] = "RUNNING"
        scanned = radar_status.get("scanned_groups", 0)
        total = radar_status.get("total_groups", 0)
        radar_status["message"] = f"Đang quét song song... [{scanned}/{total}] nhóm hoàn tất ({radar_status['new_leads']} khách mới)"
    return {"success": True, "state": "RUNNING", "message": "Đang tiếp tục quét radar"}


@app.post("/api/radar/stop")
async def radar_stop_endpoint():
    """Dừng hẳn quét radar."""
    global_radar_controller.stop()
    with radar_lock:
        radar_status["state"] = "STOPPED"
        radar_status["is_running"] = False
        scanned = radar_status.get("scanned_groups", 0)
        total = radar_status.get("total_groups", 0)
        radar_status["message"] = f"⏹️ Đã dừng quét ({scanned}/{total} nhóm)."
    return {"success": True, "state": "STOPPED", "message": "Đã dừng quét radar"}


@app.get("/api/radar/progress")
async def get_radar_progress_endpoint():
    """Returns real-time progress and state of lead radar scan."""
    with radar_lock:
        status_copy = dict(radar_status)
    return status_copy



@app.get("/api/radar/leads")
async def get_radar_leads_endpoint(
    limit: int = 50,
    offset: int = 0,
    status: str = "ALL",
    keyword: Optional[str] = None,
    has_phone: Optional[bool] = None,
    client_type: str = "ALL"
):
    """Retrieves list of captured leads."""
    only_has_phone = bool(has_phone)
    leads = db.get_leads(limit=limit, offset=offset, status=status, keyword=keyword, only_has_phone=only_has_phone, client_type=client_type)
    total = db.get_leads_count(status=status, keyword=keyword, only_has_phone=only_has_phone, client_type=client_type)
    stats = db.get_lead_stats()
    return {
        "leads": leads,
        "total": total,
        "stats": stats,
        "limit": limit,
        "offset": offset
    }


@app.get("/api/radar/leads/export-phones")
async def export_radar_phones_endpoint(
    format: str = Query("csv", pattern="^(csv|txt)$"),
    status: str = "ALL",
    keyword: Optional[str] = None,
    client_type: str = "ALL"
):
    """Xuất danh sách SĐT từ các bài viết đã quét được dưới dạng CSV hoặc TXT."""
    leads = db.get_leads(limit=5000, offset=0, status=status, keyword=keyword, only_has_phone=True, client_type=client_type)
    
    if format == "txt":
        lines = []
        for l in leads:
            phone_str = (l.get("phone") or "").strip()
            if phone_str:
                for p in phone_str.split(","):
                    p_clean = p.strip()
                    if p_clean and p_clean not in lines:
                        lines.append(p_clean)
        content = "\n".join(lines)
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=leads_phones_{get_vn_time_str('%Y%m%d_%H%M%S')}.txt"}
        )
    else:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Số điện thoại", "Tên khách hàng", "Nhóm", "Thời gian đăng", "Link bài viết", "Từ khóa khớp", "Nội dung"])
        for l in leads:
            writer.writerow([
                l.get("phone") or "",
                l.get("author_name") or "",
                l.get("group_name") or "",
                l.get("created_time") or "",
                l.get("post_url") or "",
                l.get("matched_keywords") or "",
                (l.get("content") or "").replace("\n", " ")
            ])
        csv_data = output.getvalue()
        bom_csv = "\ufeff" + csv_data
        return Response(
            content=bom_csv.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=leads_phones_{get_vn_time_str('%Y%m%d_%H%M%S')}.csv"}
        )


@app.post("/api/radar/leads/{post_id}/status")
async def update_radar_lead_status_endpoint(post_id: str, req: RadarStatusUpdateRequest):
    """Updates status for a specific lead."""
    valid_statuses = ["NEW", "CONTACTED", "IGNORED"]
    st = req.status.upper()
    if st not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Trạng thái không hợp lệ: {req.status}")

    updated = db.update_lead_status(post_id, st)
    return {
        "success": updated,
        "post_id": post_id,
        "status": st,
        "message": f"Đã cập nhật trạng thái lead sang {st}"
    }


@app.delete("/api/radar/leads/{lead_id}")
async def delete_radar_lead_endpoint(lead_id: str):
    """Deletes a single lead."""
    deleted = db.delete_lead(lead_id)
    return {"success": deleted, "message": "Đã xoá khách hàng tìm thuê này."}


@app.delete("/api/radar/leads")
async def clear_radar_leads_endpoint():
    """Clears all leads."""
    deleted = db.clear_leads()
    return {"success": True, "message": f"Đã xoá toàn bộ {deleted} khách hàng trong Radar."}


@app.get("/api/radar/settings")
async def get_radar_settings_endpoint():
    """Retrieves radar settings (whitelist, blacklist, interval, auto status)."""
    enabled = db.get_setting("lead_radar_enabled", "0") == "1"
    try:
        interval = int(db.get_setting("lead_radar_interval_minutes", "30"))
    except Exception:
        interval = 30
    try:
        max_hours = int(db.get_setting("lead_radar_max_hours", "24"))
    except Exception:
        max_hours = 24

    target_profile = db.get_setting("radar_target_profile", "ALL")

    raw_wl_vn = db.get_setting("radar_whitelist_vn", "") or db.get_setting("lead_radar_whitelist", "")
    try:
        whitelist_vn = json.loads(raw_wl_vn) if raw_wl_vn else DEFAULT_WHITELIST_VN
    except Exception:
        whitelist_vn = DEFAULT_WHITELIST_VN

    raw_bl_vn = db.get_setting("radar_blacklist_vn", "") or db.get_setting("lead_radar_blacklist", "")
    try:
        blacklist_vn = json.loads(raw_bl_vn) if raw_bl_vn else DEFAULT_BLACKLIST_VN
    except Exception:
        blacklist_vn = DEFAULT_BLACKLIST_VN

    raw_wl_en = db.get_setting("radar_whitelist_en", "")
    try:
        whitelist_en = json.loads(raw_wl_en) if raw_wl_en else DEFAULT_WHITELIST_EN
    except Exception:
        whitelist_en = DEFAULT_WHITELIST_EN

    raw_bl_en = db.get_setting("radar_blacklist_en", "")
    try:
        blacklist_en = json.loads(raw_bl_en) if raw_bl_en else DEFAULT_BLACKLIST_EN
    except Exception:
        blacklist_en = DEFAULT_BLACKLIST_EN

    return {
        "enabled": enabled,
        "interval_minutes": interval,
        "max_hours": max_hours,
        "target_profile": target_profile,
        "whitelist_vn": whitelist_vn,
        "blacklist_vn": blacklist_vn,
        "whitelist_en": whitelist_en,
        "blacklist_en": blacklist_en,
        # backward compatibility
        "whitelist": whitelist_vn,
        "blacklist": blacklist_vn
    }


@app.post("/api/radar/settings")
async def save_radar_settings_endpoint(req: RadarSettingsRequest):
    """Updates radar settings."""
    db.set_setting("lead_radar_enabled", "1" if req.enabled else "0")
    db.set_setting("lead_radar_interval_minutes", str(max(5, int(req.interval_minutes))))
    db.set_setting("lead_radar_max_hours", str(max(1, int(req.max_hours))))
    db.set_setting("radar_target_profile", req.target_profile or "ALL")

    # Vietnamese lists
    wl_vn = req.whitelist_vn if req.whitelist_vn is not None else (req.whitelist or DEFAULT_WHITELIST_VN)
    bl_vn = req.blacklist_vn if req.blacklist_vn is not None else (req.blacklist or DEFAULT_BLACKLIST_VN)
    db.set_setting("radar_whitelist_vn", json.dumps([w.strip() for w in wl_vn if w.strip()], ensure_ascii=False))
    db.set_setting("radar_blacklist_vn", json.dumps([b.strip() for b in bl_vn if b.strip()], ensure_ascii=False))
    db.set_setting("lead_radar_whitelist", json.dumps([w.strip() for w in wl_vn if w.strip()], ensure_ascii=False))
    db.set_setting("lead_radar_blacklist", json.dumps([b.strip() for b in bl_vn if b.strip()], ensure_ascii=False))

    # Expat lists
    wl_en = req.whitelist_en if req.whitelist_en is not None else DEFAULT_WHITELIST_EN
    bl_en = req.blacklist_en if req.blacklist_en is not None else DEFAULT_BLACKLIST_EN
    db.set_setting("radar_whitelist_en", json.dumps([w.strip() for w in wl_en if w.strip()], ensure_ascii=False))
    db.set_setting("radar_blacklist_en", json.dumps([b.strip() for b in bl_en if b.strip()], ensure_ascii=False))

    return {
        "success": True,
        "message": "Đã lưu cấu hình Radar Quét Khách thành công!"
    }


if __name__ == "__main__":
    import uvicorn
    import webbrowser

    is_frozen = getattr(sys, 'frozen', False)
    if is_frozen:
        def open_browser():
            import time
            time.sleep(1.2)
            try:
                webbrowser.open("http://127.0.0.1:8000")
            except Exception:
                pass
        threading.Thread(target=open_browser, daemon=True).start()

    print(f"\n{C_GREEN}{C_BOLD}[FB AUTO POSTER] Hệ thống đang chạy tại: http://127.0.0.1:8000{C_RESET}\n")
    uvicorn.run(app if is_frozen else "main:app", host="127.0.0.1", port=8000, reload=not is_frozen)
