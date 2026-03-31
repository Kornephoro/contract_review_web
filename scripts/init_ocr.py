from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from legal_review.ocr import (
    PYTHON_313_COMMAND,
    REQUIRED_PYTHON_DISPLAY,
    get_paddle_ocr_status,
    initialize_paddle_ocr,
    is_required_python_version,
)


def main() -> int:
    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    print("开始初始化本地 OCR 模型...")
    print(f"当前 Python：{sys.executable} ({current_version})")

    if not is_required_python_version():
        print(
            (
                f"\n当前解释器不是 Python {REQUIRED_PYTHON_DISPLAY}。"
                f"请改用 `{PYTHON_313_COMMAND} scripts/init_ocr.py` 运行初始化脚本。"
            ),
            file=sys.stderr,
        )
        return 1

    status_before = get_paddle_ocr_status()
    print("当前状态：")
    print(json.dumps(status_before, ensure_ascii=False, indent=2))

    if status_before["ready"]:
        print("\nOCR 已初始化，无需重复下载。")
        return 0

    try:
        status_after = initialize_paddle_ocr()
    except Exception as exc:
        print(f"\nOCR 初始化失败：{exc}", file=sys.stderr)
        return 1

    print("\nOCR 初始化完成。")
    print(json.dumps(status_after, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
