from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from functools import lru_cache
from pathlib import Path


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
PADDLE_MODEL_CACHE_DIR = Path(
    os.environ.get(
        "LEGAL_REVIEW_PADDLE_MODEL_DIR",
        str(Path.home() / ".paddlex" / "official_models"),
    )
).expanduser()
PADDLE_OCR_PROFILE = os.environ.get("LEGAL_REVIEW_PADDLE_PROFILE", "mobile").strip().lower() or "mobile"
if PADDLE_OCR_PROFILE not in {"mobile", "server"}:
    PADDLE_OCR_PROFILE = "mobile"
REQUIRED_PYTHON_VERSION = (3, 11)
REQUIRED_PYTHON_DISPLAY = "3.11+"
PYTHON_313_COMMAND = "python"

PADDLE_MODEL_NAMES = {
    "doc_orientation": "PP-LCNet_x1_0_doc_ori",
    "text_detection": "PP-OCRv5_mobile_det" if PADDLE_OCR_PROFILE == "mobile" else "PP-OCRv5_server_det",
    "text_recognition": "PP-OCRv5_mobile_rec" if PADDLE_OCR_PROFILE == "mobile" else "PP-OCRv5_server_rec",
}


def is_image_file(file_name: str) -> bool:
    return Path(file_name or "").suffix.lower() in IMAGE_SUFFIXES


def should_use_ocr_for_pdf(extracted_text: str, total_pages: int, non_empty_pages: int) -> bool:
    compact_text = re.sub(r"\s+", "", extracted_text or "")
    if not compact_text:
        return True

    readable_chars = len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", compact_text))
    if readable_chars < max(60, total_pages * 40):
        return True

    if total_pages > 0 and non_empty_pages <= max(1, total_pages // 2) and len(compact_text) < total_pages * 120:
        return True

    return False


def get_paddle_model_dirs() -> dict[str, Path]:
    return {
        key: PADDLE_MODEL_CACHE_DIR / model_name
        for key, model_name in PADDLE_MODEL_NAMES.items()
    }


def _is_model_dir_ready(model_dir: Path) -> bool:
    if not model_dir.exists() or not model_dir.is_dir():
        return False
    if not (model_dir / "config.json").exists():
        return False
    if not (model_dir / "inference.json").exists():
        return False
    if not (model_dir / "inference.yml").exists():
        return False
    if not any(model_dir.glob("*.pdiparams")):
        return False
    return True


def is_paddle_ocr_ready() -> bool:
    return all(_is_model_dir_ready(model_dir) for model_dir in get_paddle_model_dirs().values())


def get_paddle_ocr_status() -> dict:
    model_dirs = get_paddle_model_dirs()
    missing = [
        PADDLE_MODEL_NAMES[key]
        for key, model_dir in model_dirs.items()
        if not _is_model_dir_ready(model_dir)
    ]
    return {
        "ready": len(missing) == 0,
        "cache_dir": str(PADDLE_MODEL_CACHE_DIR),
        "missing_models": missing,
        "model_dirs": {key: str(path) for key, path in model_dirs.items()},
    }


def get_ocr_init_command() -> str:
    return f"{PYTHON_313_COMMAND} scripts/init_ocr.py"


def get_pip_install_command() -> str:
    return f"{PYTHON_313_COMMAND} -m pip install -r requirements.txt"


def get_streamlit_run_command() -> str:
    return f"{PYTHON_313_COMMAND} -m streamlit run app.py"


def is_required_python_version() -> bool:
    return sys.version_info[:2] >= REQUIRED_PYTHON_VERSION


def get_python_runtime_requirement_message() -> str:
    current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        f"当前运行环境是 Python {current_version}（{sys.executable}），"
        f"项目已统一要求 Python {REQUIRED_PYTHON_DISPLAY}。"
        f"请改用 `{get_streamlit_run_command()}` 启动应用，并使用 `{get_ocr_init_command()}` 初始化 OCR。"
    )


def get_paddle_ocr_not_ready_message() -> str:
    status = get_paddle_ocr_status()
    missing = "、".join(status["missing_models"]) if status["missing_models"] else "未知模型"
    return (
        f"OCR 尚未初始化，缺少模型：{missing}。"
        f"请先在项目根目录运行 `{get_ocr_init_command()}`，模型会预下载到 `{status['cache_dir']}`。"
    )


def _parse_paddleocr_version(version_text: str) -> tuple[int, ...]:
    parts = []
    for part in re.split(r"[.-]", version_text or ""):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)


def _load_paddleocr_class():
    try:
        import paddleocr
        from paddleocr import PaddleOCR
    except Exception as exc:
        raise RuntimeError(
            "OCR 功能依赖 PaddleOCR / PaddlePaddle，本地环境缺少该必需依赖。"
        ) from exc

    version_text = str(getattr(paddleocr, "__version__", "0"))
    version = _parse_paddleocr_version(version_text)
    if version and version < (3, 0, 0):
        raise RuntimeError(
            f"当前 PaddleOCR 版本为 {version_text}，低于项目要求的 3.x。"
            f"请使用与 Streamlit 相同的 Python 环境重新安装依赖，并运行 {get_ocr_init_command()}。"
        )

    return PaddleOCR, version_text


def _build_paddle_model_kwargs() -> dict:
    model_dirs = get_paddle_model_dirs()
    return {
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "doc_orientation_classify_model_name": PADDLE_MODEL_NAMES["doc_orientation"],
        "doc_orientation_classify_model_dir": str(model_dirs["doc_orientation"]),
        "text_detection_model_name": PADDLE_MODEL_NAMES["text_detection"],
        "text_detection_model_dir": str(model_dirs["text_detection"]),
        "text_recognition_model_name": PADDLE_MODEL_NAMES["text_recognition"],
        "text_recognition_model_dir": str(model_dirs["text_recognition"]),
    }


@lru_cache(maxsize=1)
def _get_paddle_ocr_engine():
    if not is_paddle_ocr_ready():
        raise RuntimeError(get_paddle_ocr_not_ready_message())

    PaddleOCR, _ = _load_paddleocr_class()
    kwargs_v3 = _build_paddle_model_kwargs()
    return PaddleOCR(**kwargs_v3)


def _extract_from_v3_result_item(result_item: object) -> str:
    payload = getattr(result_item, "json", None)
    if callable(payload):
        payload = payload()

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None

    if payload is None and isinstance(result_item, dict):
        payload = result_item

    if not isinstance(payload, dict):
        return ""

    if isinstance(payload.get("res"), dict):
        payload = payload["res"]

    texts = payload.get("rec_texts") or []
    if not isinstance(texts, list):
        return ""

    return "\n".join(str(text).strip() for text in texts if str(text).strip())


def _extract_from_v2_result(raw_result: object) -> str:
    page_texts: list[str] = []
    if not isinstance(raw_result, list):
        return ""

    for page in raw_result:
        if not isinstance(page, list):
            continue
        lines: list[str] = []
        for line in page:
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            rec_info = line[1]
            if isinstance(rec_info, (list, tuple)) and rec_info:
                text = str(rec_info[0]).strip()
                if text:
                    lines.append(text)
        if lines:
            page_texts.append("\n".join(lines))

    return "\n\n".join(page_texts).strip()


def extract_text_with_paddle(file_bytes: bytes, suffix: str) -> str:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            tmp_file.write(file_bytes)
            temp_path = Path(tmp_file.name)

        engine = _get_paddle_ocr_engine()

        if hasattr(engine, "predict"):
            page_texts: list[str] = []
            for result_item in engine.predict(str(temp_path)):
                text = _extract_from_v3_result_item(result_item)
                if text:
                    page_texts.append(text)
            return "\n\n".join(page_texts).strip()

        if hasattr(engine, "ocr"):
            raw_result = engine.ocr(str(temp_path), cls=True)
            return _extract_from_v2_result(raw_result)

        raise RuntimeError("当前 PaddleOCR 版本不支持可用的本地 OCR 调用接口。")
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def initialize_paddle_ocr() -> dict:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        raise RuntimeError("初始化 OCR 失败：缺少 Pillow 图像依赖。") from exc

    PaddleOCR, version_text = _load_paddleocr_class()

    init_kwargs_v3 = {
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "doc_orientation_classify_model_name": PADDLE_MODEL_NAMES["doc_orientation"],
        "text_detection_model_name": PADDLE_MODEL_NAMES["text_detection"],
        "text_recognition_model_name": PADDLE_MODEL_NAMES["text_recognition"],
    }

    engine = None
    first_exc: Exception | None = None
    try:
        engine = PaddleOCR(**init_kwargs_v3)
    except Exception as exc:
        first_exc = exc

    if engine is None and any(path.exists() for path in get_paddle_model_dirs().values()):
        try:
            engine = PaddleOCR(**_build_paddle_model_kwargs())
        except Exception as exc:
            first_exc = exc

    if engine is None:
        detail = str(first_exc).strip() if first_exc else "未知错误"
        raise RuntimeError(
            f"OCR 初始化失败：当前 PaddleOCR 版本为 {version_text}，"
            f"按 3.x PP-OCRv5 配置初始化未成功。底层错误：{detail}。"
            f"请先使用 `{get_pip_install_command()}` 重新安装依赖，再执行 `{get_ocr_init_command()}`。"
        ) from first_exc

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp_file:
            temp_path = Path(tmp_file.name)

        image = Image.new("RGB", (480, 120), color="white")
        drawer = ImageDraw.Draw(image)
        drawer.text((20, 40), "OCR INIT 123", fill="black")
        image.save(temp_path)

        if hasattr(engine, "predict"):
            for _ in engine.predict(str(temp_path)):
                break
        elif hasattr(engine, "ocr"):
            engine.ocr(str(temp_path), cls=True)
        else:
            raise RuntimeError("当前 PaddleOCR 版本不支持可用的初始化调用接口。")
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

    _get_paddle_ocr_engine.cache_clear()
    status = get_paddle_ocr_status()
    if not status["ready"]:
        raise RuntimeError(get_paddle_ocr_not_ready_message())
    return status
