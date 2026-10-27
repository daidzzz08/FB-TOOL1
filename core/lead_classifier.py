import re
from typing import Tuple, List, Optional

# Regex nhận diện cấu trúc câu tìm phòng tiếng Việt
VN_SEEKER_PATTERNS = [
    re.compile(r'(?:cần|muốn|đang)\s+(?:tìm|thuê|kiếm)\s+(?:phòng|căn|studio|trọ|nhà|chung cư)', re.IGNORECASE),
    re.compile(r'(?:tìm|thuê)\s+(?:gấp\s+)?(?:phòng|căn|studio|trọ|1pn|2pn)', re.IGNORECASE),
    re.compile(r'(?:ai\s+có|bên\s+mình\s+có)\s+(?:phòng|căn|studio)\s+.*(?:không|ko|inbox|ib)', re.IGNORECASE),
    re.compile(r'tài\s+chính\s+(?:tầm|khoảng|dưới|\d).*(?:quay\s+đầu|đổ\s+lại|triệu|tr\b)', re.IGNORECASE),
]

# Regex nhận diện cấu trúc tìm phòng tiếng Anh (Expat)
EN_SEEKER_PATTERNS = [
    re.compile(r'looking\s+for\s+(?:an?\s+)?(?:[\w\-]+\s+)?(?:apartment|studio|room|flat|place|1br|2br|house)', re.IGNORECASE),
    re.compile(r'(?:need|search(?:ing)?)\s+(?:for\s+)?(?:an?\s+)?(?:[\w\-]+\s+)?(?:apartment|studio|room|flat|house)', re.IGNORECASE),
    re.compile(r'looking\s+to\s+rent', re.IGNORECASE),
    re.compile(r'anyone\s+(?:have|know)\s+(?:an?\s+)?(?:apartment|studio|room)\s+available', re.IGNORECASE),
]

# Chặn tuyệt đối các từ khóa bất động sản bán
HARD_SALE_BLACKLIST = [
    "bán đất", "bán nhà", "chính chủ bán", "sổ đỏ", "sổ hồng", "đất nền", 
    "mua bán", "giá đầu tư", "cần bán", "lô đất", "for sale", "selling house"
]

# Bộ từ khóa mặc định Profile Khách Việt
DEFAULT_WHITELIST_VN = [
    "cần tìm phòng", "cần tìm căn", "cần thuê phòng", "cần thuê căn", "tìm phòng trọ",
    "tìm studio", "tìm 1pn", "tìm 2pn", "tìm căn hộ", "muốn thuê phòng",
    "ai có phòng", "có căn nào trống", "cần trọ", "tìm trọ"
]

DEFAULT_BLACKLIST_VN = [
    "bán đất", "bán nhà", "sổ đỏ", "sổ hồng", "đất nền", "chính chủ bán", "cần bán", "lô đất",
    "cho thuê", "chính chủ cho thuê", "pass phòng", "nhượng phòng", "nhượng cọc",
    "trống sẵn", "còn trống", "sẵn phòng", "tìm khách", "tìm bạn thuê", "tìm người thuê",
    "tìm nữ ở ghép", "tìm nam ở ghép"
]

# Bộ từ khóa mặc định Profile Khách Nước Ngoài (Expat)
DEFAULT_WHITELIST_EN = [
    "looking for apartment", "looking for a studio", "looking for a room",
    "looking for 1br", "looking for 2br", "looking for flat", "looking to rent",
    "searching for apartment", "need an apartment", "need a studio", "need a room",
    "any apartment available", "place to rent", "budget around", "moving to da nang",
    "rent in da nang", "long term rental", "short term rental"
]

DEFAULT_BLACKLIST_EN = [
    "for rent", "apartment for rent", "studio for rent", "room for rent",
    "available now", "ready to move in", "looking for tenant", "looking for guests",
    "for sale", "land for sale", "pm for details", "inbox for price",
    "whatsapp me", "contact via whatsapp"
]


def classify_lead_intent(
    text: str,
    target_profile: str = "ALL",  # 'VIETNAMESE', 'EXPAT', 'ALL'
    custom_whitelist_vn: Optional[List[str]] = None,
    custom_blacklist_vn: Optional[List[str]] = None,
    custom_whitelist_en: Optional[List[str]] = None,
    custom_blacklist_en: Optional[List[str]] = None
) -> Tuple[bool, Optional[str], List[str]]:
    """
    Phân loại bài viết có phải là khách hàng tìm thuê thực sự hay không.
    Trả về: (is_lead, client_type, matched_keywords)
    - client_type: 'VIETNAMESE' hoặc 'EXPAT'
    """
    if not text:
        return False, None, []

    text_lower = text.lower()

    # 1. Chặn ngay nếu chứa từ khóa bán đất/bán nhà
    if any(sale_kw in text_lower for sale_kw in HARD_SALE_BLACKLIST):
        return False, None, []

    matched_vn = []
    matched_en = []

    profile_upper = (target_profile or "ALL").upper()

    # 2. Kiểm tra Profile Khách Nước Ngoài (EXPAT)
    if profile_upper in ["EXPAT", "ALL"]:
        bl_en_source = custom_blacklist_en if custom_blacklist_en is not None else DEFAULT_BLACKLIST_EN
        bl_en = [b.lower() for b in bl_en_source if b and b.strip()]
        
        # Nếu không dính blacklist tiếng Anh
        if not any(b in text_lower for b in bl_en):
            # Khớp Regex cấu trúc tìm phòng tiếng Anh
            for pat in EN_SEEKER_PATTERNS:
                m = pat.search(text)
                if m:
                    val = m.group(0).strip()
                    if val.lower() not in [x.lower() for x in matched_en]:
                        matched_en.append(val)
            
            # Khớp Whitelist tiếng Anh
            wl_en_source = custom_whitelist_en if custom_whitelist_en is not None else DEFAULT_WHITELIST_EN
            if wl_en_source:
                for w in wl_en_source:
                    w_clean = w.strip()
                    if w_clean and w_clean.lower() in text_lower and w_clean.lower() not in [m.lower() for m in matched_en]:
                        matched_en.append(w_clean)

            # Quy tắc loại trừ: Nếu có "looking for" nhưng lại chứa "tenant" hoặc "guest" -> Loại bỏ
            if (
                re.search(r'looking\s+for\s+(?:\w+\s+){0,4}(?:tenant|guest)', text_lower) or
                "looking for tenant" in text_lower or 
                "looking for guests" in text_lower
            ):
                matched_en = []

            if matched_en:
                return True, "EXPAT", matched_en

    # 3. Kiểm tra Profile Khách Việt (VIETNAMESE)
    if profile_upper in ["VIETNAMESE", "ALL"]:
        bl_vn_source = custom_blacklist_vn if custom_blacklist_vn is not None else DEFAULT_BLACKLIST_VN
        bl_vn = [b.lower() for b in bl_vn_source if b and b.strip()]
        
        # Nếu dính từ khóa blacklist môi giới -> Loại bỏ
        if not any(b in text_lower for b in bl_vn):
            # Khớp Regex cấu trúc câu tìm phòng tiếng Việt
            for pat in VN_SEEKER_PATTERNS:
                m = pat.search(text)
                if m:
                    val = m.group(0).strip()
                    if val.lower() not in [x.lower() for x in matched_vn]:
                        matched_vn.append(val)
            
            # Khớp Whitelist tiếng Việt
            wl_vn_source = custom_whitelist_vn if custom_whitelist_vn is not None else DEFAULT_WHITELIST_VN
            if wl_vn_source:
                for w in wl_vn_source:
                    w_clean = w.strip()
                    if w_clean and w_clean.lower() in text_lower and w_clean.lower() not in [m.lower() for m in matched_vn]:
                        matched_vn.append(w_clean)

            # Quy tắc loại trừ nghịch lý tiếng Việt: "tìm khách", "tìm người thuê"
            if any(k in text_lower for k in ["tìm khách", "tìm người thuê", "tìm bạn thuê", "cần tìm khách"]):
                matched_vn = []

            if matched_vn:
                return True, "VIETNAMESE", matched_vn

    return False, None, []
