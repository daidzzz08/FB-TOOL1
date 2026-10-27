import sqlite3
import json
import re
import datetime
import zoneinfo
import sys
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Union

try:
    from core.utils import VN_TZ, get_vn_now, get_vn_time_str
except ImportError:
    VN_TZ = zoneinfo.ZoneInfo("Asia/Ho_Chi_Minh")

    def get_vn_now() -> datetime.datetime:
        """Trả về đối tượng datetime hiện tại theo múi giờ Việt Nam (UTC+7)"""
        return datetime.datetime.now(VN_TZ)

    def get_vn_time_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """Trả về chuỗi thời gian Việt Nam định dạng chuẩn"""
        return get_vn_now().strftime(fmt)

# Đảm bảo file CSDL SQLite luôn lưu tại thư mục bền vững bên ngoài gói bundle PyInstaller
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

DB_DIR = APP_DIR
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA_DIR / "app.db"

# Tự động đồng bộ CSDL từ database.db cũ sang data/app.db nếu data/app.db chưa tồn tại
OLD_DB_FILE = APP_DIR / "database.db"
if not DB_FILE.exists() and OLD_DB_FILE.exists():
    try:
        import shutil
        shutil.copy2(str(OLD_DB_FILE), str(DB_FILE))
        print(f"[DB] Đã đồng bộ dữ liệu cũ từ database.db sang {DB_FILE}")
    except Exception as e:
        print(f"[DB] Lỗi khi sao chép database cũ: {e}")

DB_PATH = str(DB_FILE)


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    """Initializes the database schema and performs migrations if needed."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Accounts table (Pure Cookie based)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cookie TEXT UNIQUE NOT NULL,
                token TEXT,
                uid TEXT,
                name TEXT,
                proxy TEXT,
                status TEXT DEFAULT 'LIVE',
                last_used DATETIME,
                rate_limited_until TIMESTAMP,
                rate_limit_hits INTEGER DEFAULT 0,
                last_error_summary TEXT,
                last_error_raw TEXT
            )
        """)

        # 2. Apartments table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS apartments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                spintax_content TEXT NOT NULL,
                image_paths TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now', '+7 hours'))
            )
        """)

        # 3. Post logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS post_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_preview TEXT,
                group_id TEXT NOT NULL,
                apartment_title TEXT NOT NULL,
                status TEXT NOT NULL,
                approval_status TEXT DEFAULT 'FAILED',
                message TEXT,
                debug_detail TEXT,
                raw_response TEXT,
                post_id TEXT,
                post_url TEXT,
                created_at DATETIME DEFAULT (datetime('now', '+7 hours'))
            )
        """)

        # 4. Group Profiles table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_profiles (
                group_id TEXT PRIMARY KEY,
                group_name TEXT,
                status TEXT NOT NULL,
                privacy TEXT DEFAULT 'PUBLIC',
                member_count INTEGER DEFAULT 0,
                url TEXT,
                last_checked DATETIME DEFAULT (datetime('now', '+7 hours'))
            )
        """)

        # 5. Group Presets table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                group_ids TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+7 hours'))
            )
        """)

        # 6. Post History table (Tracks daily posts to prevent duplicated apartment-group posts)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS post_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                apartment_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                posted_at DATE DEFAULT (date('now', '+7 hours')),
                status TEXT,
                post_url TEXT,
                UNIQUE(apartment_id, group_id, posted_at)
            )
        """)

        # 7. Post Queue table (For background batch processing and progress tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS post_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                apartment_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', '+7 hours'))
            )
        """)

        # 8. App Settings table (Key-Value configuration)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # 9. Leads table (Rental seeker leads from Groups feed)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT UNIQUE,
                post_url TEXT,
                author_id TEXT,
                author_name TEXT,
                author_url TEXT,
                group_id TEXT,
                group_name TEXT,
                content TEXT,
                created_time TIMESTAMP,
                scanned_at TIMESTAMP DEFAULT (datetime('now', '+7 hours')),
                matched_keywords TEXT,
                suggested_apartment_id INTEGER,
                phone TEXT,
                has_phone INTEGER DEFAULT 0,
                status TEXT DEFAULT 'NEW',
                client_type TEXT DEFAULT 'VIETNAMESE'
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_time ON leads(created_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
        
        # Auto-migration for leads table
        try:
            cursor.execute("ALTER TABLE leads ADD COLUMN phone TEXT")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE leads ADD COLUMN has_phone INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE leads ADD COLUMN client_type TEXT DEFAULT 'VIETNAMESE'")
        except Exception:
            pass
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_has_phone ON leads(has_phone)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_client_type ON leads(client_type)")

        # Initialize default settings
        default_settings = {
            "telegram_enabled": "1",
            "telegram_bot_token": "7644748535:AAF538KrGP7VO8-VAZhyKO3Jj_YB7pzViwQ",
            "telegram_chat_id": "1922773264",
            "telegram_alarm_interval": "30",
            "lead_radar_enabled": "0",
            "lead_radar_interval_minutes": "30",
            "lead_radar_max_hours": "24",
            "radar_target_profile": "ALL",
            "radar_whitelist_vn": json.dumps([
                "cần tìm phòng", "cần tìm căn", "cần thuê phòng", "cần thuê căn", "tìm phòng trọ",
                "tìm studio", "tìm 1pn", "tìm 2pn", "tìm căn hộ", "muốn thuê phòng",
                "ai có phòng", "có căn nào trống", "cần trọ", "tìm trọ"
            ], ensure_ascii=False),
            "radar_blacklist_vn": json.dumps([
                "bán đất", "bán nhà", "sổ đỏ", "sổ hồng", "đất nền", "chính chủ bán", "cần bán", "lô đất",
                "cho thuê", "chính chủ cho thuê", "pass phòng", "nhượng phòng", "nhượng cọc",
                "trống sẵn", "còn trống", "sẵn phòng", "tìm khách", "tìm bạn thuê", "tìm người thuê",
                "tìm nữ ở ghép", "tìm nam ở ghép"
            ], ensure_ascii=False),
            "radar_whitelist_en": json.dumps([
                "looking for apartment", "looking for a studio", "looking for a room",
                "looking for 1br", "looking for 2br", "looking for flat", "looking to rent",
                "searching for apartment", "need an apartment", "need a studio", "need a room",
                "any apartment available", "place to rent", "budget around", "moving to da nang",
                "rent in da nang", "long term rental", "short term rental"
            ], ensure_ascii=False),
            "radar_blacklist_en": json.dumps([
                "for rent", "apartment for rent", "studio for rent", "room for rent",
                "available now", "ready to move in", "looking for tenant", "looking for guests",
                "for sale", "land for sale", "pm for details", "inbox for price",
                "whatsapp me", "contact via whatsapp"
            ], ensure_ascii=False),
            "lead_radar_whitelist": json.dumps([
                "cần tìm phòng", "cần tìm căn", "cần thuê phòng", "cần thuê căn", "tìm phòng trọ",
                "tìm studio", "tìm 1pn", "tìm 2pn", "tìm căn hộ", "muốn thuê phòng",
                "ai có phòng", "có căn nào trống", "cần trọ", "tìm trọ"
            ], ensure_ascii=False),
            "lead_radar_blacklist": json.dumps([
                "bán đất", "bán nhà", "sổ đỏ", "sổ hồng", "đất nền", "chính chủ bán", "cần bán", "lô đất",
                "cho thuê", "chính chủ cho thuê", "pass phòng", "nhượng phòng", "nhượng cọc",
                "trống sẵn", "còn trống", "sẵn phòng", "tìm khách", "tìm bạn thuê", "tìm người thuê",
                "tìm nữ ở ghép", "tìm nam ở ghép"
            ], ensure_ascii=False)
        }
        for k, v in default_settings.items():
            cursor.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (k, v))

        # Column migrations for existing database if needed
        cursor.execute("PRAGMA table_info(accounts)")
        acc_cols = [row["name"] for row in cursor.fetchall()]
        if "cookie" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN cookie TEXT")
        if "uid" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN uid TEXT")
        if "name" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN name TEXT")
        if "proxy" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN proxy TEXT")
        if "token" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN token TEXT")
        if "status" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN status TEXT DEFAULT 'LIVE'")
        if "last_used" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN last_used DATETIME")
        if "rate_limited_until" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN rate_limited_until TIMESTAMP")
        if "rate_limit_hits" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN rate_limit_hits INTEGER DEFAULT 0")
        if "last_error_summary" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN last_error_summary TEXT")
        if "last_error_raw" not in acc_cols:
            cursor.execute("ALTER TABLE accounts ADD COLUMN last_error_raw TEXT")

        cursor.execute("PRAGMA table_info(post_logs)")
        cols = [row["name"] for row in cursor.fetchall()]
        if "debug_detail" not in cols:
            cursor.execute("ALTER TABLE post_logs ADD COLUMN debug_detail TEXT")
        if "raw_response" not in cols:
            cursor.execute("ALTER TABLE post_logs ADD COLUMN raw_response TEXT")
        if "approval_status" not in cols:
            cursor.execute("ALTER TABLE post_logs ADD COLUMN approval_status TEXT DEFAULT 'FAILED'")
        if "post_id" not in cols:
            cursor.execute("ALTER TABLE post_logs ADD COLUMN post_id TEXT")
        if "post_url" not in cols:
            cursor.execute("ALTER TABLE post_logs ADD COLUMN post_url TEXT")

        cursor.execute("PRAGMA table_info(group_profiles)")
        g_cols = [row["name"] for row in cursor.fetchall()]
        if "privacy" not in g_cols:
            cursor.execute("ALTER TABLE group_profiles ADD COLUMN privacy TEXT DEFAULT 'PUBLIC'")
        if "member_count" not in g_cols:
            cursor.execute("ALTER TABLE group_profiles ADD COLUMN member_count INTEGER DEFAULT 0")
        if "url" not in g_cols:
            try:
                cursor.execute("ALTER TABLE group_profiles ADD COLUMN url TEXT")
            except Exception:
                pass

        conn.commit()


# ==================== ACCOUNT CRUD (PURE COOKIE) ====================

def parse_account_line(raw_line: str) -> Optional[Dict[str, Optional[str]]]:
    """
    Parses an account line supporting multiple formats:
    - cookie (raw cookie string with c_user, xs, etc.)
    - cookie|proxy
    - cookie|token
    - cookie|token|proxy
    - token (fallback for backwards compatibility)
    """
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    parts = [p.strip() for p in line.split("|") if p.strip()]
    if not parts:
        return None

    cookie: Optional[str] = None
    proxy: Optional[str] = None
    token: Optional[str] = None
    uid: Optional[str] = None
    name: Optional[str] = None

    for p in parts:
        # Check if part is cookie (contains key FB cookie markers)
        if ("c_user=" in p or "xs=" in p or "sb=" in p or "datr=" in p or "fr=" in p) and ("=" in p):
            cookie = p
        # Check if part is proxy
        elif p.startswith("http://") or p.startswith("https://") or p.startswith("socks5://") or (":" in p and ";" not in p and "=" not in p and not p.startswith("EAA")):
            proxy = p
        # Check if part is token
        elif (p.startswith("EAA") or len(p) >= 40) and ";" not in p and "=" not in p:
            token = p
        elif not cookie and ("c_user=" in p or "xs=" in p):
            cookie = p

    # If only 1 part and no cookie matched yet, treat the entire line as cookie if it contains c_user/xs or key=value
    if not cookie and len(parts) == 1:
        if "c_user" in line or "xs" in line or "=" in line:
            cookie = line
        else:
            token = line

    if cookie:
        m = re.search(r'c_user=(\d+)', cookie)
        if m:
            uid = m.group(1)
        if not token:
            token = f"COOKIE_{uid or 'ACC'}"

    if not cookie and token:
        # Legacy token import without cookie
        cookie = f"token_only_{token[:15]}"
        uid = f"token_{token[-8:]}"

    if not cookie:
        return None

    return {
        "cookie": cookie,
        "token": token or f"COOKIE_{uid or 'ACC'}",
        "proxy": proxy,
        "uid": uid,
        "name": name
    }


def import_accounts(raw_lines: List[str], db_path: str = DB_PATH) -> Tuple[int, int]:
    """
    Imports account lines into the database supporting `cookie`, `cookie|proxy`, etc.
    Returns (imported_count, skipped_count).
    """
    imported = 0
    skipped = 0

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        for raw_line in raw_lines:
            acc_info = parse_account_line(raw_line)
            if not acc_info or not acc_info["cookie"]:
                continue

            cookie = acc_info["cookie"]
            token = acc_info["token"]
            proxy = acc_info["proxy"]
            uid = acc_info["uid"]

            try:
                cursor.execute(
                    "INSERT INTO accounts (cookie, token, proxy, uid, status) VALUES (?, ?, ?, ?, 'LIVE')",
                    (cookie, token, proxy, uid)
                )
                imported += 1
            except sqlite3.IntegrityError:
                # Account with this cookie already exists, update proxy, uid, status
                cursor.execute(
                    """
                    UPDATE accounts 
                    SET proxy = COALESCE(?, proxy),
                        uid = COALESCE(?, uid),
                        token = COALESCE(?, token),
                        status = 'LIVE'
                    WHERE cookie = ?
                    """,
                    (proxy, uid, token, cookie)
                )
                skipped += 1

        conn.commit()

    return imported, skipped

# Alias for backwards compatibility
import_tokens = import_accounts


def _enrich_account_dict(item: Dict[str, Any]) -> Dict[str, Any]:
    """Bổ sung các trường tính toán (preview, đếm ngược rate limit, status format) cho account."""
    cookie_val = item.get("cookie") or ""
    item["has_cookie"] = bool(cookie_val and ("c_user=" in cookie_val or "xs=" in cookie_val or len(cookie_val) > 15))
    item["cookie_preview"] = (cookie_val[:18] + "..." + cookie_val[-8:]) if len(cookie_val) > 28 else (cookie_val if cookie_val else "Chưa có")
    token_val = item.get("token") or ""
    item["token_preview"] = (token_val[:10] + "..." + token_val[-6:]) if len(token_val) > 16 else token_val

    # Parse Rate Limit Information
    rate_until_raw = item.get("rate_limited_until")
    is_rate_limited = False
    rate_limited_until_str = ""
    remaining_seconds = 0
    remaining_str = ""

    if rate_until_raw:
        now = get_vn_now()
        parsed_until = None
        if isinstance(rate_until_raw, datetime.datetime):
            parsed_until = rate_until_raw if rate_until_raw.tzinfo else rate_until_raw.replace(tzinfo=VN_TZ)
        else:
            val_str = str(rate_until_raw).strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    parsed_until = datetime.datetime.strptime(val_str, fmt).replace(tzinfo=VN_TZ)
                    break
                except ValueError:
                    pass
            if not parsed_until:
                try:
                    parsed_until = datetime.datetime.fromisoformat(val_str)
                    if not parsed_until.tzinfo:
                        parsed_until = parsed_until.replace(tzinfo=VN_TZ)
                    else:
                        parsed_until = parsed_until.astimezone(VN_TZ)
                except Exception:
                    parsed_until = None

        if parsed_until:
            rate_limited_until_str = parsed_until.strftime("%H:%M:%S %d/%m/%Y")
            if parsed_until > now:
                is_rate_limited = True
                delta = parsed_until - now
                remaining_seconds = int(delta.total_seconds())
                h, rem = divmod(remaining_seconds, 3600)
                m, s = divmod(rem, 60)
                remaining_str = f"{h}h {m:02d}m" if h > 0 else f"{m}m {s:02d}s"
            else:
                # Cooldown/Rate-limit has elapsed
                if item.get("status") in ("RATE_LIMITED", "SENTRY_COOLDOWN"):
                    item["status"] = "LIVE"

    item["is_rate_limited"] = is_rate_limited
    item["rate_limited_until_str"] = rate_limited_until_str
    item["remaining_seconds"] = remaining_seconds
    item["remaining_str"] = remaining_str
    return item


def get_accounts(status: Optional[str] = None, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves all accounts with cookie, proxy, and rate limit info."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, uid, name, cookie, proxy, status, last_used, token,
                   rate_limited_until, rate_limit_hits, last_error_summary, last_error_raw
            FROM accounts
        """
        if status:
            cursor.execute(f"{query} WHERE status = ? ORDER BY id DESC", (status,))
        else:
            cursor.execute(f"{query} ORDER BY id DESC")
        rows = cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            result.append(_enrich_account_dict(item))
        return result

# Alias for backwards compatibility
get_tokens = get_accounts
get_all_accounts = get_accounts


def get_account_by_id(acc_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieves a single account by ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, uid, name, cookie, proxy, status, last_used, token,
                   rate_limited_until, rate_limit_hits, last_error_summary, last_error_raw
            FROM accounts 
            WHERE id = ?
            """, 
            (acc_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return _enrich_account_dict(dict(row))


def record_account_rate_limit(
    account_id: int,
    unlock_time: Union[datetime.datetime, str],
    summary: str = "Rate limit exceeded",
    raw_resp: str = "",
    status: str = "RATE_LIMITED",
    db_path: str = DB_PATH
) -> None:
    """Ghi nhận tài khoản dính Rate Limit hoặc Meta Sentry Shadow Block, cập nhật mốc giờ mở khóa và tăng số lần vi phạm."""
    now_str = get_vn_time_str()
    if isinstance(unlock_time, datetime.datetime):
        unlock_str = unlock_time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        unlock_str = str(unlock_time)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE accounts 
            SET rate_limited_until = ?,
                rate_limit_hits = COALESCE(rate_limit_hits, 0) + 1,
                last_error_summary = ?,
                last_error_raw = ?,
                status = ?,
                last_used = ?
            WHERE id = ?
            """,
            (unlock_str, summary, raw_resp, status, now_str, account_id)
        )
        conn.commit()


def clear_account_rate_limit(account_id: int, db_path: str = DB_PATH) -> None:
    """Xóa bỏ cờ Rate Limit / Sentry cho tài khoản và đưa trạng thái về LIVE."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE accounts 
            SET rate_limited_until = NULL,
                rate_limit_hits = 0,
                last_error_summary = NULL,
                last_error_raw = NULL,
                status = CASE WHEN status IN ('RATE_LIMITED', 'SENTRY_COOLDOWN') THEN 'LIVE' ELSE status END
            WHERE id = ?
            """,
            (account_id,)
        )
        conn.commit()


def is_account_available(account_id: int, db_path: str = DB_PATH) -> Tuple[bool, Optional[str]]:
    """Kiểm tra tài khoản có đang sẵn sàng để đăng bài hay không (không bị DIE, CHECKPOINT, RATE_LIMITED, SENTRY_COOLDOWN)."""
    acc = get_account_by_id(account_id, db_path=db_path)
    if not acc:
        return False, "Tài khoản không tồn tại"
    
    status = acc.get("status", "LIVE")
    if status in ("DIE", "CHECKPOINT"):
        return False, f"Tài khoản đang ở trạng thái {status}"

    if status == "SENTRY_COOLDOWN":
        if acc.get("is_rate_limited"):
            rem_str = acc.get("remaining_str") or "48 giờ"
            unlock_str = acc.get("rate_limited_until_str") or "--"
            return False, f"Meta Sentry cách ly còn {rem_str} (mở lại lúc {unlock_str})"
        else:
            # Đã hết 48h cách ly, tự động hoàn nguyên LIVE
            update_account_status(account_id, "LIVE", db_path=db_path)
            return True, None

    if acc.get("is_rate_limited"):
        rem_str = acc.get("remaining_str") or "vài phút"
        unlock_str = acc.get("rate_limited_until_str") or "--"
        return False, f"Còn bị chặn {rem_str} (mở lại lúc {unlock_str})"
        
    return True, None


def update_account_status(account_id: int, status: str, db_path: str = DB_PATH) -> None:
    """Updates status for a specific account (LIVE, DIE, CHECKPOINT, RATE_LIMITED, SENTRY_COOLDOWN)."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE accounts SET status = ? WHERE id = ?",
            (status, account_id)
        )
        conn.commit()

# Alias for backwards compatibility
update_token_status = update_account_status


def update_account_last_used(account_id: int, db_path: str = DB_PATH) -> None:
    """Updates last_used timestamp to current local Vietnam time."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        now_str = get_vn_time_str()
        cursor.execute(
            "UPDATE accounts SET last_used = ? WHERE id = ?",
            (now_str, account_id)
        )
        conn.commit()

# Alias for backwards compatibility
update_token_last_used = update_account_last_used


def delete_dead_accounts(db_path: str = DB_PATH) -> int:
    """Deletes all accounts marked as DIE or CHECKPOINT."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM accounts WHERE status NOT IN ('LIVE', 'RATE_LIMITED', 'SENTRY_COOLDOWN')")
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count

# Alias for backwards compatibility
delete_dead_tokens = delete_dead_accounts


def delete_account(account_id: int, db_path: str = DB_PATH) -> bool:
    """Deletes an account by ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
        return cursor.rowcount > 0

# Alias for backwards compatibility
delete_token = delete_account


# ==================== GROUP PRESETS CRUD ====================

def save_preset(name: str, group_ids: List[str], db_path: str = DB_PATH) -> int:
    """
    Saves or updates a group preset configuration with list of group IDs.
    Returns preset ID.
    """
    clean_name = name.strip()
    clean_gids = [str(g).strip() for g in group_ids if str(g).strip()]
    json_gids = json.dumps(clean_gids, ensure_ascii=True, default=str)
    now_str = get_vn_time_str()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO group_presets (name, group_ids, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                group_ids = excluded.group_ids,
                created_at = ?
            """,
            (clean_name, json_gids, now_str, now_str)
        )
        conn.commit()
        return cursor.lastrowid or 0


def get_presets(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves all saved group presets."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, group_ids, created_at FROM group_presets ORDER BY id DESC")
        rows = cursor.fetchall()
        result = []
        for r in rows:
            item = dict(r)
            raw_gids = item.get("group_ids") or "[]"
            try:
                gids = json.loads(raw_gids) if raw_gids.startswith("[") else [g.strip() for g in raw_gids.split(",") if g.strip()]
            except Exception:
                gids = []
            item["group_ids"] = gids
            item["group_count"] = len(gids)
            result.append(item)
        return result


def delete_preset(preset_id: int, db_path: str = DB_PATH) -> bool:
    """Deletes a preset by ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM group_presets WHERE id = ?", (preset_id,))
        conn.commit()
        return cursor.rowcount > 0


# Aliases for preset operations
save_group_preset = save_preset
get_group_presets = get_presets
delete_group_preset = delete_preset


# ==================== POST HISTORY & QUEUE ====================

def record_post_history(
    account_id: str,
    apartment_id: int,
    group_id: str,
    status: str,
    post_url: Optional[str] = None,
    db_path: str = DB_PATH
) -> int:
    """Records a post history item to prevent duplicate posting on the same day."""
    now_date = get_vn_time_str("%Y-%m-%d")
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO post_history (account_id, apartment_id, group_id, posted_at, status, post_url)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(apartment_id, group_id, posted_at) DO UPDATE SET
                status = excluded.status,
                post_url = COALESCE(excluded.post_url, post_history.post_url),
                account_id = excluded.account_id
            """,
            (str(account_id), int(apartment_id), str(group_id), now_date, status, post_url)
        )
        conn.commit()
        return cursor.lastrowid or 0


def is_already_posted_today(apartment_id: int, group_id: str, db_path: str = DB_PATH) -> bool:
    """Checks if an apartment has already been successfully posted to a group today."""
    now_date = get_vn_time_str("%Y-%m-%d")
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM post_history
            WHERE apartment_id = ? AND group_id = ? AND posted_at = ? AND status = 'SUCCESS'
            """,
            (int(apartment_id), str(group_id), now_date)
        )
        count = cursor.fetchone()[0]
        return count > 0


def create_post_queue_batch(batch_id: str, tasks: List[Dict[str, Any]], db_path: str = DB_PATH) -> int:
    """Creates a batch of tasks in post_queue."""
    if not tasks:
        return 0
    now_str = get_vn_time_str()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        for t in tasks:
            cursor.execute(
                """
                INSERT INTO post_queue (batch_id, account_id, apartment_id, group_id, status, created_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?)
                """,
                (str(batch_id), str(t["account_id"]), int(t["apartment_id"]), str(t["group_id"]), now_str)
            )
        conn.commit()
        return len(tasks)


def get_post_history(limit: int = 100, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves recent post history."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, account_id, apartment_id, group_id, posted_at, status, post_url
            FROM post_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        return [dict(r) for r in cursor.fetchall()]


def clear_post_history(db_path: str = DB_PATH) -> int:
    """Clears all post history records."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM post_history")
        deleted = cursor.rowcount
        conn.commit()
        return deleted


# ==================== APARTMENTS CRUD ====================

def save_apartment(
    title: str,
    spintax_content: str,
    image_paths: List[str],
    db_path: str = DB_PATH
) -> int:
    """Creates a new apartment template."""
    now_str = get_vn_time_str()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        images_json = json.dumps(image_paths, ensure_ascii=False, default=str)
        cursor.execute(
            "INSERT INTO apartments (title, spintax_content, image_paths, created_at) VALUES (?, ?, ?, ?)",
            (title, spintax_content, images_json, now_str)
        )
        conn.commit()
        return cursor.lastrowid


def get_apartments(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves all apartment templates."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, spintax_content, image_paths, created_at FROM apartments ORDER BY id DESC")
        rows = cursor.fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["image_paths"] = json.loads(item["image_paths"])
            except Exception:
                item["image_paths"] = []
            results.append(item)
        return results


def get_apartment_by_id(apartment_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieves a single apartment by ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, spintax_content, image_paths, created_at FROM apartments WHERE id = ?",
            (apartment_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["image_paths"] = json.loads(item["image_paths"])
        except Exception:
            item["image_paths"] = []
        return item


def update_apartment(
    apartment_id: int,
    title: str,
    spintax_content: str,
    image_paths: List[str],
    db_path: str = DB_PATH
) -> bool:
    """Updates an existing apartment template."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        images_json = json.dumps(image_paths, ensure_ascii=False, default=str)
        cursor.execute(
            "UPDATE apartments SET title = ?, spintax_content = ?, image_paths = ? WHERE id = ?",
            (title, spintax_content, images_json, apartment_id)
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_apartment(apartment_id: int, db_path: str = DB_PATH) -> bool:
    """Deletes an apartment template by ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM apartments WHERE id = ?", (apartment_id,))
        conn.commit()
        return cursor.rowcount > 0


# ==================== GROUP PROFILES & CLASSIFICATION ====================

def upsert_group_status(
    group_id: str,
    status: str,
    group_name: Optional[str] = None,
    db_path: str = DB_PATH
) -> None:
    """Inserts or updates group approval classification."""
    clean_id = str(group_id).strip()
    status_upper = status.upper() if status else "FAILED"
    now_str = get_vn_time_str()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO group_profiles (group_id, group_name, status, last_checked)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                status = excluded.status,
                group_name = COALESCE(excluded.group_name, group_profiles.group_name),
                last_checked = ?
            """,
            (clean_id, group_name, status_upper, now_str, now_str)
        )
        conn.commit()


def upsert_scanned_group(
    group_id: str,
    name: str,
    status: str = "AUTO_APPROVED",
    privacy: str = "PUBLIC",
    member_count: int = 0,
    url: Optional[str] = None,
    db_path: str = DB_PATH
) -> None:
    """Inserts or updates scanned Facebook Group information."""
    clean_id = str(group_id).strip()
    group_url = url or f"https://www.facebook.com/groups/{clean_id}"
    now_str = get_vn_time_str()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO group_profiles (group_id, group_name, status, privacy, member_count, url, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                group_name = excluded.group_name,
                status = excluded.status,
                privacy = excluded.privacy,
                member_count = excluded.member_count,
                url = excluded.url,
                last_checked = ?
            """,
            (clean_id, name, status, privacy, member_count, group_url, now_str, now_str)
        )
        conn.commit()


def get_groups_by_status(
    status: str = "ALL",
    db_path: str = DB_PATH
) -> List[Dict[str, Any]]:
    """Retrieves groups filtered by status ('AUTO_APPROVED', 'PENDING', 'FAILED', or 'ALL')."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        if status and status.upper() != "ALL":
            cursor.execute(
                "SELECT group_id, group_name, status, privacy, member_count, url, last_checked FROM group_profiles WHERE status = ? ORDER BY last_checked DESC",
                (status.upper(),)
            )
        else:
            cursor.execute(
                "SELECT group_id, group_name, status, privacy, member_count, url, last_checked FROM group_profiles ORDER BY last_checked DESC"
            )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def delete_group(group_id: str, db_path: str = DB_PATH) -> bool:
    """Deletes a group from group_profiles by group_id."""
    clean_id = str(group_id).strip()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM group_profiles WHERE group_id = ?", (clean_id,))
        conn.commit()
        return cursor.rowcount > 0


def delete_groups_batch(group_ids: List[str], db_path: str = DB_PATH) -> int:
    """Deletes multiple groups from group_profiles by group_ids."""
    clean_ids = [str(gid).strip() for gid in group_ids if str(gid).strip()]
    if not clean_ids:
        return 0
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in clean_ids)
        cursor.execute(f"DELETE FROM group_profiles WHERE group_id IN ({placeholders})", clean_ids)
        conn.commit()
        return cursor.rowcount


def get_groups_summary(db_path: str = DB_PATH) -> Dict[str, int]:
    """Retrieves aggregate group count summary by approval classification."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM group_profiles")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM group_profiles WHERE status = 'AUTO_APPROVED'")
        auto_approved = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM group_profiles WHERE status = 'PENDING'")
        pending = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM group_profiles WHERE status = 'FAILED'")
        failed = cursor.fetchone()[0]

        return {
            "total": total,
            "auto_approved": auto_approved,
            "pending": pending,
            "failed": failed
        }


# ==================== POST LOGS CRUD ====================

def add_post_log(
    token_preview: str,
    group_id: str,
    apartment_title: str,
    status: str,
    message: Optional[str] = None,
    approval_status: str = "FAILED",
    debug_detail: Optional[Union[Dict, str]] = None,
    raw_response: Optional[str] = None,
    post_id: Optional[str] = None,
    post_url: Optional[str] = None,
    db_path: str = DB_PATH
) -> int:
    """Inserts a new post execution log record with debug detail and raw response."""
    debug_str = None
    if debug_detail:
        if isinstance(debug_detail, (dict, list)):
            try:
                debug_str = json.dumps(debug_detail, ensure_ascii=False, default=str)
            except Exception:
                debug_str = str(debug_detail)
        else:
            debug_str = str(debug_detail)

    now_str = get_vn_time_str()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO post_logs (token_preview, group_id, apartment_title, status, approval_status, message, debug_detail, raw_response, post_id, post_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token_preview, group_id, apartment_title, status, approval_status, message, debug_str, raw_response, post_id, post_url, now_str)
        )
        conn.commit()
        return cursor.lastrowid


def get_recent_logs(limit: int = 50, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieves recent post execution logs."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, token_preview, group_id, apartment_title, status, approval_status, message, debug_detail, raw_response, post_id, post_url, created_at
            FROM post_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            item = dict(r)
            if item.get("debug_detail"):
                try:
                    item["debug_detail"] = json.loads(item["debug_detail"])
                except Exception:
                    pass
            results.append(item)
        return results


def clear_logs(db_path: str = DB_PATH) -> int:
    """Clears all post logs."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM post_logs")
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count


# ==================== APP SETTINGS HELPERS ====================

def get_setting(key: str, default: str = "", db_path: str = DB_PATH) -> str:
    """Retrieves a configuration setting by key."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row and row["value"] is not None:
                return str(row["value"])
    except Exception:
        pass
    return default


def set_setting(key: str, value: str, db_path: str = DB_PATH) -> None:
    """Sets or updates a configuration setting."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        conn.commit()


def get_all_settings(db_path: str = DB_PATH) -> Dict[str, str]:
    """Retrieves all configuration settings as a key-value dictionary."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM app_settings")
            return {str(row["key"]): str(row["value"] or "") for row in cursor.fetchall()}
    except Exception:
        return {}


# ==================== LEAD RADAR CRUD ====================

def insert_lead(lead_data: Dict[str, Any], db_path: str = DB_PATH) -> bool:
    """
    Inserts a new rental seeker lead into the leads table.
    Returns True if newly inserted, False if post_id already exists.
    """
    post_id = str(lead_data.get("post_id", "")).strip()
    if not post_id:
        return False

    post_url = lead_data.get("post_url") or f"https://www.facebook.com/{post_id}"
    author_id = str(lead_data.get("author_id") or "")
    author_name = str(lead_data.get("author_name") or "Khách hàng Facebook")
    author_url = str(lead_data.get("author_url") or "")
    group_id = str(lead_data.get("group_id") or "")
    group_name = str(lead_data.get("group_name") or "")
    content = str(lead_data.get("content") or "")
    created_time = lead_data.get("created_time") or get_vn_time_str()
    matched_keywords = str(lead_data.get("matched_keywords") or "")
    suggested_apt_id = lead_data.get("suggested_apartment_id")
    phone = lead_data.get("phone")
    has_phone = int(lead_data.get("has_phone") or (1 if phone else 0))
    status = lead_data.get("status") or "NEW"
    client_type = str(lead_data.get("client_type") or "VIETNAMESE").upper()
    scanned_at = get_vn_time_str()

    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO leads (
                    post_id, post_url, author_id, author_name, author_url,
                    group_id, group_name, content, created_time, scanned_at,
                    matched_keywords, suggested_apartment_id, phone, has_phone, status, client_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    content = excluded.content,
                    matched_keywords = excluded.matched_keywords,
                    phone = COALESCE(excluded.phone, leads.phone),
                    has_phone = MAX(excluded.has_phone, leads.has_phone),
                    client_type = excluded.client_type
                """,
                (
                    post_id, post_url, author_id, author_name, author_url,
                    group_id, group_name, content, created_time, scanned_at,
                    matched_keywords, suggested_apt_id, phone, has_phone, status, client_type
                )
            )
            inserted = cursor.rowcount > 0
            conn.commit()
            return inserted
    except Exception as e:
        print(f"[DB] Lỗi chèn lead: {e}")
        return False


def get_leads(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    only_has_phone: bool = False,
    client_type: Optional[str] = "ALL",
    db_path: str = DB_PATH
) -> List[Dict[str, Any]]:
    """Retrieves rental seeker leads with optional status, keyword, phone, and client_type filtering."""
    query = "SELECT * FROM leads"
    params: List[Any] = []
    conditions = []

    if status and status != "ALL":
        conditions.append("status = ?")
        params.append(status)

    if only_has_phone:
        conditions.append("has_phone = 1")

    if client_type and client_type != "ALL":
        conditions.append("client_type = ?")
        params.append(client_type.upper())

    if keyword and keyword.strip():
        conditions.append("(content LIKE ? OR matched_keywords LIKE ? OR author_name LIKE ? OR group_name LIKE ? OR phone LIKE ?)")
        kw_pattern = f"%{keyword.strip()}%"
        params.extend([kw_pattern, kw_pattern, kw_pattern, kw_pattern, kw_pattern])

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_leads_count(
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    only_has_phone: bool = False,
    client_type: Optional[str] = "ALL",
    db_path: str = DB_PATH
) -> int:
    """Gets total count of leads matching filter."""
    query = "SELECT COUNT(*) FROM leads"
    params: List[Any] = []
    conditions = []

    if status and status != "ALL":
        conditions.append("status = ?")
        params.append(status)

    if only_has_phone:
        conditions.append("has_phone = 1")

    if client_type and client_type != "ALL":
        conditions.append("client_type = ?")
        params.append(client_type.upper())

    if keyword and keyword.strip():
        conditions.append("(content LIKE ? OR matched_keywords LIKE ? OR author_name LIKE ? OR group_name LIKE ? OR phone LIKE ?)")
        kw_pattern = f"%{keyword.strip()}%"
        params.extend([kw_pattern, kw_pattern, kw_pattern, kw_pattern, kw_pattern])

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return row[0] if row else 0


def update_lead_status(post_id: str, status: str, db_path: str = DB_PATH) -> bool:
    """Updates the status of a lead ('NEW', 'CONTACTED', 'IGNORED')."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE leads SET status = ? WHERE post_id = ? OR id = ?", (status, str(post_id), str(post_id)))
        conn.commit()
        return cursor.rowcount > 0


def delete_lead(lead_id: Union[int, str], db_path: str = DB_PATH) -> bool:
    """Deletes a lead by ID or post_id."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM leads WHERE id = ? OR post_id = ?", (str(lead_id), str(lead_id)))
        conn.commit()
        return cursor.rowcount > 0


def clear_leads(db_path: str = DB_PATH) -> int:
    """Clears all leads from database."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM leads")
        deleted = cursor.rowcount
        conn.commit()
        return deleted


def get_lead_stats(db_path: str = DB_PATH) -> Dict[str, int]:
    """Returns statistics summary of leads."""
    stats = {"total": 0, "new": 0, "contacted": 0, "ignored": 0, "has_phone": 0, "vietnamese": 0, "expat": 0}
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) as cnt FROM leads GROUP BY status")
        for row in cursor.fetchall():
            st = (row["status"] or "").lower()
            if st in stats:
                stats[st] = row["cnt"]
        cursor.execute("SELECT client_type, COUNT(*) as cnt FROM leads GROUP BY client_type")
        for row in cursor.fetchall():
            ct = (row["client_type"] or "").lower()
            if ct in stats:
                stats[ct] = row["cnt"]
        cursor.execute("SELECT COUNT(*) FROM leads WHERE has_phone = 1")
        row = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM leads")
        row = cursor.fetchone()
        stats["total"] = row[0] if row else 0
    return stats


