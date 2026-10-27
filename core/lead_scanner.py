import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Callable
from curl_cffi import requests
from core.utils import (
    DESKTOP_USER_AGENT,
    compute_jazoest,
    extract_dtsg_from_html,
    get_vn_now,
    VN_TZ
)

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
DOC_ID_GROUP_FEED = "39504340555823557"
DOC_ID_CROSS_GROUP_FEED = "29331463406455030"

DEFAULT_RELAY_PVS = {
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__CometFeedStory_enable_reactor_facepilerelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_social_bubblesrelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_post_permalink_white_space_clickrelayprovider": False,
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__TestPilotShouldIncludeDemoAdUseCaserelayprovider": False,
    "__relay_internal__pv__FBReels_deprecate_short_form_video_context_gkrelayprovider": True,
    "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
    "__relay_internal__pv__CometFeedShareMedia_shouldPrefetchShareImagerelayprovider": False,
    "__relay_internal__pv__CometImmersivePhotoCanUserDisable3DMotionrelayprovider": False,
    "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
    "__relay_internal__pv__IsMergQAPollsrelayprovider": False,
    "__relay_internal__pv__FBReelsMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": True,
    "__relay_internal__pv__relay_provider_comet_ufi_ssr_seo_deferrelayprovider": True,
    "__relay_internal__pv__ReelsIFUCard_reelsIFULikeCountrelayprovider": False,
    "__relay_internal__pv__FBReelsIFUTileContent_reelsIFUPlayOnHoverrelayprovider": True,
    "__relay_internal__pv__GroupsCometGYSJFeedItemHeightrelayprovider": 206,
    "__relay_internal__pv__StoriesShouldEnablePhotosensitiveContentWarningrelayprovider": False,
    "__relay_internal__pv__ShouldEnableBakedInTextStoriesrelayprovider": False,
    "__relay_internal__pv__StoriesShouldIncludeFbNotesrelayprovider": True,
    "__relay_internal__pv__GroupsCometGroupChatLazyLoadLastMessageSnippetrelayprovider": False,
    "__relay_internal__pv__groups_comet_use_glvrelayprovider": False
}

from core.lead_classifier import (
    classify_lead_intent,
    DEFAULT_WHITELIST_VN,
    DEFAULT_BLACKLIST_VN,
    DEFAULT_WHITELIST_EN,
    DEFAULT_BLACKLIST_EN
)

DEFAULT_WHITELIST = DEFAULT_WHITELIST_VN
DEFAULT_BLACKLIST = DEFAULT_BLACKLIST_VN

VIETNAM_PHONE_REGEX = re.compile(
    r'(?:\+?84|0)(?:3[2-9]|5[6|8|9]|7[0|6-9]|8[1-9]|9[0-9])[\s\.\-_]?(?:\d[\s\.\-_]?){7}\b'
)


def extract_phone_numbers(text: str) -> List[str]:
    """
    Trích xuất và chuẩn hóa các số điện thoại di động Việt Nam từ văn bản:
    - Nhận diện các đầu số: 03, 05, 07, 08, 09 hoặc +84
    - Loại bỏ dấu chấm, dấu cách, dấu gạch ngang
    - Chuẩn hóa các ký tự gõ lách phổ biến (o, O -> 0)
    - Chuẩn hóa về định dạng 10 chữ số (09xxxxxxxx)
    """
    if not text:
        return []
    
    cleaned_text = text
    for _ in range(3):
        cleaned_text = re.sub(r'(?<=\d)[oO]|[oO](?=\d)|\b[oO](?=[35789])', '0', cleaned_text)
    
    matches = VIETNAM_PHONE_REGEX.findall(cleaned_text)
    valid_phones = []
    
    for match in matches:
        digits = re.sub(r'\D', '', match)
        if digits.startswith('84') and len(digits) == 11:
            digits = '0' + digits[2:]
            
        if len(digits) == 10 and digits.startswith(('03', '05', '07', '08', '09')):
            if digits not in valid_phones:
                valid_phones.append(digits)
                
    return valid_phones


class RadarController:
    """Bộ điều khiển trạng thái đa luồng cho Radar Quét Lead (Pause / Resume / Stop)."""
    def __init__(self):
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

    def stop(self):
        self._stop_event.set()
        self._pause_event.clear()

    def pause(self):
        self._pause_event.set()

    def resume(self):
        self._pause_event.clear()

    def reset(self):
        self._stop_event.clear()
        self._pause_event.clear()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def wait_if_paused(self, check_interval: float = 0.5):
        """Nếu đang bị tạm dừng, block worker cho đến khi được resume hoặc stop"""
        while self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(check_interval)


def check_is_pinned(node: dict) -> bool:
    """Kiểm tra bài viết có phải bài ghim (Pinned / Featured story) hay không."""
    if not isinstance(node, dict):
        return False
    if node.get("is_pinned") or node.get("pinned_story"):
        return True
    c_story = node.get("comet_sections", {}).get("context_layout", {}).get("story", {})
    if isinstance(c_story, dict) and (c_story.get("is_pinned") or c_story.get("pinned_story")):
        return True
    return False


def parse_relay_ndjson_response(raw_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Parse toàn bộ các dòng NDJSON/Chunks trả về từ Relay GraphQL của Facebook.
    Thu thập tất cả edges và tìm page_info (end_cursor, has_next_page, errors).
    """
    edges: List[Dict[str, Any]] = []
    page_info: Dict[str, Any] = {"has_next_page": False, "end_cursor": None, "errors": []}

    if not raw_text:
        return edges, page_info

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("for (;;);"):
            line = line[len("for (;;);"):].strip()
        try:
            chunk = json.loads(line)
        except Exception:
            continue

        if not isinstance(chunk, dict):
            continue

        # Bắt lỗi từ Meta nếu có
        if "errors" in chunk and isinstance(chunk["errors"], list):
            for err in chunk["errors"]:
                if isinstance(err, dict) and err.get("message"):
                    page_info["errors"].append(err["message"])
                elif isinstance(err, str):
                    page_info["errors"].append(err)

        # 1. Lấy edges từ khối chính ban đầu (nếu có)
        node_data = chunk.get("data", {}).get("node", {}) if isinstance(chunk.get("data"), dict) else {}
        if isinstance(node_data, dict) and "group_feed" in node_data:
            feed = node_data["group_feed"]
            if isinstance(feed, dict):
                if "edges" in feed and isinstance(feed["edges"], list):
                    edges.extend(feed["edges"])
                if "page_info" in feed and isinstance(feed["page_info"], dict):
                    page_info.update(feed["page_info"])

        # 2. Lấy edges từ các streaming fragment được push về sau:
        chunk_data = chunk.get("data", {})
        if isinstance(chunk_data, dict):
            if "node" in chunk_data and isinstance(chunk_data.get("node"), dict):
                c_node = chunk_data["node"]
                if c_node.get("__typename") == "Story" or c_node.get("post_id") or c_node.get("id"):
                    edges.append({"node": c_node})

            # 3. Lấy page_info từ deferred chunk:
            if "page_info" in chunk_data and isinstance(chunk_data["page_info"], dict):
                page_info.update(chunk_data["page_info"])

    return edges, page_info


def extract_author_info(node: dict) -> Tuple[str, str, str]:
    """Trả về (author_id, author_name, author_url) an toàn kể cả bài viết ẩn danh (Anonymous Post)."""
    if not isinstance(node, dict):
        return ("0", "Người tham gia ẩn danh", "")

    actors = node.get("actors", [])
    if actors and isinstance(actors, list) and len(actors) > 0 and isinstance(actors[0], dict):
        actor = actors[0]
        return (
            str(actor.get("id", "")),
            actor.get("name", "Người dùng Facebook"),
            actor.get("url", "") or ""
        )

    # Fallback cho bài viết ẩn danh (Anonymous Post)
    story_context = node.get("comet_sections", {}).get("context_layout", {}).get("story", {})
    if isinstance(story_context, dict):
        context_actors = story_context.get("actors", [])
        if context_actors and isinstance(context_actors, list) and len(context_actors) > 0 and isinstance(context_actors[0], dict):
            actor = context_actors[0]
            return (
                str(actor.get("id", "")),
                actor.get("name", "Người tham gia ẩn danh"),
                actor.get("url", "") or ""
            )

    # Thử từ feedback.owning_profile
    feedback = node.get("feedback", {})
    if isinstance(feedback, dict) and feedback.get("owning_profile"):
        prof = feedback["owning_profile"]
        if isinstance(prof, dict):
            return (
                str(prof.get("id", "")),
                prof.get("name") or "Người tham gia ẩn danh",
                f"https://www.facebook.com/{prof.get('id')}" if prof.get("id") else ""
            )

    return ("0", "Người tham gia ẩn danh", "")


def extract_creation_time(node: dict) -> int:
    """Trích xuất Unix timestamp của bài viết từ đa dạng vị trí GraphQL."""
    if not isinstance(node, dict):
        return int(time.time())

    if node.get("creation_time"):
        try:
            return int(node["creation_time"])
        except (ValueError, TypeError):
            pass

    story = node.get("story", {})
    if isinstance(story, dict) and story.get("creation_time"):
        try:
            return int(story["creation_time"])
        except (ValueError, TypeError):
            pass

    try:
        ts = node.get("comet_sections", {}).get("context_layout", {}).get("story", {}).get("creation_time")
        if ts:
            return int(ts)
    except (KeyError, TypeError, ValueError):
        pass

    try:
        metadata = (
            node.get("comet_sections", {})
            .get("context_layout", {})
            .get("story", {})
            .get("comet_sections", {})
            .get("metadata", [])
        )
        if isinstance(metadata, list):
            for m in metadata:
                if isinstance(m, dict):
                    ts = m.get("story", {}).get("creation_time")
                    if ts:
                        return int(ts)
    except (KeyError, TypeError, ValueError):
        pass

    try:
        ts = node.get("comet_sections", {}).get("content", {}).get("story", {}).get("creation_time")
        if ts:
            return int(ts)
    except (KeyError, TypeError, ValueError):
        pass

    return int(time.time())


def extract_post_text(node: dict) -> str:
    """Trích xuất nội dung text bài viết đa tầng (Multi-tier Fallback bao gồm attachments và accessibility captions)."""
    if not isinstance(node, dict):
        return ""

    text_parts: List[str] = []

    try:
        story = node.get("comet_sections", {}).get("content", {}).get("story", {})
        if isinstance(story, dict):
            msg = story.get("message")
            if isinstance(msg, dict) and msg.get("text"):
                text_parts.append(str(msg["text"]).strip())

            if isinstance(msg, dict):
                inner_msg = msg.get("story", {}).get("message", {})
                if isinstance(inner_msg, dict) and inner_msg.get("text"):
                    text_parts.append(str(inner_msg["text"]).strip())

            c_sections = story.get("comet_sections", {})
            if isinstance(c_sections, dict):
                msg_sec = c_sections.get("message", {}).get("story", {}).get("message", {})
                if isinstance(msg_sec, dict) and msg_sec.get("text"):
                    text_parts.append(str(msg_sec["text"]).strip())

                msg_cnt = c_sections.get("message_container", {}).get("story", {}).get("message", {})
                if isinstance(msg_cnt, dict) and msg_cnt.get("text"):
                    text_parts.append(str(msg_cnt["text"]).strip())
    except Exception:
        pass

    try:
        msg = node.get("message")
        if isinstance(msg, dict) and msg.get("text"):
            text_parts.append(str(msg["text"]).strip())
        elif isinstance(msg, str) and msg.strip():
            text_parts.append(msg.strip())
    except Exception:
        pass

    try:
        attachments = (
            node.get("attachments", [])
            or (node.get("comet_sections", {}).get("content", {}).get("story", {}).get("attachments", []) if isinstance(node.get("comet_sections"), dict) else [])
        )
        if attachments and isinstance(attachments, list):
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                desc = (
                    att.get("description", {}).get("text")
                    or att.get("title", {}).get("text")
                    or att.get("source", {}).get("text")
                )
                if desc and str(desc).strip():
                    text_parts.append(str(desc).strip())
                
                styles = att.get("styles", {}).get("attachment", {}) if isinstance(att.get("styles"), dict) else {}
                if isinstance(styles, dict):
                    media = styles.get("media", {})
                    if isinstance(media, dict) and media.get("accessibility_caption"):
                        text_parts.append(str(media["accessibility_caption"]).strip())
                    sub_att = styles.get("all_subattachments", {}).get("nodes", []) if isinstance(styles.get("all_subattachments"), dict) else []
                    if isinstance(sub_att, list):
                        for sa in sub_att:
                            if isinstance(sa, dict) and sa.get("media", {}).get("accessibility_caption"):
                                text_parts.append(str(sa["media"]["accessibility_caption"]).strip())
    except Exception:
        pass

    try:
        att_story = (
            node.get("attached_story")
            or (node.get("comet_sections", {}).get("content", {}).get("story", {}).get("attached_story") if isinstance(node.get("comet_sections"), dict) else None)
        )
        if isinstance(att_story, dict):
            txt = att_story.get("message", {}).get("text")
            if txt and str(txt).strip():
                text_parts.append(str(txt).strip())
    except Exception:
        pass

    if not text_parts:
        try:
            def _deep_search(obj, depth=0):
                if depth > 6 or not obj:
                    return None
                if isinstance(obj, dict):
                    if "text" in obj and isinstance(obj["text"], str):
                        cand = obj["text"].strip()
                        if len(cand) > 5 and cand not in ["Thích", "Bình luận", "Chia sẻ", "Gửi"]:
                            return cand
                    for k in ["message", "story", "content", "comet_sections"]:
                        if k in obj:
                            res = _deep_search(obj[k], depth + 1)
                            if res:
                                return res
                return None

            found = _deep_search(node)
            if found:
                text_parts.append(found)
        except Exception:
            pass

    unique_parts = []
    for p in text_parts:
        p_clean = p.strip()
        if p_clean and p_clean not in unique_parts:
            unique_parts.append(p_clean)

    return "\n".join(unique_parts)


def extract_author_and_group(node: dict) -> Tuple[Dict[str, str], Dict[str, str], Optional[str], Optional[str]]:
    """Trích xuất Author, Group, Post ID, Post URL từ node Facebook Comet."""
    story = node.get("comet_sections", {}).get("content", {}).get("story", {}) if isinstance(node, dict) else {}
    feedback = node.get("feedback", {}) if isinstance(node, dict) else {}
    if not isinstance(feedback, dict):
        feedback = {}

    post_id = (
        node.get("post_id")
        or (story.get("post_id") if isinstance(story, dict) else None)
        or node.get("legacy_story_hideable_id")
        or (story.get("legacy_story_hideable_id") if isinstance(story, dict) else None)
        or node.get("id")
    )

    post_url = ""
    if isinstance(story, dict):
        post_url = story.get("wwwURL") or story.get("url") or story.get("permalink_url") or ""
    if not post_url and isinstance(node, dict):
        post_url = node.get("url") or node.get("permalink_url") or ""

    if post_url:
        m = (
            re.search(r'/posts/(\d+)', post_url)
            or re.search(r'/multi_permalinks/(\d+)', post_url)
            or re.search(r'story_fbid=(\d+)', post_url)
        )
        if m:
            extracted_num_id = m.group(1)
            if not str(post_id).isdigit():
                post_id = extracted_num_id

    auth_id, auth_name, auth_url = extract_author_info(node)
    author = {"id": auth_id, "name": auth_name, "url": auth_url}

    group = {"id": "", "name": "Hội nhóm Facebook"}
    target_group = node.get("to") or node.get("target_group")
    if not target_group and isinstance(story, dict):
        target_group = story.get("to") or story.get("target_group")
    if isinstance(target_group, dict):
        group["id"] = str(target_group.get("id", ""))
        group["name"] = target_group.get("name") or group["name"]
    elif feedback.get("associated_group"):
        ag = feedback["associated_group"]
        if isinstance(ag, dict):
            group["id"] = str(ag.get("id", ""))
            group["name"] = ag.get("name") or group["name"]

    if not post_url and post_id:
        if group["id"]:
            post_url = f"https://www.facebook.com/groups/{group['id']}/posts/{post_id}/"
        else:
            post_url = f"https://www.facebook.com/{post_id}"

    return author, group, str(post_id) if post_id else None, post_url


class GroupLeadScanner:
    """
    Module Radar quét và phân tích bài viết tìm phòng/căn hộ trực tiếp từ từng nhóm mục tiêu
    sử dụng cơ chế 2 bước: Bootstrap HTML nhóm & Phân trang GroupsCometFeedRegularStoriesPaginationQuery.
    Hỗ trợ quét đa luồng (Multi-threaded Concurrent Scanning) & Callback Real-time.
    """

    def __init__(self, cookie_str: str, proxy: Optional[str] = None):
        self.cookie_str = cookie_str
        self.proxy = proxy
        self.session = self._create_isolated_session()
                
        c_user_match = re.search(r'c_user=(\d+)', cookie_str)
        self.user_id = c_user_match.group(1) if c_user_match else "0"
        self.fb_dtsg: Optional[str] = None
        self.jazoest: Optional[str] = None
        self.last_metrics: Dict[str, int] = {
            "total_fetched": 0,
            "filtered_out_by_time": 0,
            "filtered_out_by_blacklist": 0,
            "unmatched_keywords": 0,
            "matched_leads": 0
        }

    def _create_isolated_session(self) -> requests.Session:
        """Tạo session requests độc lập an toàn cho từng worker thread."""
        s = requests.Session(impersonate="chrome124")
        if self.proxy and self.proxy.strip():
            p = self.proxy.strip()
            if not (p.startswith("http://") or p.startswith("https://") or p.startswith("socks5://")):
                p = f"http://{p}"
            s.proxies = {"http": p, "https": p}
            
        for item in self.cookie_str.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=".facebook.com")
        return s

    def init_dtsg(self) -> bool:
        """Lấy fb_dtsg và jazoest từ trang Facebook."""
        try:
            headers = {
                "User-Agent": DESKTOP_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
            resp = self.session.get("https://www.facebook.com/", headers=headers, timeout=15)
            self.fb_dtsg = extract_dtsg_from_html(resp.text)
            if self.fb_dtsg:
                self.jazoest = compute_jazoest(self.fb_dtsg)
                return True
            return False
        except Exception as e:
            print(f"[RADAR] ❌ Lỗi init_dtsg: {e}")
            return False

    def scan_single_group(
        self,
        group_id: str,
        max_hours: int = 24,
        whitelist: Optional[List[str]] = None,
        blacklist: Optional[List[str]] = None,
        target_profile: str = "ALL",
        whitelist_vn: Optional[List[str]] = None,
        blacklist_vn: Optional[List[str]] = None,
        whitelist_en: Optional[List[str]] = None,
        blacklist_en: Optional[List[str]] = None,
        session: Optional[requests.Session] = None,
        on_lead_found: Optional[Callable[[Dict[str, Any]], None]] = None,
        controller: Optional[RadarController] = None
    ) -> List[Dict[str, Any]]:
        """
        Quét các bài viết mới nhất trong 1 nhóm cụ thể theo thứ tự CHRONOLOGICAL.
        Bước 1: Bootstrap HTML lấy bài preloaded, group title, fb_dtsg và initial_cursor (có retry chống timeout).
        Bước 2: Phân trang GroupsCometFeedRegularStoriesPaginationQuery (có retry chống timeout).
        Hỗ trợ truyền session riêng biệt, callback real-time và RadarController (pause/stop).
        """
        if controller and controller.is_stopped():
            return []
        if controller:
            controller.wait_if_paused()
            if controller.is_stopped():
                return []

        req_session = session or self.session

        active_wl_vn = whitelist_vn if whitelist_vn is not None else whitelist
        active_bl_vn = blacklist_vn if blacklist_vn is not None else blacklist
        active_wl_en = whitelist_en
        active_bl_en = blacklist_en

        now = get_vn_now()
        cutoff_time = now - timedelta(hours=max_hours)
        cutoff_timestamp = int(cutoff_time.timestamp())

        group_url = f"https://www.facebook.com/groups/{group_id}/?sorting_setting=CHRONOLOGICAL"
        headers = {
            "Origin": "https://www.facebook.com",
            "Referer": group_url,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": DESKTOP_USER_AGENT,
            "X-Fb-Friendly-Name": "GroupsCometFeedRegularStoriesPaginationQuery"
        }

        matched_leads: List[Dict[str, Any]] = []
        seen_post_ids = set()
        cursor = None
        group_name = f"Nhóm {group_id}"
        stop_group_scan = False
        consecutive_old_posts = 0
        max_retries = 2

        # ==================== BƯỚC 1: BOOTSTRAP NHÓM TỪ HTML ====================
        g_resp = None
        for attempt in range(max_retries + 1):
            if controller and controller.is_stopped():
                return []
            if controller:
                controller.wait_if_paused()
                if controller.is_stopped():
                    return []
            try:
                g_resp = req_session.get(group_url, headers={"User-Agent": DESKTOP_USER_AGENT}, timeout=25)
                break
            except Exception as req_err:
                err_str = str(req_err).lower()
                if attempt < max_retries and ("timed out" in err_str or "curl: (28)" in err_str or "timeout" in err_str):
                    print(f"[RADAR] ⚠️ Nhóm {group_id} bị timeout mạng khi lấy HTML (lần {attempt+1}/{max_retries}). Thử lại sau 2.5s...")
                    time.sleep(2.5)
                    continue
                print(f"[RADAR] ⚠️ Lỗi đọc HTML nhóm {group_id}: {req_err}")
                break

        if g_resp and g_resp.status_code == 200:
            html = g_resp.text
            
            # Cập nhật fb_dtsg nếu có
            extracted_dtsg = extract_dtsg_from_html(html)
            if extracted_dtsg:
                self.fb_dtsg = extracted_dtsg
                self.jazoest = compute_jazoest(self.fb_dtsg)

            # Lấy tên nhóm từ HTML
            title_match = re.search(r'<title>([^<]+)</title>', html)
            if title_match:
                t_val = title_match.group(1).replace(" | Facebook", "").replace(" - Facebook", "").strip()
                if t_val:
                    group_name = t_val

            # Tìm initial_cursor từ HTML (lọc cursor hợp lệ có độ dài > 20)
            all_cursors = [c for c in re.findall(r'"end_cursor":"([^"]+)"', html) if len(c) > 20]
            if all_cursors:
                cursor = all_cursors[0]

            # Bóc tách các bài viết preloaded từ các block JSON
            scripts = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)
            for s in scripts:
                if "Story" in s and ("post_id" in s or "creation_time" in s or "legacy_story_hideable_id" in s):
                    try:
                        d = json.loads(s)
                        def find_stories(obj):
                            nonlocal stop_group_scan, group_name, consecutive_old_posts
                            if stop_group_scan or not isinstance(obj, (dict, list)):
                                return
                            if isinstance(obj, dict):
                                if obj.get("__typename") == "Story" and (obj.get("post_id") or obj.get("id") or obj.get("legacy_story_hideable_id")):
                                    is_pinned = check_is_pinned(obj)
                                    c_time = extract_creation_time(obj)
                                    if is_pinned:
                                        # Bỏ qua bài ghim cũ, không ngắt luồng
                                        pass
                                    elif c_time and c_time < cutoff_timestamp:
                                        consecutive_old_posts += 1
                                        if consecutive_old_posts >= 5:
                                            stop_group_scan = True
                                            self.last_metrics["filtered_out_by_time"] += 1
                                            return
                                        return
                                    else:
                                        consecutive_old_posts = 0

                                    text = extract_post_text(obj)
                                    if text:
                                        self.last_metrics["total_fetched"] += 1
                                        is_lead, client_type, hit_kw = classify_lead_intent(
                                            text=text,
                                            target_profile=target_profile,
                                            custom_whitelist_vn=active_wl_vn,
                                            custom_blacklist_vn=active_bl_vn,
                                            custom_whitelist_en=active_wl_en,
                                            custom_blacklist_en=active_bl_en
                                        )
                                        if not is_lead:
                                            self.last_metrics["unmatched_keywords"] += 1
                                        else:
                                            author, grp, p_id, p_url = extract_author_and_group(obj)
                                            post_id = p_id or str(obj.get("post_id") or obj.get("id") or "")
                                            if post_id not in seen_post_ids and post_id != str(group_id):
                                                seen_post_ids.add(post_id)
                                                g_title = grp.get("name") or group_name
                                                group_name = g_title
                                                phones = extract_phone_numbers(text)
                                                phone_str = ", ".join(phones) if phones else None
                                                has_phone = 1 if phones else 0
                                                lead_item = {
                                                    "post_id": post_id,
                                                    "post_url": p_url or f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/",
                                                    "author_id": author["id"],
                                                    "author_name": author["name"],
                                                    "author_url": author["url"],
                                                    "group_id": str(group_id),
                                                    "group_name": g_title,
                                                    "content": text.strip(),
                                                    "created_time": datetime.fromtimestamp(c_time, tz=VN_TZ).strftime("%Y-%m-%d %H:%M:%S") if c_time else None,
                                                    "matched_keywords": ", ".join(hit_kw) if hit_kw else "N/A",
                                                    "client_type": client_type or "VIETNAMESE",
                                                    "phone": phone_str,
                                                    "has_phone": has_phone,
                                                    "status": "NEW"
                                                }
                                                matched_leads.append(lead_item)
                                                print(f"[RADAR] 🎯 [KHỚP LEAD - {client_type}] Nhóm '{g_title}' | Khách: '{author['name']}' | Từ khóa: {hit_kw}")
                                                if on_lead_found:
                                                    try:
                                                        on_lead_found(lead_item)
                                                    except Exception as cb_err:
                                                        print(f"[RADAR] ⚠️ Lỗi callback lead: {cb_err}")
                                for v in obj.values():
                                    find_stories(v)
                            elif isinstance(obj, list):
                                for itm in obj:
                                    find_stories(itm)
                        find_stories(d)
                    except Exception:
                        pass
        elif g_resp:
            print(f"[RADAR] ⚠️ Lỗi khi GET HTML nhóm {group_id}: HTTP {g_resp.status_code}")

        # Đảm bảo có fb_dtsg trước khi phân trang GraphQL
        if not self.fb_dtsg:
            self.init_dtsg()

        # ==================== BƯỚC 2: PHÂN TRANG GRAPHQL ====================
        page_index = 0
        while not stop_group_scan and cursor and page_index < 100:
            if controller and controller.is_stopped():
                break
            if controller:
                controller.wait_if_paused()
                if controller.is_stopped():
                    break

            page_index += 1
            variables = {
                "count": 10,
                "cursor": cursor,
                "feedLocation": "GROUP",
                "feedType": "DISCUSSION",
                "feedbackSource": 0,
                "filterTopicId": None,
                "focusCommentID": None,
                "privacySelectorRenderLocation": "COMET_STREAM",
                "referringStoryRenderLocation": None,
                "renderLocation": "group",
                "scale": 2,
                "sortingSetting": "CHRONOLOGICAL",
                "stream_initial_count": 1,
                "useDefaultActor": False,
                "youthIntegrityHostID": str(group_id),
                "id": str(group_id),
                **DEFAULT_RELAY_PVS
            }

            payload = {
                "av": self.user_id,
                "__user": self.user_id,
                "__a": "1",
                "fb_dtsg": self.fb_dtsg,
                "jazoest": self.jazoest,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "GroupsCometFeedRegularStoriesPaginationQuery",
                "doc_id": DOC_ID_GROUP_FEED,
                "variables": json.dumps(variables, default=str)
            }

            resp = None
            for attempt in range(max_retries + 1):
                if controller and controller.is_stopped():
                    break
                if controller:
                    controller.wait_if_paused()
                    if controller.is_stopped():
                        break
                try:
                    resp = req_session.post(GRAPHQL_URL, data=payload, headers=headers, timeout=25)
                    break
                except Exception as req_err:
                    err_str = str(req_err).lower()
                    if attempt < max_retries and ("timed out" in err_str or "curl: (28)" in err_str or "timeout" in err_str):
                        print(f"[RADAR] ⚠️ Nhóm {group_id} bị timeout mạng (lần {attempt+1}/{max_retries}). Thử lại sau 2.5s...")
                        time.sleep(2.5)
                        continue
                    print(f"[RADAR] ❌ Lỗi quét nhóm {group_id}: {req_err}")
                    break

            if not resp or resp.status_code != 200:
                if resp:
                    print(f"[RADAR] ⚠️ Lỗi từ Meta: HTTP {resp.status_code}")
                break

            edges, page_info = parse_relay_ndjson_response(resp.text)
            
            if page_info.get("errors"):
                print(f"[RADAR] ⚠️ Lỗi từ Meta: {page_info['errors'][0]}")

            if not edges:
                break

            for edge in edges:
                node = edge.get("node", {}) if isinstance(edge, dict) else {}
                if not node or not isinstance(node, dict):
                    continue

                # Bỏ qua node rác nếu là group container node
                node_id = str(node.get("id") or "")
                if node_id == str(group_id):
                    continue

                is_pinned = check_is_pinned(node)
                creation_time = extract_creation_time(node)
                if is_pinned:
                    # Bỏ qua bài ghim cũ, không ngắt luồng
                    pass
                elif creation_time and creation_time < cutoff_timestamp:
                    consecutive_old_posts += 1
                    if consecutive_old_posts >= 5:
                        stop_group_scan = True
                        self.last_metrics["filtered_out_by_time"] += 1
                        break
                    continue
                else:
                    consecutive_old_posts = 0

                content = extract_post_text(node)
                if not content or not content.strip():
                    continue

                self.last_metrics["total_fetched"] += 1
                is_lead, client_type, hit_keywords = classify_lead_intent(
                    text=content,
                    target_profile=target_profile,
                    custom_whitelist_vn=active_wl_vn,
                    custom_blacklist_vn=active_bl_vn,
                    custom_whitelist_en=active_wl_en,
                    custom_blacklist_en=active_bl_en
                )
                if not is_lead:
                    self.last_metrics["unmatched_keywords"] += 1
                    continue

                author, grp, p_id, p_url = extract_author_and_group(node)
                post_id = p_id or str(node.get("post_id") or node.get("id") or "")
                if not post_id or post_id in seen_post_ids or post_id == str(group_id):
                    continue
                seen_post_ids.add(post_id)

                g_name = grp.get("name") or group_name
                post_url = p_url or f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"

                phones = extract_phone_numbers(content)
                phone_str = ", ".join(phones) if phones else None
                has_phone = 1 if phones else 0

                lead_item = {
                    "post_id": post_id,
                    "post_url": post_url,
                    "author_id": author["id"],
                    "author_name": author["name"],
                    "author_url": author["url"],
                    "group_id": str(group_id),
                    "group_name": g_name,
                    "content": content.strip(),
                    "created_time": datetime.fromtimestamp(creation_time, tz=VN_TZ).strftime("%Y-%m-%d %H:%M:%S") if creation_time else None,
                    "matched_keywords": ", ".join(hit_keywords) if hit_keywords else "N/A",
                    "client_type": client_type or "VIETNAMESE",
                    "phone": phone_str,
                    "has_phone": has_phone,
                    "status": "NEW"
                }
                matched_leads.append(lead_item)
                print(f"[RADAR] 🎯 [KHỚP LEAD - {client_type}] Nhóm '{g_name}' | Khách: '{author['name']}' | Từ khóa: {hit_keywords}")
                if on_lead_found:
                    try:
                        on_lead_found(lead_item)
                    except Exception as cb_err:
                        print(f"[RADAR] ⚠️ Lỗi callback lead: {cb_err}")

            if stop_group_scan:
                break

            if not page_info.get("has_next_page"):
                break
            cursor = page_info.get("end_cursor")
            if not cursor:
                break

        self.last_metrics["matched_leads"] += len(matched_leads)
        return matched_leads

    def scan_group_list(
        self,
        group_ids: List[str],
        max_hours: int = 24,
        whitelist: Optional[List[str]] = None,
        blacklist: Optional[List[str]] = None,
        target_profile: str = "ALL",
        whitelist_vn: Optional[List[str]] = None,
        blacklist_vn: Optional[List[str]] = None,
        whitelist_en: Optional[List[str]] = None,
        blacklist_en: Optional[List[str]] = None,
        max_workers: int = 4,
        on_lead_found: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_group_completed: Optional[Callable[[str, int, int, List[Dict[str, Any]]], None]] = None,
        controller: Optional[RadarController] = None
    ) -> List[Dict[str, Any]]:
        """
        Quét song song danh sách group_ids bằng ThreadPoolExecutor có hỗ trợ:
        - Giãn cách khởi động luồng (Stagger delay) chống nghẽn mạng/DNS.
        - Điều khiển Tạm dừng (Pause), Tiếp tục (Resume), Dừng hẳn (Stop).
        - Callbacks cập nhật thời gian thực.
        """
        if not self.fb_dtsg:
            self.init_dtsg()

        valid_gids = [str(gid).strip() for gid in group_ids if str(gid).strip()]
        if not valid_gids:
            print("[RADAR] ⚠️ Danh sách group_ids rỗng, không thể quét.")
            return []

        all_leads: List[Dict[str, Any]] = []
        total_groups = len(valid_gids)
        completed_count = 0
        lock = threading.Lock()

        # Giới hạn max_workers an toàn
        workers = max(1, min(max_workers, 10))

        print(f"[RADAR] 🚀 Bắt đầu quét ĐA LUỒNG ({workers} workers) cho {total_groups} nhóm trong {max_hours}h qua [Profile: {target_profile}]...")

        def _worker_task(gid: str, stagger_delay: float):
            if controller and controller.is_stopped():
                return gid, []

            # Tránh gửi đồng thời 4 request cùng lúc gây nghẽn DNS / Socket
            if stagger_delay > 0:
                time.sleep(stagger_delay)

            if controller:
                controller.wait_if_paused()
                if controller.is_stopped():
                    return gid, []

            thread_session = self._create_isolated_session()
            try:
                leads = self.scan_single_group(
                    group_id=str(gid),
                    max_hours=max_hours,
                    whitelist=whitelist,
                    blacklist=blacklist,
                    target_profile=target_profile,
                    whitelist_vn=whitelist_vn,
                    blacklist_vn=blacklist_vn,
                    whitelist_en=whitelist_en,
                    blacklist_en=blacklist_en,
                    session=thread_session,
                    on_lead_found=on_lead_found,
                    controller=controller
                )
                return gid, leads
            except Exception as e:
                print(f"[RADAR] ❌ Bỏ qua nhóm {gid} do lỗi: {e}")
                return gid, []
            finally:
                try:
                    thread_session.close()
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_gid = {
                executor.submit(_worker_task, gid, (idx % workers) * 0.4): gid
                for idx, gid in enumerate(valid_gids)
            }

            for future in as_completed(future_to_gid):
                if controller and controller.is_stopped():
                    print("[RADAR] ⏹️ Nhận lệnh dừng quét từ người dùng.")
                    break
                try:
                    gid, leads = future.result()
                    with lock:
                        completed_count += 1
                        all_leads.extend(leads)
                        print(f"[RADAR] ⚡ [{completed_count}/{total_groups}] Nhóm {gid} hoàn tất (Thu được {len(leads)} lead).")
                        if on_group_completed:
                            try:
                                on_group_completed(gid, completed_count, total_groups, leads)
                            except Exception as g_cb_err:
                                print(f"[RADAR] ⚠️ Lỗi callback group: {g_cb_err}")
                except Exception as fut_err:
                    print(f"[RADAR] ⚠️ Lỗi xử lý future: {fut_err}")

        print(f"[RADAR] 🏁 Hoàn tất ({completed_count}/{total_groups} nhóm)! Tổng thu được {len(all_leads)} lead tiềm năng.")
        return all_leads

    def scan_feed(
        self,
        max_hours: int = 24,
        whitelist: Optional[List[str]] = None,
        blacklist: Optional[List[str]] = None,
        max_posts: Optional[int] = None,
        group_ids: Optional[List[str]] = None,
        target_profile: str = "ALL",
        whitelist_vn: Optional[List[str]] = None,
        blacklist_vn: Optional[List[str]] = None,
        whitelist_en: Optional[List[str]] = None,
        blacklist_en: Optional[List[str]] = None,
        max_workers: int = 4,
        on_lead_found: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_group_completed: Optional[Callable[[str, int, int, List[Dict[str, Any]]], None]] = None,
        controller: Optional[RadarController] = None
    ) -> List[Dict[str, Any]]:
        """Compatibility wrapper for scan_feed."""
        if group_ids:
            return self.scan_group_list(
                group_ids,
                max_hours=max_hours,
                whitelist=whitelist,
                blacklist=blacklist,
                target_profile=target_profile,
                whitelist_vn=whitelist_vn,
                blacklist_vn=blacklist_vn,
                whitelist_en=whitelist_en,
                blacklist_en=blacklist_en,
                max_workers=max_workers,
                on_lead_found=on_lead_found,
                on_group_completed=on_group_completed,
                controller=controller
            )
        return []


# Alias for backward compatibility
LeadScanner = GroupLeadScanner
