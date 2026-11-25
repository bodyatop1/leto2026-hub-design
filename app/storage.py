import json
from datetime import datetime
from pathlib import Path
from app.seed_data import get_default_event_data, get_default_quiz_config

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
BACKUPS_DIR = BASE_DIR / "backups"

EVENT_FILE = DATA_DIR / "event_data.json"
QUIZ_FILE = DATA_DIR / "quiz_config.json"


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)


def load_event_data() -> dict:
    _ensure_dirs()
    defaults = get_default_event_data()
    if not EVENT_FILE.exists():
        save_event_data(defaults)
        return defaults
    try:
        with EVENT_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return defaults
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return defaults


def save_event_data(data: dict) -> None:
    _ensure_dirs()
    with EVENT_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_question(q: dict) -> dict:
    q_type = q.get("question_type") or ("open" if q.get("is_open_answer") else "choice")
    q_type = "open" if q_type == "open" else "choice"
    raw_opts = q.get("options")
    options = []
    if isinstance(raw_opts, dict):
        for k in sorted(raw_opts.keys()):
            options.append({"key": str(k), "text": str(raw_opts[k])})
    elif isinstance(raw_opts, list):
        for idx, item in enumerate(raw_opts):
            if isinstance(item, dict):
                key = str(item.get("key") or chr(65 + idx))
                txt = str(item.get("text", ""))
                options.append({"key": key, "text": txt})
            else:
                options.append({"key": chr(65 + idx), "text": str(item)})
    if q_type == "choice" and not options:
        options = [
            {"key": "A", "text": "Вариант A"},
            {"key": "B", "text": "Вариант B"},
        ]
    points = int(q.get("points", 100) or 100)
    partial_points = int(q.get("partial_points", max(10, points // 2)) or 0)
    correct_opt = str(q.get("correct_option", ""))
    if q_type == "choice" and options and correct_opt not in [o["key"] for o in options]:
        correct_opt = options[0]["key"]
    return {
        "id": str(q.get("id", "")),
        "question_type": q_type,
        "text": str(q.get("text", "")),
        "options": options,
        "correct_option": correct_opt,
        "reference_answer": str(q.get("reference_answer", q.get("correct_answer", ""))),
        "time_limit": max(5, int(q.get("time_limit", 30) or 30)),
        "points": max(10, points),
        "partial_points": max(0, partial_points),
        "image_url": str(q.get("image_url", "")),
        "audio_url": str(q.get("audio_url", "")),
    }


def load_quiz_config() -> dict:
    _ensure_dirs()
    defaults = get_default_quiz_config()
    if not QUIZ_FILE.exists():
        save_quiz_config(defaults)
        return load_quiz_config()
    try:
        with QUIZ_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = dict(defaults)
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        data["questions"] = [_normalize_question(q) for q in data.get("questions", []) if isinstance(q, dict)]
        return data
    except Exception:
        return defaults


def save_quiz_config(data: dict) -> None:
    _ensure_dirs()
    payload = dict(data)
    payload["questions"] = [_normalize_question(q) for q in payload.get("questions", []) if isinstance(q, dict)]
    with QUIZ_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def build_settings_backup_payload() -> dict:
    ev = load_event_data()
    qcfg = load_quiz_config()
    quiz_without_questions = {k: v for k, v in qcfg.items() if k != "questions"}
    return {
        "backup_kind": "settings_and_pages",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "event_data": ev,
        "quiz_settings": quiz_without_questions,
    }


def build_questions_backup_payload() -> dict:
    qcfg = load_quiz_config()
    return {
        "backup_kind": "questions_pack",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "questions": qcfg.get("questions", []),
    }


def create_settings_backup_file() -> Path:
    _ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUPS_DIR / f"settings_backup_{ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(build_settings_backup_payload(), f, ensure_ascii=False, indent=2)
    return path


def create_questions_backup_file() -> Path:
    _ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUPS_DIR / f"questions_pack_{ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(build_questions_backup_payload(), f, ensure_ascii=False, indent=2)
    return path
