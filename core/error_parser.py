import re
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from core.utils import get_vn_now, VN_TZ


def parse_facebook_error(
    response_status: int,
    response_text: str,
    response_headers: Optional[Dict[str, Any]] = None,
    account_fail_count: int = 1
) -> Dict[str, Any]:
    """
    Phân tích chi tiết lỗi từ Facebook:
    - Bóc tách mã lỗi, message, description, error_data
    - Tính toán thời gian hết hạn Rate Limit / Khóa tính năng (UTC+7)
    - Toàn bộ kết quả trả về là kiểu dữ liệu nguyên thủy (Primitive / JSON Serializable), không chứa đối tượng datetime.
    """
    headers = response_headers or {}
    now = get_vn_now()
    unlock_datetime: Optional[datetime] = None
    retry_after_seconds: Optional[int] = None

    parsed_result = {
        "error_type": "UNKNOWN",
        "error_code": None,
        "error_subcode": None,
        "summary": "Lỗi không xác định",
        "detail": "",
        "raw_response": response_text[:2000] if response_text else "",
        "is_rate_limit": False,
        "is_checkpoint": False,
        "retry_after_seconds": None,
        "unlock_time_utc7": None,
        "unlock_time_str": None,
        "duration_str": None
    }

    # 1. Kiểm tra HTTP Header 'Retry-After'
    retry_after_hdr = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after_hdr:
        try:
            seconds = int(retry_after_hdr)
            retry_after_seconds = seconds
            unlock_datetime = now + timedelta(seconds=seconds)
        except ValueError:
            pass

    # 2. Parse cấu trúc JSON GraphQL
    error_msg = ""
    error_data = {}
    try:
        data = json.loads(response_text)
        if isinstance(data, dict):
            if "errors" in data and isinstance(data["errors"], list) and len(data["errors"]) > 0:
                err = data["errors"][0]
                parsed_result["error_code"] = err.get("code")
                parsed_result["error_subcode"] = err.get("error_subcode")
                error_msg = str(err.get("message", ""))
                parsed_result["summary"] = err.get("summary") or error_msg
                parsed_result["detail"] = err.get("description") or error_msg
                error_data = err.get("error_data") or {}
            elif "error" in data:
                err = data["error"]
                if isinstance(err, dict):
                    parsed_result["error_code"] = err.get("code")
                    parsed_result["error_subcode"] = err.get("error_subcode")
                    error_msg = str(err.get("message", ""))
                    parsed_result["summary"] = error_msg
                    parsed_result["detail"] = str(err.get("error_user_msg") or error_msg)
                else:
                    error_msg = str(err)
                    parsed_result["summary"] = error_msg
    except Exception:
        error_msg = response_text or ""

    if not parsed_result["summary"] or parsed_result["summary"] == "Lỗi không xác định":
        if error_msg:
            parsed_result["summary"] = error_msg[:120]

    combined_text = f"{error_msg} {parsed_result['detail']} {parsed_result['summary']} {json.dumps(error_data, ensure_ascii=False, default=str)} {response_text}".lower()

    # 3. Phân loại nhóm lỗi
    # Kiểm tra mã lỗi 3809006 hoặc sự hiện diện của sentry_block_user_info
    has_sentry = (
        parsed_result.get("error_code") == 3809006 or 
        "sentry_block_user_info" in response_text or 
        "sentry_block_data" in response_text or
        "tính năng này không khả dụng ngay bây giờ" in combined_text
    )

    if has_sentry:
        parsed_result["error_type"] = "SENTRY_SHADOW_BLOCK"
        parsed_result["is_rate_limit"] = True
        # Phạt mặc định của Sentry: 48 giờ
        delta = timedelta(hours=48)
        unlock_dt = now + delta
        parsed_result["unlock_time_utc7"] = unlock_dt.isoformat()
        parsed_result["unlock_time_str"] = unlock_dt.strftime("%H:%M:%S %d/%m/%Y")
        parsed_result["duration_str"] = "48h 00m"
        parsed_result["retry_after_seconds"] = int(delta.total_seconds())
        parsed_result["summary"] = "Meta Sentry chặn tính năng đăng bài (Shadow Action Block)"
        parsed_result["detail"] = (
            "Hệ thống bảo vệ Meta Sentry đã khóa ngầm quyền tạo bài viết do tần suất đăng/đăng chéo cao. "
            "Tài khoản cần nghỉ tối thiểu 48h không thực hiện mutation để hạ nhiệt Risk Score."
        )
        return parsed_result

    if any(k in combined_text for k in ["rate limit", "rate limit exceeded", "1675004", "too many requests", "spam", "user request limit reached"]):
        parsed_result["error_type"] = "RATE_LIMIT"
        parsed_result["is_rate_limit"] = True
    elif any(k in combined_text for k in ["action_blocked", "tạm thời bị chặn", "temporarily blocked", "bị hạn chế", "restricted", "hạn chế tính năng"]):
        parsed_result["error_type"] = "ACTION_BLOCKED"
        parsed_result["is_rate_limit"] = True
    elif any(k in combined_text for k in ["checkpoint", "login", "cookie_expired", "session", "1357004", "error_subcode 458", "error_subcode 460"]):
        parsed_result["error_type"] = "COOKIE_EXPIRED"
        parsed_result["is_checkpoint"] = True

    # 4. Trích xuất thời gian mở khóa từ nội dung thông báo (Regex)
    if parsed_result["is_rate_limit"] and not unlock_datetime:
        # Mẫu 1: "thử lại sau X giờ/phút" hoặc "try again in X hours/minutes"
        duration_match = re.search(r'(?:thử lại sau|try again in|sau|trong)\s*(\d+)\s*(giây|phút|giờ|tiếng|ngày|seconds?|minutes?|hours?|days?)', combined_text)
        if duration_match:
            val = int(duration_match.group(1))
            unit = duration_match.group(2)
            if any(u in unit for u in ["giây", "second"]):
                delta = timedelta(seconds=val)
            elif any(u in unit for u in ["phút", "minute"]):
                delta = timedelta(minutes=val)
            elif any(u in unit for u in ["giờ", "tiếng", "hour"]):
                delta = timedelta(hours=val)
            else:
                delta = timedelta(days=val)
            unlock_datetime = now + delta
            retry_after_seconds = int(delta.total_seconds())

        # Mẫu 2: Bị chặn đến ngày cụ thể "đến ngày DD/MM lúc HH:mm" hoặc "until YYYY-MM-DD"
        if not unlock_datetime:
            date_match = re.search(r'(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?(?:\s+(?:lúc|at)\s+(\d{1,2}):(\d{1,2}))?', combined_text)
            if date_match:
                try:
                    day, month = int(date_match.group(1)), int(date_match.group(2))
                    year = int(date_match.group(3)) if date_match.group(3) else now.year
                    hour = int(date_match.group(4)) if date_match.group(4) else 0
                    minute = int(date_match.group(5)) if date_match.group(5) else 0
                    parsed_date = datetime(year, month, day, hour, minute, tzinfo=VN_TZ)
                    if parsed_date > now:
                        unlock_datetime = parsed_date
                        retry_after_seconds = int((parsed_date - now).total_seconds())
                except Exception:
                    pass

    # 5. Fallback Heuristic Ladder (Nếu Facebook không cấp thời gian cụ thể)
    if parsed_result["is_rate_limit"] and not unlock_datetime:
        # Bậc thang: Lần 1: 1 giờ | Lần 2: 4 giờ | Lần >= 3: 24 giờ
        ladder_hours = {1: 1, 2: 4}.get(account_fail_count, 24)
        delta = timedelta(hours=ladder_hours)
        unlock_datetime = now + delta
        retry_after_seconds = int(delta.total_seconds())
        if parsed_result["detail"]:
            parsed_result["detail"] += f" (Meta không công bố thời gian; áp dụng bậc thang ước tính: {ladder_hours} giờ)"
        else:
            parsed_result["detail"] = f"Meta không công bố thời gian; áp dụng bậc thang ước tính: {ladder_hours} giờ"

    # Format chuỗi hiển thị và đảm bảo không để object datetime trong dict trả về
    if unlock_datetime:
        parsed_result["retry_after_seconds"] = retry_after_seconds
        parsed_result["unlock_time_utc7"] = unlock_datetime.isoformat()
        parsed_result["unlock_time_str"] = unlock_datetime.strftime("%H:%M:%S %d/%m/%Y")
        total_sec = retry_after_seconds or 0
        h, rem = divmod(total_sec, 3600)
        m, s = divmod(rem, 60)
        parsed_result["duration_str"] = f"{h}h {m:02d}m" if h > 0 else f"{m}m {s:02d}s"
    else:
        parsed_result["unlock_time_utc7"] = None
        parsed_result["unlock_time_str"] = None
        parsed_result["retry_after_seconds"] = None
        parsed_result["duration_str"] = None

    return parsed_result
