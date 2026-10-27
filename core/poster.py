import os
import re
import json
import time
import uuid
import base64
import random
import traceback
from typing import List, Optional, Dict, Any, Tuple
from curl_cffi import requests
from curl_cffi.curl import CurlMime
from core.network import execute_with_smart_retry_sync
from core.error_parser import parse_facebook_error

FB_COMET_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
DESKTOP_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
DOC_ID_COMPOSER_CREATE = "27731606676514713"
DEFAULT_STORY_CREATE_DOC_ID = "27731606676514713"
FALLBACK_STORY_CREATE_DOC_ID = "28329575890036120"
LEGACY_STORY_CREATE_DOC_ID = "3559434960802556"


def format_proxy(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    """Format proxy string into curl_cffi proxies dict.

    Args:
        proxy: Proxy connection string (e.g., 'http://user:pass@host:port').

    Returns:
        Dictionary with http and https keys, or None if no proxy.
    """
    if not proxy or not proxy.strip():
        return None
    proxy = proxy.strip()
    if not (proxy.startswith("http://") or proxy.startswith("https://") or proxy.startswith("socks5://")):
        proxy = f"http://{proxy}"
    return {"http": proxy, "https": proxy}


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """Parse raw cookie string into a dictionary.

    Args:
        cookie_str: Raw HTTP cookie string.

    Returns:
        Dictionary of cookie key-value pairs.
    """
    cookie_dict = {}
    if not cookie_str:
        return cookie_dict
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        parts = item.split("=", 1)
        cookie_dict[parts[0].strip()] = parts[1].strip()
    return cookie_dict


def compute_jazoest(fb_dtsg: str) -> str:
    """Compute jazoest value from fb_dtsg token.

    Args:
        fb_dtsg: Facebook DTSG token string.

    Returns:
        Computed numeric jazoest string.
    """
    if not fb_dtsg:
        return "2957"
    return "2" + str(sum(ord(c) for c in fb_dtsg))


def extract_fb_dtsg_from_html(html: str) -> str:
    """Extracts fb_dtsg token from Facebook Comet HTML page.

    Args:
        html: Raw HTML string of Facebook response.

    Returns:
        Extracted fb_dtsg token or empty string.
    """
    if not html:
        return ""
    patterns = [
        r'"dtsg":\{"token":"([^"]+)"',
        r'\["DTSGInitialData",\[\],\{"token":"([^"]+)"\}',
        r'"DTSGInitialData",\[\],\{"token":"([^"]+)"\}',
        r'\["DTSGInitData",\[\],\{"token":"([^"]+)"',
        r'"DTSGInitData":\{"token":"([^"]+)"',
        r'name="fb_dtsg"\s+value="([^"]+)"',
        r'"async_get_token":"([^"]+)"',
        r'"token":"([A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+)"'
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return ""


def extract_lsd_from_html(html: str) -> str:
    """Extracts LSD token from Facebook Comet HTML page.

    Args:
        html: Raw HTML string of Facebook response.

    Returns:
        Extracted LSD token or empty string.
    """
    if not html:
        return ""
    patterns = [
        r'\["LSD",\[\],\{"token":"([^"]+)"\}',
        r'"LSD",\[\],\{"token":"([^"]+)"\}',
        r'name="lsd"\s+value="([^"]+)"',
        r'"lsd":\{"token":"([^"]+)"\}'
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return ""


def check_cookie_live(cookie_str: str, proxy: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
    """Checks if a Facebook Cookie is currently LIVE or DIE/CHECKPOINT.

    Args:
        cookie_str: Raw Facebook cookie string.
        proxy: Optional proxy string.

    Returns:
        Tuple of (is_live: bool, status_message: str, data: dict).
    """
    clean_cookie = cookie_str.strip()
    if not clean_cookie:
        return False, "Cookie rỗng", {}

    cookie_dict = parse_cookie_string(clean_cookie)
    user_id = cookie_dict.get("c_user", "")
    if not user_id:
        m = re.search(r'c_user=(\d+)', clean_cookie)
        if m:
            user_id = m.group(1)

    if not user_id:
        return False, "Không tìm thấy UID (c_user) trong Cookie", {}

    proxies = format_proxy(proxy)
    headers = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Cookie": clean_cookie
    }

    try:
        with requests.Session(impersonate="chrome124", proxies=proxies) as session:
            for k, v in cookie_dict.items():
                session.cookies.set(k.strip(), v.strip(), domain=".facebook.com")

            resp = execute_with_smart_retry_sync(
                lambda: session.get("https://www.facebook.com/", headers=headers, timeout=15),
                max_retries=2
            )
            
            # Check Set-Cookie headers for session invalidation
            set_cookie_header = str(resp.headers.get("Set-Cookie", "") or resp.headers.get("set-cookie", ""))
            if "c_user=deleted" in set_cookie_header or "xs=deleted" in set_cookie_header:
                return False, f"DIE / EXPIRED: Facebook đã huỷ phiên đăng nhập (UID: {user_id})", {"user_id": user_id, "status": "EXPIRED"}

            if "checkpoint" in resp.url or "login" in resp.url:
                return False, f"CHECKPOINT / EXPIRED: URL redirected to {resp.url}", {"url": resp.url}

            fb_dtsg = extract_fb_dtsg_from_html(resp.text)
            if fb_dtsg:
                return True, f"LIVE (UID: {user_id})", {"user_id": user_id, "has_dtsg": True, "fb_dtsg": fb_dtsg}
            elif f'"USER_ID":"{user_id}"' in resp.text or f'"c_user":"{user_id}"' in resp.text:
                return True, f"LIVE (UID: {user_id})", {"user_id": user_id, "has_dtsg": False}
            else:
                return False, f"DIE: Phiên cookie không còn hiệu lực (UID: {user_id})", {"url": resp.url}

    except Exception as e:
        return False, f"Lỗi kết nối kiểm tra Cookie: {str(e)}", {"exception": str(e)}


def extract_post_id_from_story(story_obj: Dict[str, Any]) -> Optional[str]:
    """Extracts numeric Facebook Post ID from story object.

    Args:
        story_obj: Story dictionary returned by GraphQL mutation.

    Returns:
        Post ID string if found, else None.
    """
    if not isinstance(story_obj, dict):
        return None

    # 1. Check legacy_story_hideable_id
    legacy_id = story_obj.get("legacy_story_hideable_id")
    if legacy_id and str(legacy_id).isdigit():
        return str(legacy_id)

    # 2. Check id (base64 encoded format: S:_I<uid>:VK:<post_id>)
    story_id = story_obj.get("id")
    if story_id and isinstance(story_id, str):
        try:
            decoded = base64.b64decode(story_id).decode("utf-8", "ignore")
            m = re.search(r'VK:(\d+)', decoded)
            if m:
                return m.group(1)
            m2 = re.search(r':(\d+)$', decoded)
            if m2:
                return m2.group(1)
        except Exception:
            pass

    return None


def upload_photo_via_session(
    session: requests.Session,
    photo_path: str,
    fb_dtsg: str,
    jazoest: str,
    user_id: str,
    headers: Dict[str, str]
) -> Optional[str]:
    """Uploads a local image file using an existing requests.Session to Comet upload endpoints.

    Uses CurlMime multipart form handling compatible with curl_cffi.

    Args:
        session: Active curl_cffi session.
        photo_path: Absolute path to local image file.
        fb_dtsg: DTSG token string.
        jazoest: Jazoest token string.
        user_id: Numeric Facebook user ID (c_user).
        headers: Base request headers.

    Returns:
        Numeric Facebook Photo ID string if successful, otherwise None.
    """
    if not photo_path or not os.path.exists(photo_path):
        print(f"  [POSTER-UPLOAD] [ERROR] File ảnh không tồn tại: {photo_path}")
        return None

    filename = os.path.basename(photo_path)
    content_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"

    # Endpoints to attempt upload (Primary and Fallback)
    endpoints = [
        ("https://upload.facebook.com/ajax/react_composer/attachments/photo/upload/", "farr", "8"),
        ("https://www.facebook.com/ajax/react_composer/attachments/photo/upload/", "farr", "8"),
        ("https://upload.facebook.com/ajax/composer/attachment/media/upload/", "file", "13"),
        ("https://www.facebook.com/ajax/composer/attachment/media/upload/", "file", "13"),
    ]

    upload_headers = {
        "User-Agent": headers.get("User-Agent", DESKTOP_USER_AGENT),
        "Accept": "*/*",
        "Origin": "https://www.facebook.com",
        "Referer": "https://www.facebook.com/",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": headers.get("Cookie", ""),
        "X-Fb-Friendly-Name": "ComposerMediaAttachmentUpload"
    }

    for url, file_field_name, source_val in endpoints:
        try:
            mime = CurlMime()
            mime.addpart(
                name=file_field_name,
                filename=filename,
                content_type=content_type,
                local_path=photo_path
            )

            data = {
                "source": str(source_val),
                "profile_id": str(user_id),
                "waterfall_id": str(uuid.uuid4()),
                "waterfallxapp": "comet",
                "fb_dtsg": str(fb_dtsg),
                "jazoest": str(jazoest),
                "__user": str(user_id),
                "__a": "1"
            }

            resp = execute_with_smart_retry_sync(
                lambda: session.post(url, data=data, multipart=mime, headers=upload_headers, timeout=35),
                max_retries=4
            )
            
            if resp.status_code != 200:
                print(f"  [POSTER-UPLOAD] [WARN] Endpoint {url} trả về HTTP {resp.status_code}: {resp.text[:200]}")
                continue

            raw = resp.text
            if raw.startswith("for (;;);"):
                raw = raw[len("for (;;);"):]

            data_json = {}
            try:
                data_json = json.loads(raw)
            except Exception:
                for line in resp.text.split("\n"):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            data_json = json.loads(line)
                            if "payload" in data_json or "data" in data_json:
                                break
                        except Exception:
                            pass

            payload = data_json.get("payload", {}) if isinstance(data_json, dict) else {}
            photo_id = (
                payload.get("photoID") or 
                payload.get("fbid") or 
                payload.get("id") or
                (payload.get("photo", {}) or {}).get("fbid")
            )

            # Defensive regex extraction if JSON field parsing fails
            if not photo_id:
                m = re.search(r'"photoID":"?(\d+)"?', resp.text)
                if m:
                    photo_id = m.group(1)
                else:
                    m_fbid = re.search(r'"fbid":"?(\d+)"?', resp.text)
                    if m_fbid:
                        photo_id = m_fbid.group(1)

            if photo_id and str(photo_id).isdigit():
                return str(photo_id)

            print(f"  [POSTER-UPLOAD] [WARN] Upload 200 OK nhưng không trích xuất được photoID: {resp.text[:200]}")

        except Exception as e:
            print(f"  [POSTER-UPLOAD] [ERROR] Lỗi khi upload ảnh lên {url}: {type(e).__name__}: {str(e)}")
            continue

    return None


def upload_group_photo_via_cookie(
    cookie_str: str,
    photo_path: str,
    proxy: Optional[str] = None
) -> Optional[str]:
    """Uploads a local image file to Facebook Comet composer endpoint using Cookie string.

    Args:
        cookie_str: Raw Facebook cookie string.
        photo_path: Path to local image file.
        proxy: Optional proxy URL.

    Returns:
        Numeric Photo ID string if successful, else None.
    """
    if not photo_path or not os.path.exists(photo_path):
        return None

    clean_cookie = cookie_str.strip()
    cookie_dict = parse_cookie_string(clean_cookie)
    user_id = cookie_dict.get("c_user", "")
    if not user_id:
        m = re.search(r'c_user=(\d+)', clean_cookie)
        if m:
            user_id = m.group(1)

    if not user_id:
        return None

    proxies = format_proxy(proxy)
    headers = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Origin": "https://www.facebook.com",
        "Referer": "https://www.facebook.com/",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": clean_cookie
    }

    try:
        with requests.Session(impersonate="chrome124", proxies=proxies) as session:
            for k, v in cookie_dict.items():
                session.cookies.set(k.strip(), v.strip(), domain=".facebook.com")

            # Obtain fb_dtsg and jazoest
            home_resp = execute_with_smart_retry_sync(
                lambda: session.get("https://www.facebook.com/", headers=headers, timeout=15),
                max_retries=3
            )
            fb_dtsg = extract_fb_dtsg_from_html(home_resp.text)
            if not fb_dtsg:
                feed_resp = execute_with_smart_retry_sync(
                    lambda: session.get("https://www.facebook.com/groups/feed/", headers=headers, timeout=15),
                    max_retries=3
                )
                fb_dtsg = extract_fb_dtsg_from_html(feed_resp.text)
            jazoest = compute_jazoest(fb_dtsg)

            return upload_photo_via_session(session, photo_path, fb_dtsg, jazoest, user_id, headers)

    except Exception as e:
        print(f"[ERROR] upload_group_photo_via_cookie failed: {e}")
        return None


# Alias for backward compatibility
upload_photo_via_cookie = upload_photo_via_session


def spin_text(text: str) -> str:
    """Xử lý Spin Syntax dạng {A|B|C} ngẫu nhiên để nội dung bài đăng luôn độc nhất."""
    if not text:
        return ""
    pattern = re.compile(r'\{([^{}]+)\}')
    while True:
        match = pattern.search(text)
        if not match:
            break
        choices = match.group(1).split('|')
        text = text[:match.start()] + random.choice(choices) + text[match.end():]
    return text


def publish_post_via_cookie(
    group_id: str,
    message: str,
    cookie_str: str,
    photo_paths: Optional[List[str]] = None,
    proxy: Optional[str] = None,
    retry_count: int = 1,
    doc_id: str = DEFAULT_STORY_CREATE_DOC_ID,
    crosspost_group_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Publishes a post to a Facebook Group via Web Comet GraphQL Mutation `story_create`.

    Safely sanitizes attachments, logs errors defensibly, supports Comet Crossposting, and falls back across doc_ids.

    Args:
        group_id: Target numeric Facebook Group ID (primary).
        message: Content/caption text of the post.
        cookie_str: Raw Facebook session cookie string.
        photo_paths: Optional list of local image file paths.
        proxy: Optional HTTP/HTTPS proxy string.
        retry_count: Number of retries on network/server error.
        doc_id: GraphQL document ID (defaults to DEFAULT_STORY_CREATE_DOC_ID).
        crosspost_group_ids: Optional list of secondary group IDs for crossposting (up to 9 groups).

    Returns:
        Dictionary containing posting result status, post_id, post_url, and debug info.
    """
    clean_group_id = str(group_id).strip()
    clean_cookie = cookie_str.strip()
    proxies = format_proxy(proxy)
    crosspost_list = [str(gid).strip() for gid in (crosspost_group_ids or []) if str(gid).strip() and str(gid).strip() != clean_group_id][:9]

    cookie_dict = parse_cookie_string(clean_cookie)
    user_id = cookie_dict.get("c_user", "")
    if not user_id:
        m = re.search(r'c_user=(\d+)', clean_cookie)
        if m:
            user_id = m.group(1)

    if not user_id:
        return {
            "success": False,
            "approval_status": "FAILED",
            "post_id": None,
            "post_url": None,
            "message": "Không tìm thấy UID (c_user) trong Cookie.",
            "group_id": clean_group_id,
            "is_cookie_dead": True,
            "error_type": "COOKIE_DIE",
            "debug_detail": {"error": "Missing c_user"}
        }

    trace_steps = []
    max_attempts = max(1, retry_count + 1)

    headers = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Origin": "https://www.facebook.com",
        "Referer": f"https://www.facebook.com/groups/{clean_group_id}/",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": clean_cookie
    }

    doc_ids_to_try = [str(doc_id)]
    for fid in [DOC_ID_COMPOSER_CREATE, FALLBACK_STORY_CREATE_DOC_ID, LEGACY_STORY_CREATE_DOC_ID]:
        if fid not in doc_ids_to_try:
            doc_ids_to_try.append(fid)

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            trace_steps.append(f"[Retry {attempt - 1}/{retry_count}] Thử lại sau 2s...")
            time.sleep(2)

        try:
            with requests.Session(impersonate="chrome124", proxies=proxies) as session:
                for k, v in cookie_dict.items():
                    session.cookies.set(k.strip(), v.strip(), domain=".facebook.com")

                # Step 1: Fetch group page to extract fb_dtsg, jazoest, lsd
                trace_steps.append(f"[Step 1/3] Lấy fb_dtsg từ trang nhóm {clean_group_id}...")
                home_resp = execute_with_smart_retry_sync(
                    lambda: session.get(f"https://www.facebook.com/groups/{clean_group_id}/", headers=headers, timeout=20),
                    max_retries=3
                )
                
                if "checkpoint" in home_resp.url or "login" in home_resp.url:
                    return {
                        "success": False,
                        "approval_status": "FAILED",
                        "post_id": None,
                        "post_url": None,
                        "message": f"Cookie bị Checkpoint hoặc Hết hạn: Redirect to {home_resp.url}",
                        "group_id": clean_group_id,
                        "is_cookie_dead": True,
                        "error_type": "COOKIE_DIE",
                        "debug_detail": {"url": home_resp.url, "steps": trace_steps}
                    }

                fb_dtsg = extract_fb_dtsg_from_html(home_resp.text)
                lsd = extract_lsd_from_html(home_resp.text)
                if not fb_dtsg:
                    home_root = execute_with_smart_retry_sync(
                        lambda: session.get("https://www.facebook.com/", headers=headers, timeout=15),
                        max_retries=3
                    )
                    fb_dtsg = extract_fb_dtsg_from_html(home_root.text)
                    if not lsd:
                        lsd = extract_lsd_from_html(home_root.text)

                jazoest = compute_jazoest(fb_dtsg)

                # Step 2: Handle Photo Attachments (Defensive Validation)
                photo_attachments: List[Dict[str, Any]] = []
                if photo_paths and isinstance(photo_paths, list):
                    valid_paths = [p for p in photo_paths if p and os.path.exists(p)]
                    if valid_paths:
                        trace_steps.append(f"[Step 2/3] Bắt đầu upload {len(valid_paths)} ảnh đính kèm...")
                        for p_path in valid_paths:
                            uploaded_id = upload_photo_via_session(session, p_path, fb_dtsg, jazoest, user_id, headers)
                            # Strict validation: Only append if valid numeric ID
                            if uploaded_id and str(uploaded_id).isdigit():
                                photo_attachments.append({"photo": {"id": str(uploaded_id)}})
                                trace_steps.append(f"[+] Upload ảnh '{os.path.basename(p_path)}' thành công ID: {uploaded_id}")
                            else:
                                trace_steps.append(f"[-] Không thể upload ảnh '{os.path.basename(p_path)}'")
                        
                        print(f"  [POSTER] Đã upload thành công {len(photo_attachments)}/{len(valid_paths)} ảnh.")

                # Ensure attachments is strictly a list (never contains null or invalid objects)
                sanitized_attachments = [
                    att for att in photo_attachments 
                    if isinstance(att, dict) and "photo" in att and isinstance(att["photo"], dict) and att["photo"].get("id")
                ]

                # Step 3: Build Story Create GraphQL Payload (ComposerStoryCreateMutation)
                composer_session_id = str(uuid.uuid4())
                client_mutation_id = str(uuid.uuid4())[:8]
                message_text = spin_text(str(message or ""))
                post_headers = {
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Fb-Friendly-Name": "ComposerStoryCreateMutation"
                }

                # Chuẩn hóa mảng groups_schedule_xposting (Tối đa 9 nhóm phụ)
                groups_schedule_xposting = []
                clean_sub_groups = []
                if crosspost_group_ids:
                    clean_sub_groups = [str(gid).strip() for gid in crosspost_group_ids if str(gid).strip() and str(gid).strip() != str(clean_group_id)][:9]
                    for sub_gid in clean_sub_groups:
                        groups_schedule_xposting.append({
                            "xpost_audience": {
                                "to_id": str(sub_gid)
                            }
                        })

                input_data = {
                    "composer_entry_point": "inline_composer",
                    "composer_source_surface": "group",
                    "composer_type": "group",
                    "logging": {
                        "composer_session_id": composer_session_id
                    },
                    "source": "WWW",
                    "message": {
                        "ranges": [],
                        "text": message_text
                    },
                    "with_tags_ids": None,
                    "inline_activities": [],
                    "text_format_preset_id": "0",
                    "groups_schedule_xposting": groups_schedule_xposting,
                    "group_flair": {"flair_id": None},
                    "attachments": sanitized_attachments,
                    "tracking": [None],
                    "audience": {
                        "to_id": str(clean_group_id)
                    },
                    "actor_id": str(user_id),
                    "client_mutation_id": client_mutation_id
                }

                variables = {
                    "input": input_data,
                    "feedLocation": "GROUP",
                    "feedbackSource": 0,
                    "focusCommentID": None,
                    "gridMediaWidth": None,
                    "groupID": None,
                    "scale": 2,
                    "privacySelectorRenderLocation": "COMET_STREAM",
                    "renderLocation": "group",
                    "useDefaultActor": False,
                    "isFeed": False,
                    "isFundraiser": False,
                    "isFunFactPost": False,
                    "isGroup": True,
                    "isEvent": False,
                    "isTimeline": False,
                    "isSocialLearning": False,
                    "isPageNewsFeed": False,
                    "isProfileReviews": False,
                    "isWorkSharedDraft": False
                }

                last_error_data = None
                for cur_doc_id in doc_ids_to_try:
                    trace_steps.append(f"[Step 3/3] Gửi Mutation ComposerStoryCreateMutation (doc_id: {cur_doc_id}, session: {composer_session_id[:8]}...)...")

                    payload = {
                        "av": str(user_id),
                        "__user": str(user_id),
                        "__a": "1",
                        "fb_dtsg": fb_dtsg,
                        "jazoest": jazoest,
                        "lsd": lsd or "AVrO1XWz",
                        "fb_api_caller_class": "RelayModern",
                        "fb_api_req_friendly_name": "ComposerStoryCreateMutation",
                        "doc_id": str(cur_doc_id),
                        "variables": json.dumps(variables, ensure_ascii=False, default=str)
                    }

                    post_resp = execute_with_smart_retry_sync(
                        lambda: session.post(FB_COMET_GRAPHQL_URL, data=payload, headers=post_headers, timeout=30),
                        max_retries=4
                    )
                    
                    raw = post_resp.text
                    if raw.startswith("for (;;);"):
                        raw = raw[len("for (;;);"):]

                    data = {}
                    try:
                        data = json.loads(raw)
                    except Exception:
                        for line in post_resp.text.split("\n"):
                            line = line.strip()
                            if line.startswith("{") and line.endswith("}"):
                                try:
                                    data = json.loads(line)
                                    if "data" in data or "errors" in data:
                                        break
                                except Exception:
                                    pass

                    # Analyze Response
                    res_data = data.get("data", {}) if isinstance(data, dict) else {}
                    story_create = res_data.get("story_create", {}) if isinstance(res_data, dict) else {}
                    story = story_create.get("story", {}) if isinstance(story_create, dict) else {}

                    if story and isinstance(story, dict) and (story.get("id") or story.get("legacy_story_hideable_id") or story.get("url")):
                        primary_post_id = story.get("legacy_story_hideable_id") or extract_post_id_from_story(story) or str(clean_group_id)
                        primary_url = story.get("url") or f"https://www.facebook.com/groups/{clean_group_id}/posts/{primary_post_id}/"
                        crosspost_log_str = f" + {len(clean_sub_groups)} nhóm phụ ({', '.join(clean_sub_groups)})" if clean_sub_groups else ""
                        trace_steps.append(f"[+] Đăng bài thành công -> Post ID: {primary_post_id}{crosspost_log_str} | Link: {primary_url}")

                        return {
                            "success": True,
                            "approval_status": "AUTO_APPROVED",
                            "primary_group_id": str(clean_group_id),
                            "post_id": str(primary_post_id),
                            "post_url": primary_url,
                            "message": f"Đăng bài thành công lên nhóm chính ({clean_group_id}){crosspost_log_str}! Link: {primary_url}",
                            "group_id": str(clean_group_id),
                            "crossposted_groups": clean_sub_groups,
                            "crosspost_group_ids": clean_sub_groups,
                            "crosspost_count": len(clean_sub_groups),
                            "is_cookie_dead": False,
                            "debug_detail": {
                                "primary_post_id": str(primary_post_id),
                                "primary_url": primary_url,
                                "story": story,
                                "crossposted_groups": clean_sub_groups,
                                "steps": trace_steps,
                                "attempts": attempt,
                                "doc_id": cur_doc_id
                            }
                        }

                    # Check for errors
                    errors = data.get("errors", []) if isinstance(data, dict) else []
                    err_msg = ""
                    err_code = 0
                    if errors and isinstance(errors, list):
                        err_msg = errors[0].get("message", "") if isinstance(errors[0], dict) else str(errors[0])
                        err_code = errors[0].get("code", 0) if isinstance(errors[0], dict) else 0

                    last_error_data = data

                    # If missing variable error on this doc_id, try fallback doc_id
                    if "missing_required_variable_value" in err_msg.lower() and cur_doc_id != doc_ids_to_try[-1]:
                        trace_steps.append(f"[WARN] doc_id {cur_doc_id} gặp lỗi missing_required_variable_value. Thử fallback doc_id...")
                        continue

                    # If fatal error, break to evaluate
                    break

                # Evaluate final error
                data = last_error_data or {}
                raw_resp_str = post_resp.text if 'post_resp' in locals() and post_resp else json.dumps(data, ensure_ascii=False, default=str)
                resp_status = post_resp.status_code if 'post_resp' in locals() and post_resp else 400
                resp_hdrs = dict(post_resp.headers) if 'post_resp' in locals() and post_resp else {}

                parsed_err = parse_facebook_error(
                    response_status=resp_status,
                    response_text=raw_resp_str,
                    response_headers=resp_hdrs,
                    account_fail_count=attempt
                )

                errors = data.get("errors", []) if isinstance(data, dict) else []
                err_msg = ""
                err_code = parsed_err.get("error_code") or 0
                if errors and isinstance(errors, list):
                    err_msg = errors[0].get("message", "") if isinstance(errors[0], dict) else str(errors[0])
                    if not err_code:
                        err_code = errors[0].get("code", 0) if isinstance(errors[0], dict) else 0

                is_cookie_dead = parsed_err["is_checkpoint"]
                error_type = "COOKIE_DIE" if is_cookie_dead else ("RATE_LIMIT" if parsed_err["is_rate_limit"] else None)

                if is_cookie_dead:
                    user_message = f"Cookie hết hạn / Checkpoint: {parsed_err['summary'] or err_msg}"
                elif error_type == "RATE_LIMIT":
                    duration_info = f" (Mở lại: {parsed_err['unlock_time_str']})" if parsed_err.get("unlock_time_str") else ""
                    user_message = f"Tài khoản bị Rate Limit / Chặn đăng bài: {parsed_err['summary'] or err_msg}{duration_info}"
                elif "permission" in (err_msg or "").lower() or "member" in (err_msg or "").lower():
                    user_message = f"Clone chưa tham gia nhóm hoặc nhóm chỉ cho Admin đăng: {err_msg}"
                else:
                    user_message = f"Lỗi Facebook: {parsed_err['summary'] or err_msg or 'Không thể tạo bài viết'}"

                if is_cookie_dead or error_type == "RATE_LIMIT":
                    return {
                        "success": False,
                        "approval_status": "FAILED",
                        "post_id": None,
                        "post_url": None,
                        "message": user_message,
                        "group_id": clean_group_id,
                        "is_cookie_dead": is_cookie_dead,
                        "error_type": error_type,
                        "parsed_error": parsed_err,
                        "raw_response": raw_resp_str,
                        "debug_detail": {"errors": errors, "parsed_error": parsed_err, "steps": trace_steps, "raw_response": raw_resp_str}
                    }

                if attempt < max_attempts:
                    continue

                return {
                    "success": False,
                    "approval_status": "FAILED",
                    "post_id": None,
                    "post_url": None,
                    "message": user_message,
                    "group_id": clean_group_id,
                    "is_cookie_dead": False,
                    "error_type": error_type,
                    "parsed_error": parsed_err,
                    "raw_response": raw_resp_str,
                    "debug_detail": {"errors": errors, "parsed_error": parsed_err, "steps": trace_steps, "raw_response": raw_resp_str}
                }

        except Exception as e:
            tb_str = traceback.format_exc()
            trace_steps.append(f"[Exception - Lần {attempt}] {type(e).__name__}: {str(e)}")
            if attempt < max_attempts:
                continue

            return {
                "success": False,
                "approval_status": "FAILED",
                "post_id": None,
                "post_url": None,
                "message": f"Lỗi kết nối mạng khi đăng bài: {str(e)}",
                "group_id": clean_group_id,
                "is_cookie_dead": False,
                "error_type": None,
                "debug_detail": {"exception": str(e), "trace": tb_str, "steps": trace_steps}
            }

    return {
        "success": False,
        "approval_status": "FAILED",
        "post_id": None,
        "post_url": None,
        "message": "Không thể đăng bài sau các lần thử.",
        "group_id": clean_group_id,
        "is_cookie_dead": False,
        "error_type": None,
        "debug_detail": {"steps": trace_steps}
    }


# Backwards compatible aliases
post_to_group = publish_post_via_cookie
check_token_live = check_cookie_live
