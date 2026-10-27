import json
import re
import time
import random
import uuid
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlencode
from typing import List, Optional, Dict, Any, Generator, Tuple
from curl_cffi import requests

FB_COMET_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
FB_GRAPH_URL = "https://graph.facebook.com/v19.0"
DESKTOP_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def format_proxy(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    """Format proxy string into curl_cffi proxies dict."""
    if not proxy or not proxy.strip():
        return None
    proxy = proxy.strip()
    if not (proxy.startswith("http://") or proxy.startswith("https://") or proxy.startswith("socks5://")):
        proxy = f"http://{proxy}"
    return {"http": proxy, "https": proxy}


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """Parse raw cookie string (key1=val1; key2=val2) into a dictionary."""
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
    """Compute jazoest value from fb_dtsg token."""
    if not fb_dtsg:
        return "2957"
    return "2" + str(sum(ord(c) for c in fb_dtsg))


def extract_fb_dtsg_from_html(html: str) -> str:
    """Extracts fb_dtsg token from Facebook Comet HTML page."""
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


# ==================== FETCH JOINED GROUPS VIA COOKIE ====================

def fetch_joined_groups_by_cookie(
    cookie_str: str,
    proxy: Optional[str] = None,
    doc_id: str = "3884641628300421"
) -> List[Dict[str, Any]]:
    """
    Fetches the complete list of Facebook Groups the account has joined using Cookie via
    ComposerGroupTaggerTypeaheadGroupsQuery (doc_id: 3884641628300421).
    
    Returns:
    [
        {
            "id": "1456724238445887",
            "group_id": "1456724238445887",
            "name": "Group Name",
            "url": "https://www.facebook.com/groups/1456724238445887",
            "avatar": "https://..."
        }
    ]
    """
    clean_cookie = cookie_str.strip()
    if not clean_cookie:
        return []

    cookie_dict = parse_cookie_string(clean_cookie)
    user_id = cookie_dict.get("c_user", "")
    if not user_id:
        m = re.search(r'c_user=(\d+)', clean_cookie)
        if m:
            user_id = m.group(1)

    proxies = format_proxy(proxy)
    headers = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Origin": "https://www.facebook.com",
        "Referer": "https://www.facebook.com",
        "Sec-Fetch-Site": "same-origin",
        "Cookie": clean_cookie
    }

    results: List[Dict[str, Any]] = []
    seen_ids = set()

    try:
        with requests.Session(impersonate="chrome124", proxies=proxies) as session:
            for k, v in cookie_dict.items():
                session.cookies.set(k.strip(), v.strip(), domain=".facebook.com")

            # 1. Fetch desktop home to get fb_dtsg
            home_resp = session.get("https://www.facebook.com/", headers=headers, timeout=20)
            fb_dtsg = extract_fb_dtsg_from_html(home_resp.text)
            if not fb_dtsg:
                feed_resp = session.get("https://www.facebook.com/groups/feed/", headers=headers, timeout=20)
                fb_dtsg = extract_fb_dtsg_from_html(feed_resp.text)
            jazoest = compute_jazoest(fb_dtsg)

            # 2. Query ComposerGroupTaggerTypeaheadGroupsQuery
            gql_headers = {
                **headers,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Fb-Friendly-Name": "ComposerGroupTaggerTypeaheadGroupsQuery"
            }

            variables = {
                "adminGroupsCount": 90,
                "memberGroupsCount": 5000,
                "scale": 1.5,
                "count": 10,
                "cursor": None
            }

            payload = {
                "av": str(user_id or "0"),
                "__user": str(user_id or "0"),
                "__a": "1",
                "fb_dtsg": fb_dtsg,
                "jazoest": jazoest,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "ComposerGroupTaggerTypeaheadGroupsQuery",
                "doc_id": str(doc_id),
                "variables": json.dumps(variables, ensure_ascii=True, default=str)
            }

            resp = session.post(FB_COMET_GRAPHQL_URL, data=payload, headers=gql_headers, timeout=25)
            
            raw_text = resp.text
            if raw_text.startswith("for (;;);"):
                raw_text = raw_text[len("for (;;);"):]

            data = {}
            try:
                data = json.loads(raw_text)
            except Exception:
                for line in resp.text.split("\n"):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            data = json.loads(line)
                            if "data" in data:
                                break
                        except Exception:
                            pass

            res_data = data.get("data", {})
            non_admin_edges = res_data.get("nonAdminGroups", {}).get("groups_tab", {}).get("tab_groups_list", {}).get("edges", [])
            admin_edges = res_data.get("adminGroups", {}).get("groups_tab", {}).get("tab_groups_list", {}).get("edges", [])

            all_edges = non_admin_edges + admin_edges

            for edge in all_edges:
                node = edge.get("node", {}) if isinstance(edge, dict) else {}
                gid = str(node.get("id") or "").strip()
                name = node.get("name") or f"Group {gid}"
                url = node.get("url") or f"https://www.facebook.com/groups/{gid}"
                avatar = node.get("profile_picture", {}).get("uri", "") if isinstance(node.get("profile_picture"), dict) else ""

                if gid and gid.isdigit() and gid not in seen_ids:
                    seen_ids.add(gid)
                    results.append({
                        "id": gid,
                        "group_id": gid,
                        "name": name,
                        "url": url,
                        "avatar": avatar
                    })

            # Fallback recursive search if direct edge extraction is empty
            if not results and "data" in data:
                def recursive_extract(obj):
                    if isinstance(obj, dict):
                        if (obj.get("__typename") == "Group" or "group" in str(obj.get("url", "")).lower()) and obj.get("id"):
                            gid = str(obj.get("id")).strip()
                            name = obj.get("name") or obj.get("title", {}).get("text") if isinstance(obj.get("title"), dict) else obj.get("name")
                            if gid.isdigit() and name and gid not in seen_ids:
                                seen_ids.add(gid)
                                results.append({
                                    "id": gid,
                                    "group_id": gid,
                                    "name": str(name).strip(),
                                    "url": f"https://www.facebook.com/groups/{gid}",
                                    "avatar": ""
                                })
                        for v in obj.values():
                            recursive_extract(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            recursive_extract(item)

                recursive_extract(res_data)

    except Exception as e:
        print(f"[SCANNER ERROR] Lỗi lấy nhóm đã tham gia bằng Cookie: {e}")

    return results


# ==================== KEYWORD GROUP SCANNER ====================

def is_valid_text_snippet(text: str) -> bool:
    """Loại bỏ URL ảnh, token, hash, URL link hoặc chuỗi không phải text hiển thị."""
    if not text or not isinstance(text, str):
        return False
    t = text.lower()
    bad_tokens = [
        "http://", "https://", "scontent", "fbcdn", ".jpg", ".png", ".webp",
        "urlgen", "ohc=", "oe=", "fna=", "fbid=", "dst-jpg", "p100x100",
        "p200x200", "p50x50", "base64", "graphql", "relayprovider"
    ]
    if any(bad in t for bad in bad_tokens):
        return False
    if len(text) > 160:
        return False
    return True


def decode_facebook_html(raw_html: str) -> str:
    """Giải mã toàn bộ các ký tự unicode escape trong HTML sang UTF-8 thuần"""
    if not raw_html:
        return ""
    try:
        return re.sub(
            r'\\u([0-9a-fA-F]{4})', 
            lambda m: chr(int(m.group(1), 16)), 
            raw_html
        )
    except Exception:
        return raw_html


def parse_member_count(text: str) -> int:
    """Bóc tách số lượng thành viên từ text snippet bất kỳ của Facebook."""
    if not is_valid_text_snippet(text):
        return 0
    try:
        clean = str(text).replace('\u00a0', ' ').replace('\u202f', ' ').replace('\u00b7', ' ').replace('\u2022', ' ').strip()
        m_unit = re.search(
            r'([\d]+(?:[\.,]\d+)?)\s*([kKmM]|nghìn|ngàn|triệu|tr)\s*(?:thành viên|th\u00e0nh vi\u00ean|members|member|người|ng\u01b0\u1eddi|người tham gia)?\b',
            clean,
            re.IGNORECASE
        )
        if m_unit:
            num_str = m_unit.group(1).replace(',', '.')
            unit = m_unit.group(2).lower()
            val = float(num_str)
            if unit in ['k', 'nghìn', 'ngàn']:
                return int(val * 1000)
            elif unit in ['m', 'triệu', 'tr']:
                return int(val * 1000000)

        m_int = re.search(
            r'([\d\s\.,]+)\s*(?:thành viên|th\u00e0nh vi\u00ean|members|member|người tham gia|người|ng\u01b0\u1eddi)',
            clean,
            re.IGNORECASE
        )
        if m_int:
            digits = m_int.group(1).replace('.', '').replace(',', '').replace(' ', '').strip()
            if digits.isdigit():
                return int(digits)
    except Exception:
        pass
    return 0


def extract_member_count_from_node(node_obj: Any) -> tuple[int, str]:
    """Duyệt đệ quy toàn bộ các trường để tìm số lượng thành viên."""
    if not node_obj or not isinstance(node_obj, (dict, list)):
        return 0, ""

    if isinstance(node_obj, dict):
        for num_field in ["group_member_count", "member_count", "members_count", "total_members"]:
            val = node_obj.get(num_field)
            if isinstance(val, (int, float)) and val > 0:
                return int(val), f"{int(val):,} thành viên (numeric field)"
        
        for sub_key in ["group_members", "members"]:
            sub = node_obj.get(sub_key)
            if isinstance(sub, dict):
                cnt = sub.get("count") or sub.get("total_count")
                if isinstance(cnt, (int, float)) and cnt > 0:
                    return int(cnt), f"{int(cnt):,} thành viên (nested count)"

    all_strings = []

    def collect_strings(obj):
        if isinstance(obj, str):
            if is_valid_text_snippet(obj):
                all_strings.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                collect_strings(v)
        elif isinstance(obj, list):
            for item in obj:
                collect_strings(item)

    collect_strings(node_obj)

    best_count = 0
    best_snippet = ""

    for s in all_strings:
        s_clean = decode_facebook_html(s)
        s_lower = s_clean.lower()
        if any(kw in s_lower for kw in ['thành viên', 'th\u00e0nh vi\u00ean', 'members', 'member', 'người', 'ng\u01b0\u1eddi', 'người tham gia', 'k', 'm', 'tr', 'triệu', 'nghìn', 'ngàn']):
            count = parse_member_count(s_clean)
            if count > best_count:
                best_count = count
                best_snippet = s_clean

    return best_count, best_snippet


def extract_groups_from_comet_payload(data_obj: Any) -> List[Dict[str, Any]]:
    """Recursively inspects Comet GraphQL / Search response to extract Facebook Group entities."""
    found_groups: List[Dict[str, Any]] = []
    seen_ids = set()

    def recursive_find(obj: Any):
        if isinstance(obj, dict):
            # 1. Ưu tiên cấu trúc modern Comet Search ListCell view_model
            vm = obj.get("view_model") or obj.get("rendering_strategy", {}).get("view_model")
            if isinstance(vm, dict) and ("profile" in vm or "loggedProfile" in vm):
                prof = vm.get("profile") or vm.get("loggedProfile") or {}
                prof_typename = prof.get("__typename") or prof.get("type")
                if prof_typename == "Group" or "groups" in str(prof.get("url", "")).lower():
                    gid = str(prof.get("id") or "")
                    name = prof.get("name")
                    if gid.isdigit() and name and gid not in seen_ids:
                        seen_ids.add(gid)
                        snip_obj = vm.get("primary_snippet_text_with_entities") or {}
                        snip_text = snip_obj.get("text", "") if isinstance(snip_obj, dict) else str(snip_obj)
                        mc = parse_member_count(snip_text) if snip_text else 0
                        is_pub = "công khai" in snip_text.lower() or "public" in snip_text.lower()
                        found_groups.append({
                            "id": gid,
                            "group_id": gid,
                            "name": html.unescape(str(name)).strip(),
                            "privacy": "PUBLIC" if is_pub else "CLOSED",
                            "visibility": "OPEN" if is_pub else "CLOSED",
                            "member_count": mc,
                            "snippet": snip_text,
                            "url": prof.get("url") or f"https://www.facebook.com/groups/{gid}"
                        })

            # 2. Cấu trúc node Group thông thường (dành cho các query / response khác)
            target_node = obj.get("node") if isinstance(obj.get("node"), dict) else obj
            is_group = (
                target_node.get("__typename") == "Group" or
                "group_privacy" in target_node or
                "group_privacy" in obj or
                "group_snippets" in obj or
                "group_snippets" in target_node or
                (target_node.get("id") and "group" in str(target_node.get("url", "")).lower())
            )

            group_id = str(target_node.get("id") or target_node.get("group_id") or obj.get("id") or obj.get("group_id") or "")
            name = target_node.get("name") or obj.get("name")
            if not name and isinstance(target_node.get("title"), dict):
                name = target_node.get("title", {}).get("text")

            if is_group and group_id.isdigit() and name and group_id not in seen_ids:
                seen_ids.add(group_id)
                member_count, raw_snippet_str = extract_member_count_from_node(obj)
                if member_count == 0 and target_node != obj:
                    m_cnt, m_snip = extract_member_count_from_node(target_node)
                    if m_cnt > 0:
                        member_count, raw_snippet_str = m_cnt, m_snip

                privacy_raw = str(target_node.get("group_privacy") or target_node.get("privacy") or obj.get("group_privacy") or obj.get("privacy") or "").lower()
                is_public = "public" in privacy_raw or "open" in privacy_raw or "công khai" in privacy_raw
                privacy = "PUBLIC" if is_public else "CLOSED"

                found_groups.append({
                    "id": group_id,
                    "group_id": group_id,
                    "name": html.unescape(str(name)).strip(),
                    "privacy": privacy,
                    "visibility": "OPEN" if is_public else "CLOSED",
                    "member_count": member_count,
                    "snippet": raw_snippet_str,
                    "url": f"https://www.facebook.com/groups/{group_id}"
                })

            for v in obj.values():
                recursive_find(v)
        elif isinstance(obj, list):
            for item in obj:
                recursive_find(item)

    recursive_find(data_obj)
    return found_groups


def extract_groups_from_search_html(html_text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Bóc tách các nhóm đã hydrate sẵn và danh sách Unicorn IDs từ HTML trang tìm kiếm nhóm Facebook.
    """
    hydrated_groups: List[Dict[str, Any]] = []
    unicorn_ids: List[str] = []
    seen_ids = set()

    # Pattern trích xuất RelayPrefetchedStreamCache chứa kết quả tìm kiếm Comet Search
    pattern = r'\["RelayPrefetchedStreamCache","next",\[\],\["[^"]*Search[^"]*",(\{.*?\})\]\]'
    for m in re.finditer(pattern, html_text):
        try:
            cache_json = m.group(1)
            obj = json.loads(cache_json)
            result_data = obj.get("__bbox", {}).get("result", {}).get("data", {}) or obj.get("data", {})
            serp_res = result_data.get("serpResponse", {}).get("results", {})
            
            # 1. Bóc tách các nhóm đã render đầy đủ
            groups = extract_groups_from_comet_payload(serp_res)
            for g in groups:
                if g["group_id"] not in seen_ids:
                    seen_ids.add(g["group_id"])
                    hydrated_groups.append(g)

            # 2. Bóc tách danh sách unicorn_ids để crawl sâu hơn khi cần vượt quá số nhóm hydrate
            page_info = serp_res.get("page_info", {})
            end_cursor = page_info.get("end_cursor", "")
            if end_cursor:
                try:
                    c_obj = json.loads(end_cursor)
                    gt = c_obj.get("flow_cursors_serialized", {}).get("GROUPS_TAB", "")
                    if gt:
                        gt_obj = json.loads(gt)
                        u_list = gt_obj.get("processed_unicorn_ids", [])
                        for uid in u_list:
                            suid = str(uid).strip()
                            if suid.isdigit() and suid not in unicorn_ids:
                                unicorn_ids.append(suid)
                except Exception:
                    pass
        except Exception:
            pass

    return hydrated_groups, unicorn_ids


def fetch_single_group_meta(
    gid: str,
    cookies_dict: Dict[str, str],
    proxy: Optional[str] = None,
    timeout: int = 12
) -> Optional[Dict[str, Any]]:
    """Truy vấn metadata (tên, số thành viên, quyền riêng tư) của 1 nhóm Facebook bằng HTTP."""
    proxies = format_proxy(proxy)
    try:
        with requests.Session(impersonate="chrome124", proxies=proxies) as s:
            s.cookies.update(cookies_dict)
            headers = {
                "User-Agent": DESKTOP_USER_AGENT,
                "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
            }
            url = f"https://www.facebook.com/groups/{gid}/"
            r = s.get(url, headers=headers, timeout=timeout)
            if r.status_code != 200 or len(r.text) < 500:
                return None

            # Tiêu đề nhóm
            tm = re.search(r'<title>([^<]+)</title>', r.text)
            raw_title = tm.group(1) if tm else ""
            cleaned_title = re.sub(r'\s*\|\s*Facebook\s*$', '', raw_title, flags=re.IGNORECASE).strip()
            name = html.unescape(cleaned_title) if cleaned_title else f"Nhóm {gid}"

            # Số lượng thành viên
            m_match = re.search(
                r'(\d+[\d\.,]*\s*(?:nghìn|triệu|k|m)?\s*thành viên|\d+[\d\.,]*\s*(?:K|M)?\s*members)',
                r.text,
                re.IGNORECASE
            )
            snip = m_match.group(1) if m_match else ""
            mc = parse_member_count(snip) if snip else 0

            # Quyền riêng tư
            text_lower = r.text.lower()
            is_pub = "công khai" in text_lower or "public" in text_lower

            return {
                "id": str(gid),
                "group_id": str(gid),
                "name": name,
                "member_count": mc,
                "privacy": "PUBLIC" if is_pub else "CLOSED",
                "visibility": "OPEN" if is_pub else "CLOSED",
                "snippet": snip or f"{mc:,} thành viên",
                "url": url
            }
    except Exception:
        return None


def find_cursor_recursively(data_obj: Any) -> Optional[str]:
    """Finds next page cursor in arbitrary JSON payload."""
    if isinstance(data_obj, dict):
        if "end_cursor" in data_obj and isinstance(data_obj["end_cursor"], str) and data_obj["end_cursor"]:
            return data_obj["end_cursor"]
        if "browse_cursor_serialized" in data_obj and isinstance(data_obj["browse_cursor_serialized"], str) and data_obj["browse_cursor_serialized"]:
            return data_obj["browse_cursor_serialized"]
        for v in data_obj.values():
            found = find_cursor_recursively(v)
            if found:
                return found
    elif isinstance(data_obj, list):
        for item in data_obj:
            found = find_cursor_recursively(item)
            if found:
                return found
    return None


def stream_search_groups_by_keywords(
    keywords: List[str],
    cookie_str: str,
    limit_per_keyword: int = 30,
    min_members: int = 1000,
    doc_id: str = "28372304112465940",
    proxy: Optional[str] = None
) -> Generator[Dict[str, Any], None, None]:
    """
    Generator yielding real-time stream events while crawling Facebook Groups for multiple keywords.
    Sử dụng kiến trúc lai:
    1. Trích xuất ngay lập tức các nhóm đã hydrate sẵn từ RelayPrefetchedStreamCache trong trang Search của Meta.
    2. Nếu cần thêm nhóm (> 8 nhóm), phân tích danh sách Unicorn IDs được Meta xếp hạng cao nhất và bóc tách song song.
    """
    if not keywords or not cookie_str or not cookie_str.strip():
        yield {"type": "error", "message": "Thiếu danh sách từ khóa hoặc Cookie Facebook."}
        return

    proxies = format_proxy(proxy)
    cookies = parse_cookie_string(cookie_str)
    user_id = cookies.get("c_user", "0")

    headers_base = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Site": "same-origin",
    }

    seen_group_ids = set()
    valid_group_ids = set()

    with requests.Session(impersonate="chrome124", proxies=proxies) as session:
        session.cookies.update(cookies)

        yield {"type": "status", "message": f"Đang xác thực Cookie và khởi tạo phiên kết nối (UID: {user_id})..."}

        clean_keywords = [k.strip() for k in keywords if k and k.strip()]
        total_kw = len(clean_keywords)

        for kw_idx, clean_kw in enumerate(clean_keywords, 1):
            yield {
                "type": "status",
                "message": f"[{kw_idx}/{total_kw}] Bắt đầu cào từ khóa: '{clean_kw}' (Tối đa: {limit_per_keyword} nhóm, Lọc TV: >= {min_members:,})..."
            }

            kw_found_count = 0
            encoded_kw = quote(clean_kw)
            search_url = f"https://www.facebook.com/search/groups/?q={encoded_kw}"

            try:
                resp = session.get(search_url, headers=headers_base, timeout=20)
                hydrated_groups, unicorn_ids = extract_groups_from_search_html(resp.text)
            except Exception as req_err:
                yield {
                    "type": "status",
                    "message": f"⚠️ Lỗi kết nối trang tìm kiếm cho '{clean_kw}': {req_err}"
                }
                hydrated_groups, unicorn_ids = [], []

            # 1. Duyệt qua các nhóm đã hydrate sẵn từ trang search của Meta
            for g in hydrated_groups:
                gid = g["group_id"]
                gname = g.get("name", f"Group {gid}")
                member_count = g.get("member_count", 0)
                passes_filter = (min_members <= 0) or (member_count >= min_members)

                if gid not in seen_group_ids:
                    seen_group_ids.add(gid)
                    if passes_filter:
                        valid_group_ids.add(gid)
                        kw_found_count += 1
                        yield {
                            "type": "group_found",
                            "keyword": clean_kw,
                            "group": g,
                            "total_collected": len(valid_group_ids)
                        }
                        if kw_found_count >= limit_per_keyword:
                            break
                    else:
                        yield {
                            "type": "status",
                            "message": f"ℹ️ Bỏ qua nhóm '{gname}' ({member_count:,} TV < {min_members:,} TV tối thiểu)"
                        }

            # 2. Nếu người dùng yêu cầu nhiều hơn số nhóm hydrate sẵn (>8) và có danh sách unicorn_ids từ Meta
            if kw_found_count < limit_per_keyword and unicorn_ids:
                candidate_ids = [uid for uid in unicorn_ids if uid not in seen_group_ids]
                if candidate_ids:
                    needed = limit_per_keyword - kw_found_count
                    yield {
                        "type": "status",
                        "message": f"🔍 Khai thác thêm {len(candidate_ids)} nhóm tiềm năng từ chỉ mục Meta..."
                    }
                    
                    # Lấy batch vừa đủ (kèm dự phòng nhóm bị lọc thành viên)
                    batch_size = min(len(candidate_ids), max(needed * 2, 10))
                    fetch_batch = candidate_ids[:batch_size]

                    with ThreadPoolExecutor(max_workers=min(len(fetch_batch), 6)) as executor:
                        futures = {
                            executor.submit(fetch_single_group_meta, uid, cookies, proxy): uid 
                            for uid in fetch_batch
                        }
                        for f in as_completed(futures):
                            if kw_found_count >= limit_per_keyword:
                                break
                            res = f.result()
                            if not res:
                                continue
                            gid = res["group_id"]
                            if gid in seen_group_ids:
                                continue
                            seen_group_ids.add(gid)
                            member_count = res.get("member_count", 0)
                            passes_filter = (min_members <= 0) or (member_count >= min_members)

                            if passes_filter:
                                valid_group_ids.add(gid)
                                kw_found_count += 1
                                yield {
                                    "type": "group_found",
                                    "keyword": clean_kw,
                                    "group": res,
                                    "total_collected": len(valid_group_ids)
                                }
                            else:
                                yield {
                                    "type": "status",
                                    "message": f"ℹ️ Bỏ qua nhóm '{res.get('name')}' ({member_count:,} TV < {min_members:,} TV tối thiểu)"
                                }

            yield {
                "type": "keyword_done",
                "keyword": clean_kw,
                "count": kw_found_count,
                "message": f"Hoàn tất từ khóa '{clean_kw}': Tìm được {kw_found_count} nhóm đạt chuẩn."
            }

            if kw_idx < total_kw:
                time.sleep(round(random.uniform(1.0, 2.0), 2))

    yield {
        "type": "completed",
        "total_found": len(valid_group_ids),
        "message": f"🎉 Hoàn tất cào {len(clean_keywords)} từ khóa! Tổng cộng thu thập được {len(valid_group_ids)} nhóm duy nhất."
    }
