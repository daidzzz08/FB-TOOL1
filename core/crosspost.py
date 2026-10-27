import json
import re
from typing import List, Dict, Optional, Any
from curl_cffi import requests
from core.poster import DESKTOP_USER_AGENT, compute_jazoest, extract_fb_dtsg_from_html
from core.network import execute_with_smart_retry_sync

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
DOC_ID_CROSSPOST_PICKER = "27326873186925786"
DOC_ID_CROSSPOST_REFETCH = "26999555389693570"


def get_eligible_crosspost_groups(
    primary_group_id: str,
    cookie_str: str,
    proxy: Optional[str] = None
) -> List[Dict[str, str]]:
    """Lấy danh sách các nhóm đủ điều kiện đăng chéo từ primary_group_id.

    Sử dụng GroupsCometCrosspostGroupPickerQuery (doc_id: 27326873186925786).
    """
    clean_group_id = str(primary_group_id).strip()
    clean_cookie = cookie_str.strip()
    if not clean_cookie or not clean_group_id:
        return []

    groups = []
    headers = {
        "Origin": "https://www.facebook.com",
        "Referer": f"https://www.facebook.com/groups/{clean_group_id}/",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": DESKTOP_USER_AGENT,
        "X-Fb-Friendly-Name": "GroupsCometCrosspostGroupPickerQuery",
        "Sec-Fetch-Site": "same-origin"
    }

    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    try:
        session = requests.Session(impersonate="chrome124", proxies=proxies)
        for item in clean_cookie.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                session.cookies.set(k.strip(), v.strip(), domain=".facebook.com")

        c_user_match = re.search(r'c_user=(\d+)', clean_cookie)
        user_id = c_user_match.group(1) if c_user_match else "0"

        home_resp = execute_with_smart_retry_sync(
            lambda: session.get("https://www.facebook.com/", headers=headers, timeout=15),
            max_retries=2
        )
        fb_dtsg = extract_fb_dtsg_from_html(home_resp.text)
        jazoest = compute_jazoest(fb_dtsg)

        variables = {
            "group_id": clean_group_id,
            "scale": 2
        }
        payload = {
            "av": str(user_id),
            "__user": str(user_id),
            "__a": "1",
            "fb_dtsg": fb_dtsg,
            "jazoest": jazoest,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "GroupsCometCrosspostGroupPickerQuery",
            "doc_id": DOC_ID_CROSSPOST_PICKER,
            "variables": json.dumps(variables, ensure_ascii=False, default=str)
        }

        resp = execute_with_smart_retry_sync(
            lambda: session.post(GRAPHQL_URL, data=payload, headers=headers, timeout=20),
            max_retries=2
        )

        if resp.status_code == 200:
            raw = resp.text
            if raw.startswith("for (;;);"):
                raw = raw[len("for (;;);"):]
            try:
                res_data = json.loads(raw)
            except Exception:
                res_data = {}

            edges = (
                res_data.get("data", {})
                .get("me", {})
                .get("groups_eligible_for_group_composer_crossposting", {})
                .get("edges", [])
            )
            for edge in edges:
                node = edge.get("node", {})
                if node and node.get("id"):
                    groups.append({
                        "id": str(node.get("id")),
                        "name": node.get("name", f"Group #{node.get('id')}"),
                        "photo": node.get("photo", {}).get("uri", "") if isinstance(node.get("photo"), dict) else ""
                    })
    except Exception as e:
        print(f"[CROSSPOST] Lỗi bóc tách nhóm đăng chéo: {e}")

    return groups


def chunk_groups_for_crossposting(group_ids: List[str], chunk_size: int = 10) -> List[Dict[str, Any]]:
    """Gom danh sách Group IDs thành các cụm 1 nhóm chính + tối đa (chunk_size - 1) nhóm phụ.

    Ví dụ: chunk_size=10 -> 1 nhóm chính + tối đa 9 nhóm phụ.
    """
    cleaned = [str(g).strip() for g in group_ids if str(g).strip()]
    batches = []
    chunk_size = max(1, min(10, int(chunk_size)))

    for i in range(0, len(cleaned), chunk_size):
        chunk = cleaned[i:i + chunk_size]
        if not chunk:
            continue
        primary = chunk[0]
        secondaries = chunk[1:]
        batches.append({
            "primary_group_id": primary,
            "crosspost_group_ids": secondaries,
            "total_in_batch": len(chunk)
        })

    return batches
