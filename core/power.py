import subprocess
import os
import signal
import platform
from typing import Optional


class MacSleepPreventer:
    """Quản lý Power Assertion trên macOS qua công cụ `caffeinate`.

    Ngăn MacBook rơi vào trạng thái ngủ khi đang chạy chiến dịch cào/đăng bài.
    """

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None

    def start(self) -> bool:
        if platform.system() != "Darwin":
            return False  # Chỉ hỗ trợ macOS

        if self.process and self.process.poll() is None:
            return True  # Đang chạy sẵn

        try:
            # -d: Ngăn màn hình ngủ
            # -i: Ngăn idle sleep
            # -m: Ngăn disk idle
            # -s: Ngăn sleep khi cắm nguồn
            # -u: Đánh dấu có hoạt động người dùng
            self.process = subprocess.Popen(
                ["caffeinate", "-dimsu"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp
            )
            print(f"[POWER] ⚡ Đã kích hoạt chế độ No-Sleep (Caffeinate active: PID {self.process.pid})")
            return True
        except Exception as e:
            print(f"[POWER] ⚠️ Không thể bật Caffeinate: {e}")
            return False

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=2)
                print("[POWER] 💤 Đã tắt No-Sleep, hệ thống có thể ngủ bình thường.")
            except Exception:
                pass
            finally:
                self.process = None

    def is_active(self) -> bool:
        return self.process is not None and self.process.poll() is None


power_manager = MacSleepPreventer()
