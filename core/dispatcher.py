import asyncio
import time
import random
from typing import List, Dict, Any, Optional, Callable


class CampaignState:
    """Các hằng số trạng thái cho chiến dịch đăng bài."""
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class CampaignController:
    """Bộ điều phối trạng thái chiến dịch đăng bài với khả năng Tạm dừng/Tiếp tục (Pause/Resume)

    và thay đổi danh sách tài khoản trực tiếp (Hot-swap Accounts) trong thời gian thực.
    """

    def __init__(self):
        self.state: str = CampaignState.IDLE
        self.resume_event: asyncio.Event = asyncio.Event()
        self.resume_event.set()  # Mặc định mở cờ cho chạy
        self.active_account_ids: List[int] = []
        self.current_task_index: int = 0
        self.tasks: List[Dict[str, Any]] = []
        self.acc_usage: Dict[int, int] = {}
        self.max_quota_per_acc: int = 50

        # Stats tracking
        self.total_tasks: int = 0
        self.completed_tasks: int = 0
        self.success_count: int = 0
        self.failed_count: int = 0
        self.auto_approved_count: int = 0
        self.pending_count: int = 0
        self.current_group: str = ""
        self.current_token_preview: str = ""
        self.current_apt_title: str = ""
        self.current_status_msg: str = "Idle"
        self.batch_id: str = ""
        self.start_time: Optional[float] = None
        self.acc_pointer: int = 0

    def reset(
        self,
        total: int,
        account_ids: Optional[List[Any]] = None,
        max_quota_per_acc: int = 50,
        batch_id: str = ""
    ) -> None:
        """Khởi tạo lại trạng thái chiến dịch mới."""
        self.state = CampaignState.RUNNING
        self.resume_event.set()
        self.total_tasks = total
        self.completed_tasks = 0
        self.current_task_index = 0
        self.success_count = 0
        self.failed_count = 0
        self.auto_approved_count = 0
        self.pending_count = 0
        self.current_group = ""
        self.current_token_preview = ""
        self.current_apt_title = ""
        self.current_status_msg = "Khởi chạy chiến dịch..."
        self.batch_id = batch_id
        self.start_time = time.time()
        self.max_quota_per_acc = int(max_quota_per_acc)
        self.acc_pointer = 0

        parsed_ids = []
        for a in (account_ids or []):
            try:
                parsed_ids.append(int(a))
            except (ValueError, TypeError):
                pass
        self.active_account_ids = parsed_ids
        self.acc_usage = {aid: 0 for aid in parsed_ids}

    def pause(self) -> None:
        """Tạm dừng chiến dịch."""
        if self.state == CampaignState.RUNNING:
            self.state = CampaignState.PAUSED
            self.resume_event.clear()
            self.current_status_msg = "Chiến dịch đang tạm dừng..."

    def resume(self) -> None:
        """Tiếp tục chiến dịch."""
        if self.state == CampaignState.PAUSED:
            self.state = CampaignState.RUNNING
            self.resume_event.set()
            self.current_status_msg = "Đang tiếp tục chiến dịch..."

    def stop(self) -> None:
        """Hủy bỏ hoàn toàn chiến dịch."""
        self.state = CampaignState.STOPPED
        self.resume_event.set()  # Mở cờ để worker thoát khỏi await nếu đang kẹt pause
        self.current_status_msg = "Chiến dịch đã kết thúc hoặc bị hủy."

    def finish(self) -> None:
        """Đánh dấu hoàn tất chiến dịch."""
        self.state = CampaignState.IDLE
        self.resume_event.set()
        self.current_status_msg = "Chiến dịch đã hoàn tất."

    def update_accounts(self, new_account_ids: List[Any]) -> None:
        """Cập nhật danh sách tài khoản mới khi đang tạm dừng hoặc đang chạy (Hot-swap)."""
        parsed_ids = []
        for a in new_account_ids:
            try:
                parsed_ids.append(int(a))
            except (ValueError, TypeError):
                pass
        self.active_account_ids = parsed_ids
        for aid in self.active_account_ids:
            if aid not in self.acc_usage:
                self.acc_usage[aid] = 0

    def get_next_available_account(self, live_db_accounts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Chọn tài khoản tiếp theo theo Round-Robin từ danh sách active_account_ids thỏa mãn hạn mức và LIVE."""
        if not self.active_account_ids:
            return None

        db_live_map = {}
        for a in live_db_accounts:
            # Chỉ lấy tài khoản LIVE và không bị Rate Limit
            if a.get("status") == "LIVE" and not a.get("is_rate_limited"):
                try:
                    db_live_map[int(a["id"])] = a
                except (ValueError, TypeError, KeyError):
                    pass

        # Lọc các tài khoản còn LIVE và chưa vượt quá hạn mức (max_quota <= 0 là vô hạn)
        available_ids = [
            acc_id for acc_id in self.active_account_ids
            if acc_id in db_live_map and (self.max_quota_per_acc <= 0 or self.acc_usage.get(acc_id, 0) < self.max_quota_per_acc)
        ]

        if not available_ids:
            return None

        # Xoay vòng Round Robin
        selected_id = available_ids[self.acc_pointer % len(available_ids)]
        self.acc_pointer += 1
        return db_live_map.get(selected_id)

    def record_account_usage(self, acc: Any) -> None:
        """Ghi nhận 1 lượt sử dụng cho tài khoản."""
        if isinstance(acc, dict):
            raw_id = acc.get("id")
        else:
            raw_id = acc
        try:
            aid = int(raw_id)
            self.acc_usage[aid] = self.acc_usage.get(aid, 0) + 1
        except (ValueError, TypeError):
            pass

    @property
    def is_running(self) -> bool:
        return self.state == CampaignState.RUNNING

    @property
    def is_paused(self) -> bool:
        return self.state == CampaignState.PAUSED

    @property
    def is_active(self) -> bool:
        return self.state in (CampaignState.RUNNING, CampaignState.PAUSED)

    @property
    def should_stop(self) -> bool:
        return self.state == CampaignState.STOPPED

    def to_dict(self) -> Dict[str, Any]:
        """Xuất thông tin trạng thái phục vụ API / frontend monitor."""
        return {
            "state": self.state,
            "is_running": self.state == CampaignState.RUNNING,
            "is_paused": self.state == CampaignState.PAUSED,
            "current_index": self.current_task_index,
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "auto_approved_count": self.auto_approved_count,
            "pending_count": self.pending_count,
            "current_group": self.current_group,
            "current_token_preview": self.current_token_preview,
            "current_apt_title": self.current_apt_title,
            "status_message": self.current_status_msg,
            "active_accounts": self.active_account_ids,
            "acc_usage": self.acc_usage,
            "max_quota_per_acc": self.max_quota_per_acc,
            "batch_id": self.batch_id
        }


def generate_posting_matrix(
    apartment_ids: List[int],
    group_ids: List[str],
    account_ids: List[Any],
    max_quota_per_acc: int = 50,
    allow_repeat_group: bool = True,
    enable_crosspost: bool = False,
    shuffle_groups: bool = False
) -> List[Dict[str, Any]]:
    """Sinh ma trận hàng đợi đăng bài tối ưu bằng thuật toán Shifted Round-Robin.

    - Hỗ trợ chế độ Đăng chéo Comet Crossposting (Gom 1 nhóm chính + tối đa 9 nhóm phụ vào 1 lượt đăng).
    - Căn hộ được tịnh tiến bước nhảy qua từng vòng (Pass) để giãn cách tối đa việc đăng cùng 1 căn hộ vào 1 nhóm.
    - Phân bổ công việc xoay vòng cho danh sách tài khoản theo hạn ngạch `max_quota_per_acc`.
    - Hỗ trợ xáo trộn ngẫu nhiên danh sách nhóm (shuffle_groups=True) để tránh cố định thứ tự qua các lần chạy.

    Returns:
    [
        {
            "task_index": 1,
            "account_id": 1,
            "group_id": "1456724238445887",
            "crosspost_group_ids": ["123", "456"],
            "total_groups_in_batch": 3,
            "apartment_id": 1,
            "pass_index": 0,
            "is_crosspost": True
        },
        ...
    ]
    """
    if not apartment_ids or not group_ids:
        return []

    # Clean inputs
    apt_list = [int(a) for a in apartment_ids]
    grp_list = [str(g).strip() for g in group_ids if str(g).strip()]
    if shuffle_groups:
        random.shuffle(grp_list)
    parsed_acc_list = []
    for a in account_ids:
        try:
            parsed_acc_list.append(int(a))
        except (ValueError, TypeError):
            pass

    if not apt_list or not grp_list:
        return []

    num_apts = len(apt_list)

    # Nếu allow_repeat_group=True: tối đa num_apts vòng (mỗi group nhận đủ các căn hộ)
    # Nếu allow_repeat_group=False: chỉ 1 vòng (mỗi group nhận đúng 1 căn hộ)
    max_passes = num_apts if allow_repeat_group else 1

    # 1. Sinh danh sách các lượt đăng (đơn lẻ hoặc theo cụm Crosspost 10 nhóm/lần)
    if enable_crosspost:
        group_chunks = []
        for i in range(0, len(grp_list), 10):
            chunk = grp_list[i:i + 10]
            if chunk:
                group_chunks.append({
                    "primary_group_id": chunk[0],
                    "crosspost_group_ids": chunk[1:],
                    "total_groups": len(chunk)
                })

        scheduled_items = []
        for pass_idx in range(max_passes):
            for c_idx, chunk_item in enumerate(group_chunks):
                apt_idx = (c_idx + pass_idx) % num_apts
                scheduled_items.append({
                    "group_id": chunk_item["primary_group_id"],
                    "crosspost_group_ids": chunk_item["crosspost_group_ids"],
                    "total_groups_in_batch": chunk_item["total_groups"],
                    "apartment_id": apt_list[apt_idx],
                    "pass_index": pass_idx,
                    "is_crosspost": len(chunk_item["crosspost_group_ids"]) > 0
                })
    else:
        scheduled_items = []
        for pass_idx in range(max_passes):
            for g_idx, group_id in enumerate(grp_list):
                apt_idx = (g_idx + pass_idx) % num_apts
                scheduled_items.append({
                    "group_id": group_id,
                    "crosspost_group_ids": [],
                    "total_groups_in_batch": 1,
                    "apartment_id": apt_list[apt_idx],
                    "pass_index": pass_idx,
                    "is_crosspost": False
                })

    # 2. Phân bổ các cặp trên cho danh sách tài khoản theo hạn ngạch
    final_tasks = []
    acc_usage = {acc: 0 for acc in parsed_acc_list}
    available_accs = list(parsed_acc_list) if parsed_acc_list else [0]
    acc_pointer = 0

    quota = int(max_quota_per_acc)

    for idx, item in enumerate(scheduled_items, 1):
        if parsed_acc_list and not available_accs:
            # Tất cả tài khoản đã đạt quota tối đa
            break

        current_acc = available_accs[acc_pointer % len(available_accs)] if available_accs else 0

        final_tasks.append({
            "task_index": idx,
            "account_id": current_acc,
            "group_id": item["group_id"],
            "crosspost_group_ids": item.get("crosspost_group_ids", []),
            "total_groups_in_batch": item.get("total_groups_in_batch", 1),
            "apartment_id": item["apartment_id"],
            "pass_index": item["pass_index"],
            "is_crosspost": item.get("is_crosspost", False)
        })

        if current_acc in acc_usage:
            acc_usage[current_acc] += 1
            if quota > 0 and acc_usage[current_acc] >= quota:
                available_accs.remove(current_acc)
                if not available_accs:
                    break
            else:
                acc_pointer += 1

    return final_tasks
