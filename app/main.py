import asyncio
import base64
import hashlib
import hmac
import io
import json
import smtplib
import socket
import uuid
from contextlib import asynccontextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional

import qrcode
import socketio
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.quiz_engine import QuizEngine
from app.storage import (
    create_questions_backup_file,
    create_settings_backup_file,
    load_event_data,
    load_quiz_config,
    save_event_data,
    save_quiz_config,
)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"

SECRET_KEY = "leto-2026-local-secret"

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
engine = QuizEngine()


def sign_scope(scope: str, password: str) -> str:
    msg = f"{scope}:{password}".encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def is_authorized(request: Request, scope: str, password: str) -> bool:
    cookie_val = request.cookies.get(f"auth_{scope}", "")
    expected = sign_scope(scope, password)
    return bool(cookie_val) and hmac.compare_digest(cookie_val, expected)


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def make_qr_data_uri(url: str) -> str:
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


async def save_upload(file: Optional[UploadFile], subdir: str) -> str:
    if not file or not file.filename:
        return ""
    ext = Path(file.filename).suffix.lower()
    safe_name = f"{uuid.uuid4().hex}{ext}"
    target_dir = UPLOADS_DIR / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    content = await file.read()
    if not content:
        return ""
    target_path.write_bytes(content)
    return f"/static/uploads/{subdir}/{safe_name}"


def send_backup_via_email(backup_path: Path, email_cfg: dict, title_prefix: str = "Резервная копия") -> tuple[bool, str]:
    recipient = str(email_cfg.get("recipient_email", "")).strip()
    smtp_host = str(email_cfg.get("smtp_host", "")).strip()
    smtp_port = int(email_cfg.get("smtp_port", 465) or 465)
    smtp_user = str(email_cfg.get("smtp_user", "")).strip()
    smtp_pass = str(email_cfg.get("smtp_password", "")).strip()

    if not recipient or not smtp_host or not smtp_user or not smtp_pass:
        return False, "Не заполнены SMTP-настройки или email получателя"

    msg = EmailMessage()
    msg["Subject"] = f"{title_prefix} ЛЭТО-2026 ({backup_path.name})"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.set_content(f"Во вложении  файл {backup_path.name}.")
    msg.add_attachment(
        backup_path.read_bytes(),
        maintype="application",
        subtype="json",
        filename=backup_path.name,
    )

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        return True, f"Отправлено на {recipient}"
    except Exception as exc:
        return False, f"Ошибка отправки почты: {exc}"


async def broadcast_all():
    await sio.emit("broadcast_state", engine.build_broadcast_state(), room="broadcast_room")
    for team_id in engine.teams:
        await sio.emit("team_state", engine.build_team_state(team_id), room=team_id)


async def timer_watcher():
    while True:
        await asyncio.sleep(1)
        if engine.status == "question" and engine.question_ends_at:
            rem = engine.get_remaining_seconds()
            await sio.emit("timer_tick", {"remaining_seconds": rem})
            if rem <= 0:
                await engine.close_question_and_score()
                await broadcast_all()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(timer_watcher())
    yield
    task.cancel()


fastapi_app = FastAPI(title="ЛЭТО-2026", lifespan=lifespan)
fastapi_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def base_hub_context(request: Request, event_data: dict):
    quiz_cfg = load_quiz_config()
    local_ip = get_local_ip()
    port = request.url.port or 8000
    play_url = f"http://{local_ip}:{port}/play"
    return {
        "request": request,
        "event": event_data,
        "quiz": quiz_cfg,
        "ui": event_data.get("ui_custom", {}),
        "play_url": play_url,
        "qr_play": make_qr_data_uri(play_url),
    }


@fastapi_app.get("/", response_class=HTMLResponse)
def hub_page(request: Request):
    ev = load_event_data()
    return templates.TemplateResponse("hub.html", base_hub_context(request, ev))


@fastapi_app.get("/program", response_class=HTMLResponse)
def program_page(request: Request):
    ev = load_event_data()
    return templates.TemplateResponse("program.html", base_hub_context(request, ev))


@fastapi_app.get("/photos", response_class=HTMLResponse)
def photos_page(request: Request):
    ev = load_event_data()
    return templates.TemplateResponse("photos.html", base_hub_context(request, ev))


@fastapi_app.get("/partners", response_class=HTMLResponse)
def partners_page(request: Request):
    ev = load_event_data()
    return templates.TemplateResponse("partners.html", base_hub_context(request, ev))


@fastapi_app.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    ev = load_event_data()
    committees = {}
    for org in ev.get("organizers", []):
        c_name = org.get("committee", "Организаторы")
        committees.setdefault(c_name, []).append(org)
    ctx = base_hub_context(request, ev)
    ctx["committees"] = committees
    return templates.TemplateResponse("team.html", ctx)


@fastapi_app.post("/admin/team-member/save")
async def admin_team_member_save(
    request: Request,
    org_id: str = Form(...),
    name: str = Form(...),
    committee: str = Form("Организаторы"),
    role: str = Form(""),
    photo_file: Optional[UploadFile] = File(None),
):
    ev = load_event_data()
    if not is_authorized(request, "admin", str(ev.get("admin_password", "0000"))):
        return RedirectResponse(url="/admin", status_code=303)
    saved = await save_upload(photo_file, "team")
    for org in ev.get("organizers", []):
        if org.get("id") == org_id:
            if name.strip():
                org["name"] = name.strip()
            org["committee"] = committee.strip() or "Организаторы"
            org["role"] = role.strip()
            if saved:
                org["photo"] = saved
            break
    save_event_data(ev)
    return RedirectResponse(url="/admin?tab=team_photos&msg=Данные+участника+команды+сохранены", status_code=303)


@fastapi_app.post("/admin/team-member/add")
async def admin_team_member_add(
    request: Request,
    name: str = Form(...),
    committee: str = Form("Организаторы"),
    role: str = Form(""),
    photo_file: Optional[UploadFile] = File(None),
):
    ev = load_event_data()
    if not is_authorized(request, "admin", str(ev.get("admin_password", "0000"))):
        return RedirectResponse(url="/admin", status_code=303)
    saved = await save_upload(photo_file, "team")
    if name.strip():
        ev.setdefault("organizers", []).append({
            "id": f"org_{uuid.uuid4().hex[:8]}",
            "committee": committee.strip() or "Организаторы",
            "name": name.strip(),
            "role": role.strip(),
            "photo": saved or "/static/uploads/avatars/default_person.svg",
        })
        save_event_data(ev)
    return RedirectResponse(url="/admin?tab=team_photos&msg=Участник+команды+добавлен", status_code=303)


@fastapi_app.post("/admin/team-member/delete/{org_id}")
async def admin_team_member_delete(request: Request, org_id: str):
    ev = load_event_data()
    if not is_authorized(request, "admin", str(ev.get("admin_password", "0000"))):
        return RedirectResponse(url="/admin", status_code=303)
    ev["organizers"] = [x for x in ev.get("organizers", []) if x.get("id") != org_id]
    save_event_data(ev)
    return RedirectResponse(url="/admin?tab=team_photos&msg=Участник+команды+удалён", status_code=303)


@fastapi_app.post("/admin/team-member/photo")
async def admin_team_member_photo(
    request: Request,
    org_id: str = Form(...),
    photo_file: UploadFile = File(...),
):
    ev = load_event_data()
    if not is_authorized(request, "admin", str(ev.get("admin_password", "0000"))):
        return RedirectResponse(url="/admin", status_code=303)
    saved = await save_upload(photo_file, "team")
    if saved:
        for org in ev.get("organizers", []):
            if org.get("id") == org_id:
                org["photo"] = saved
                break
        save_event_data(ev)
    return RedirectResponse(url="/admin?tab=team_photos&msg=Фото+участника+команды+обновлено", status_code=303)


@fastapi_app.get("/org-info", response_class=HTMLResponse)
def org_info_page(request: Request, err: str = ""):
    ev = load_event_data()
    if not ev.get("org_info_enabled", True):
        return RedirectResponse(url="/team", status_code=303)
    authed = is_authorized(request, "org", str(ev.get("org_info_password", "16")))
    ctx = base_hub_context(request, ev)
    ctx["authed"] = authed
    ctx["err"] = err
    return templates.TemplateResponse("org_info.html", ctx)


@fastapi_app.post("/org-info/auth")
async def org_info_auth(password: str = Form(...)):
    ev = load_event_data()
    expected = str(ev.get("org_info_password", "16"))
    if password.strip() != expected:
        return RedirectResponse(url="/org-info?err=1", status_code=303)
    resp = RedirectResponse(url="/org-info", status_code=303)
    resp.set_cookie("auth_org", sign_scope("org", expected), httponly=True, samesite="lax")
    return resp


@fastapi_app.post("/org-info/add-announcement")
async def org_add_announcement(announcement: str = Form(...)):
    ev = load_event_data()
    if announcement.strip():
        ev.setdefault("org_announcements", []).append(announcement.strip())
        save_event_data(ev)
    return RedirectResponse(url="/org-info", status_code=303)


@fastapi_app.post("/org-info/add-link")
async def org_add_link(title: str = Form(...), url: str = Form(...)):
    ev = load_event_data()
    if title.strip() and url.strip():
        ev.setdefault("org_cloud_links", []).append({"title": title.strip(), "url": url.strip()})
        save_event_data(ev)
    return RedirectResponse(url="/org-info", status_code=303)


@fastapi_app.post("/org-info/add-file")
async def org_add_file(title: str = Form(""), upload_file: UploadFile = File(...)):
    ev = load_event_data()
    saved = await save_upload(upload_file, "org_files")
    if saved:
        ev.setdefault("org_files", []).append({
            "id": f"of_{uuid.uuid4().hex[:8]}",
            "title": title.strip() or upload_file.filename,
            "url": saved,
        })
        save_event_data(ev)
    return RedirectResponse(url="/org-info", status_code=303)


@fastapi_app.get("/host")
def legacy_host_redirect():
    return RedirectResponse(url="/broadcast", status_code=307)


@fastapi_app.get("/broadcast", response_class=HTMLResponse)
def broadcast_page(request: Request):
    local_ip = get_local_ip()
    port = request.url.port or 8000
    play_url = f"http://{local_ip}:{port}/play"
    return templates.TemplateResponse(
        "broadcast.html",
        {
            "request": request,
            "play_url": play_url,
            "qr_play": make_qr_data_uri(play_url),
        },
    )


@fastapi_app.get("/control", response_class=HTMLResponse)
def control_page(request: Request, err: str = ""):
    ev = load_event_data()
    ctrl_pwd = str(ev.get("control_password", "0000"))
    if not is_authorized(request, "control", ctrl_pwd):
        return templates.TemplateResponse("control_login.html", {"request": request, "err": err})
    return templates.TemplateResponse(
        "control.html",
        {"request": request, "quiz_ready_mode": bool(ev.get("quiz_ready_mode", False))},
    )


@fastapi_app.post("/control/login")
async def control_login(password: str = Form(...)):
    ev = load_event_data()
    expected = str(ev.get("control_password", "0000"))
    if password.strip() != expected:
        return RedirectResponse(url="/control?err=1", status_code=303)
    resp = RedirectResponse(url="/control", status_code=303)
    resp.set_cookie("auth_control", sign_scope("control", expected), httponly=True, samesite="lax")
    return resp


@fastapi_app.get("/play", response_class=HTMLResponse)
def play_page(request: Request, team: str = "", curator: str = ""):
    cap = engine.get_team_capacity()
    teams_list = [
        {
            "id": engine.teams[tid].id,
            "name": engine.teams[tid].name,
            "color": engine.teams[tid].color,
            "count": len(engine.teams[tid].members),
            "capacity": cap,
        }
        for tid in engine.active_team_ids()
    ]
    return templates.TemplateResponse(
        "play.html",
        {
            "request": request,
            "teams": teams_list,
            "preselected_team": team,
            "force_curator": (curator == "1"),
        },
    )


@fastapi_app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, edit_id: Optional[str] = None, err: str = "", msg: str = "", tab: str = "quiz"):
    event_data = load_event_data()
    admin_pwd = str(event_data.get("admin_password", "0000"))
    if not is_authorized(request, "admin", admin_pwd):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "err": err},
        )

    quiz_cfg = load_quiz_config()
    questions = quiz_cfg.get("questions", [])
    editing_question = None
    if edit_id:
        for q in questions:
            if q.get("id") == edit_id:
                editing_question = q
                break

    teams_snapshot = [
        {
            "id": t.id,
            "name": t.name,
            "color": t.color,
            "score": t.score,
            "submitted_option": t.submitted_option,
            "curator_change_allowed": t.curator_change_allowed,
            "members": [
                {"token": m.session_token, "nickname": m.nickname, "is_curator": m.is_curator}
                for m in t.members.values()
            ],
        }
        for t in engine.teams.values()
    ]
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "event": event_data,
            "quiz": quiz_cfg,
            "ui": event_data.get("ui_custom", {}),
            "email_cfg": event_data.get("email_backup", {}),
            "editing_question": editing_question,
            "teams": teams_snapshot,
            "msg": msg,
            "active_tab": tab,
        },
    )


@fastapi_app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    ev = load_event_data()
    expected = str(ev.get("admin_password", "0000"))
    if password.strip() != expected:
        return RedirectResponse(url="/admin?err=1", status_code=303)
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("auth_admin", sign_scope("admin", expected), httponly=True, samesite="lax")
    return resp


@fastapi_app.post("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.delete_cookie("auth_admin")
    return resp


@fastapi_app.post("/admin/quiz/settings")
async def admin_quiz_settings(
    quiz_title: str = Form(...),
    team_capacity: int = Form(40),
    organizers_enabled: Optional[str] = Form(None),
    organizers_off_podium: Optional[str] = Form(None),
    stepwise_podium_enabled: Optional[str] = Form(None),
    auto_random_unanswered: Optional[str] = Form(None),
    sponsor_enabled: Optional[str] = Form(None),
    sponsor_title: str = Form(""),
    sponsor_description: str = Form(""),
    sponsor_prizes: str = Form(""),
    place_prizes_text: str = Form(""),
    remove_sponsor_image: Optional[str] = Form(None),
    remove_logo: Optional[str] = Form(None),
    logo_file: Optional[UploadFile] = File(None),
    sponsor_image_file: Optional[UploadFile] = File(None),
):
    cfg = load_quiz_config()
    cfg["quiz_title"] = quiz_title.strip() or "КВИЗ"
    cfg["team_capacity"] = max(1, int(team_capacity))
    cfg["organizers_enabled"] = (organizers_enabled == "on")
    cfg["organizers_off_podium"] = (organizers_off_podium == "on")
    cfg["stepwise_podium_enabled"] = (stepwise_podium_enabled == "on")
    cfg["auto_random_unanswered"] = (auto_random_unanswered == "on")
    cfg["sponsor_enabled"] = (sponsor_enabled == "on")
    cfg["sponsor_title"] = sponsor_title.strip()
    cfg["sponsor_description"] = sponsor_description.strip()
    cfg["sponsor_prizes"] = sponsor_prizes.strip()

    parsed_prizes = {}
    for line in place_prizes_text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if k.isdigit():
            parsed_prizes[k] = v.strip()
    if parsed_prizes:
        cfg["place_prizes"] = parsed_prizes

    if remove_logo == "on":
        cfg["logo_url"] = ""
    new_logo = await save_upload(logo_file, "questions")
    if new_logo:
        cfg["logo_url"] = new_logo

    if remove_sponsor_image == "on":
        cfg["sponsor_image_url"] = ""
    new_sp_img = await save_upload(sponsor_image_file, "questions")
    if new_sp_img:
        cfg["sponsor_image_url"] = new_sp_img

    save_quiz_config(cfg)
    engine.sync_teams_from_config()
    await broadcast_all()
    return RedirectResponse(url="/admin?tab=quiz&msg=Настройки+квиза+сохранены", status_code=303)


@fastapi_app.post("/admin/teams/update")
async def admin_teams_update(
    team_ids: List[str] = Form(...),
    team_names: List[str] = Form(...),
    team_colors: List[str] = Form(...),
    team_org_flags: Optional[List[str]] = Form(None),
    new_team_name: str = Form(""),
    new_team_color: str = Form("#9333ea"),
):
    cfg = load_quiz_config()
    org_set = set(team_org_flags or [])
    updated_list = []
    for tid, tname, tcolor in zip(team_ids, team_names, team_colors):
        tid = tid.strip()
        if not tid:
            continue
        updated_list.append({
            "id": tid,
            "name": tname.strip() or tid,
            "color": tcolor.strip() or "#2563eb",
            "is_organizers": (tid in org_set),
        })

    if new_team_name.strip():
        updated_list.append({
            "id": f"team_{uuid.uuid4().hex[:6]}",
            "name": new_team_name.strip(),
            "color": new_team_color.strip() or "#9333ea",
            "is_organizers": False,
        })

    if updated_list:
        cfg["teams_config"] = updated_list
        save_quiz_config(cfg)
        engine.sync_teams_from_config()
        await broadcast_all()

    return RedirectResponse(url="/admin?tab=teams&msg=Команды+сохранены", status_code=303)


@fastapi_app.post("/admin/teams/delete/{tid}")
async def admin_team_delete(tid: str):
    cfg = load_quiz_config()
    items = [t for t in cfg.get("teams_config", []) if t.get("id") != tid]
    if items:
        cfg["teams_config"] = items
        save_quiz_config(cfg)
        engine.sync_teams_from_config()
        await broadcast_all()
    return RedirectResponse(url="/admin?tab=teams&msg=Команда+удалена", status_code=303)


def _parse_options_from_form(keys: List[str], texts: List[str]) -> List[dict]:
    result = []
    for idx, (k, txt) in enumerate(zip(keys, texts)):
        t = txt.strip()
        if not t:
            continue
        key = (k or "").strip() or chr(65 + idx)
        result.append({"key": key, "text": t})
    return result


@fastapi_app.post("/admin/question/add")
async def admin_add_question(
    question_type: str = Form("choice"),
    text: str = Form(...),
    opt_keys: Optional[List[str]] = Form(None),
    opt_texts: Optional[List[str]] = Form(None),
    correct_option: str = Form(""),
    reference_answer: str = Form(""),
    time_limit: int = Form(30),
    points: int = Form(100),
    partial_points: int = Form(50),
    image_file: Optional[UploadFile] = File(None),
    audio_file: Optional[UploadFile] = File(None),
):
    cfg = load_quiz_config()
    img_url = await save_upload(image_file, "questions")
    aud_url = await save_upload(audio_file, "questions")
    q_type = "open" if question_type == "open" else "choice"
    opts = _parse_options_from_form(opt_keys or [], opt_texts or []) if q_type == "choice" else []
    if q_type == "choice" and not opts:
        opts = [{"key": "A", "text": "Вариант A"}, {"key": "B", "text": "Вариант B"}]
    valid_keys = [o["key"] for o in opts]
    corr = correct_option.strip() if correct_option.strip() in valid_keys else (valid_keys[0] if valid_keys else "")

    new_q = {
        "id": f"q_{uuid.uuid4().hex[:8]}",
        "question_type": q_type,
        "text": text.strip(),
        "options": opts,
        "correct_option": corr if q_type == "choice" else "",
        "reference_answer": reference_answer.strip() if q_type == "open" else "",
        "time_limit": max(5, int(time_limit)),
        "points": max(10, int(points)),
        "partial_points": max(0, int(partial_points)),
        "image_url": img_url,
        "audio_url": aud_url,
    }
    cfg.setdefault("questions", []).append(new_q)
    save_quiz_config(cfg)
    return RedirectResponse(url="/admin?tab=questions&msg=Вопрос+добавлен", status_code=303)


@fastapi_app.post("/admin/question/edit/{q_id}")
async def admin_edit_question(
    q_id: str,
    question_type: str = Form("choice"),
    text: str = Form(...),
    opt_keys: Optional[List[str]] = Form(None),
    opt_texts: Optional[List[str]] = Form(None),
    correct_option: str = Form(""),
    reference_answer: str = Form(""),
    time_limit: int = Form(30),
    points: int = Form(100),
    partial_points: int = Form(50),
    remove_image: Optional[str] = Form(None),
    remove_audio: Optional[str] = Form(None),
    image_file: Optional[UploadFile] = File(None),
    audio_file: Optional[UploadFile] = File(None),
):
    cfg = load_quiz_config()
    for q in cfg.get("questions", []):
        if q.get("id") == q_id:
            q_type = "open" if question_type == "open" else "choice"
            opts = _parse_options_from_form(opt_keys or [], opt_texts or []) if q_type == "choice" else []
            if q_type == "choice" and not opts:
                opts = [{"key": "A", "text": "Вариант A"}, {"key": "B", "text": "Вариант B"}]
            valid_keys = [o["key"] for o in opts]
            corr = correct_option.strip() if correct_option.strip() in valid_keys else (valid_keys[0] if valid_keys else "")

            q["question_type"] = q_type
            q["text"] = text.strip()
            q["options"] = opts
            q["correct_option"] = corr if q_type == "choice" else ""
            q["reference_answer"] = reference_answer.strip() if q_type == "open" else ""
            q["time_limit"] = max(5, int(time_limit))
            q["points"] = max(10, int(points))
            q["partial_points"] = max(0, int(partial_points))

            if remove_image == "on":
                q["image_url"] = ""
            new_img = await save_upload(image_file, "questions")
            if new_img:
                q["image_url"] = new_img

            if remove_audio == "on":
                q["audio_url"] = ""
            new_aud = await save_upload(audio_file, "questions")
            if new_aud:
                q["audio_url"] = new_aud
            break

    save_quiz_config(cfg)
    if engine.status in ("question", "reveal"):
        await broadcast_all()
    return RedirectResponse(url="/admin?tab=questions&msg=Вопрос+обновлён", status_code=303)


@fastapi_app.post("/admin/question/delete/{q_id}")
async def admin_delete_question(q_id: str):
    cfg = load_quiz_config()
    cfg["questions"] = [q for q in cfg.get("questions", []) if q.get("id") != q_id]
    save_quiz_config(cfg)
    return RedirectResponse(url="/admin?tab=questions&msg=Вопрос+удалён", status_code=303)


@fastapi_app.post("/admin/music")
async def admin_upload_music(
    music_name: str = Form("Фоновая музыка для обсуждения"),
    music_file: UploadFile = File(...),
):
    saved_url = await save_upload(music_file, "music")
    if saved_url:
        cfg = load_quiz_config()
        cfg["thinking_music_url"] = saved_url
        cfg["thinking_music_name"] = music_name.strip() or music_file.filename
        save_quiz_config(cfg)
        await broadcast_all()
    return RedirectResponse(url="/admin?tab=music&msg=Трек+загружен", status_code=303)


@fastapi_app.post("/admin/curator/assign")
async def admin_assign_curator(team_id: str = Form(...), session_token: str = Form(...)):
    await engine.set_curator(team_id, session_token)
    await broadcast_all()
    return RedirectResponse(url="/admin?tab=curators&msg=Куратор+назначен", status_code=303)


@fastapi_app.post("/admin/event/meta")
async def admin_update_event_meta(
    event_title: str = Form(...),
    theme: str = Form(...),
    dates: str = Form(...),
    time_range: str = Form(""),
    location: str = Form(...),
    vk_group: str = Form(...),
    curator_chat_url: str = Form(...),
    curator_chat_label: str = Form(...),
    about_text: str = Form(""),
    welcome_media_type: str = Form("none"),
    welcome_video_url: str = Form(""),
    welcome_video_file: Optional[UploadFile] = File(None),
    slideshow_files: Optional[List[UploadFile]] = File(None),
    clear_slideshow: Optional[str] = Form(None),
    quiz_ready_mode: Optional[str] = Form(None),
    quiz_ready_title: str = Form("Квиз начинается!"),
    quiz_ready_subtitle: str = Form(""),
    admin_password: str = Form("0000"),
    control_password: str = Form("0000"),
    org_info_enabled: Optional[str] = Form(None),
    org_info_password: str = Form("16"),
    org_info_title: str = Form("Я организатор  рабочие материалы"),
    bg_color: str = Form("#f4f5f7"),
    card_color: str = Form("#ffffff"),
    text_color: str = Form("#181a1f"),
    accent_color: str = Form("#2563eb"),
    program_title: str = Form("Программа мероприятия"),
    partners_title: str = Form("Наши партнёры"),
    photos_title: str = Form("Фото с мероприятия"),
    team_title: str = Form("Наша команда"),
):
    ev = load_event_data()
    ev["event_title"] = event_title.strip()
    ev["theme"] = theme.strip()
    ev["dates"] = dates.strip()
    ev["time_range"] = time_range.strip()
    ev["location"] = location.strip()
    ev["vk_group"] = vk_group.strip()
    ev["curator_chat_url"] = curator_chat_url.strip()
    ev["curator_chat_label"] = curator_chat_label.strip()
    paragraphs = [p.strip() for p in about_text.splitlines() if p.strip()]
    if paragraphs:
        ev["about_paragraphs"] = paragraphs

    ev["welcome_media_type"] = welcome_media_type if welcome_media_type in ("none", "video", "slideshow") else "none"
    uploaded_vid = await save_upload(welcome_video_file, "video")
    if uploaded_vid:
        ev["welcome_video_url"] = uploaded_vid
    else:
        ev["welcome_video_url"] = welcome_video_url.strip()

    if clear_slideshow == "on":
        ev["slideshow_images"] = []
    if slideshow_files:
        for sf in slideshow_files:
            s_url = await save_upload(sf, "slideshow")
            if s_url:
                ev.setdefault("slideshow_images", []).append(s_url)

    ev["quiz_ready_mode"] = (quiz_ready_mode == "on")
    ev["quiz_ready_title"] = quiz_ready_title.strip() or "Квиз начинается!"
    ev["quiz_ready_subtitle"] = quiz_ready_subtitle.strip()

    if admin_password.strip():
        ev["admin_password"] = admin_password.strip()
    if control_password.strip():
        ev["control_password"] = control_password.strip()
    ev["org_info_enabled"] = (org_info_enabled == "on")
    if org_info_password.strip():
        ev["org_info_password"] = org_info_password.strip()
    ev["org_info_title"] = org_info_title.strip() or "Я организатор  рабочие материалы"

    ev["ui_custom"] = {
        "bg_color": bg_color.strip() or "#f4f5f7",
        "card_color": card_color.strip() or "#ffffff",
        "text_color": text_color.strip() or "#181a1f",
        "accent_color": accent_color.strip() or "#2563eb",
        "program_title": program_title.strip() or "Программа мероприятия",
        "partners_title": partners_title.strip() or "Наши партнёры",
        "photos_title": photos_title.strip() or "Фото с мероприятия",
        "team_title": team_title.strip() or "Наша команда",
    }

    save_event_data(ev)
    resp = RedirectResponse(url="/admin?tab=pages&msg=Настройки+страниц+и+пароли+сохранены", status_code=303)
    resp.set_cookie("auth_admin", sign_scope("admin", ev["admin_password"]), httponly=True, samesite="lax")
    resp.set_cookie("auth_control", sign_scope("control", ev["control_password"]), httponly=True, samesite="lax")
    return resp


@fastapi_app.post("/admin/photo/add")
async def admin_add_photo(
    title: str = Form(...),
    photo_url: str = Form(""),
    photo_file: Optional[UploadFile] = File(None),
):
    ev = load_event_data()
    saved = await save_upload(photo_file, "photos")
    final_url = saved or photo_url.strip()
    if final_url:
        ev.setdefault("photos", []).append({
            "id": f"ph_{uuid.uuid4().hex[:8]}",
            "title": title.strip() or "Фото с мероприятия",
            "url": final_url,
        })
        save_event_data(ev)
    return RedirectResponse(url="/admin?tab=pages&msg=Фото+добавлено", status_code=303)


@fastapi_app.post("/admin/photo/delete/{ph_id}")
async def admin_delete_photo(ph_id: str):
    ev = load_event_data()
    ev["photos"] = [x for x in ev.get("photos", []) if x.get("id") != ph_id]
    save_event_data(ev)
    return RedirectResponse(url="/admin?tab=pages&msg=Фото+удалено", status_code=303)


@fastapi_app.post("/admin/partner/add")
async def admin_add_partner(
    name: str = Form(...),
    description: str = Form(""),
    url: str = Form(""),
):
    ev = load_event_data()
    ev.setdefault("partners", []).append({
        "id": f"pt_{uuid.uuid4().hex[:8]}",
        "name": name.strip(),
        "description": description.strip(),
        "url": url.strip(),
    })
    save_event_data(ev)
    return RedirectResponse(url="/admin?tab=pages&msg=Партнёр+добавлен", status_code=303)


@fastapi_app.post("/admin/partner/delete/{pt_id}")
async def admin_delete_partner(pt_id: str):
    ev = load_event_data()
    ev["partners"] = [x for x in ev.get("partners", []) if x.get("id") != pt_id]
    save_event_data(ev)
    return RedirectResponse(url="/admin?tab=pages&msg=Партнёр+удалён", status_code=303)


@fastapi_app.post("/admin/backup/save-settings")
async def admin_backup_save_settings(
    recipient_email: str = Form(""),
    smtp_host: str = Form("smtp.yandex.ru"),
    smtp_port: int = Form(465),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    send_email_flag: Optional[str] = Form(None),
):
    ev = load_event_data()
    ev["email_backup"] = {
        "recipient_email": recipient_email.strip(),
        "smtp_host": smtp_host.strip(),
        "smtp_port": int(smtp_port or 465),
        "smtp_user": smtp_user.strip(),
        "smtp_password": smtp_password.strip(),
    }
    save_event_data(ev)
    backup_path = create_settings_backup_file()
    status_text = f"Настройки сохранены в backups/{backup_path.name}."
    if send_email_flag == "on":
        ok, mail_msg = send_backup_via_email(backup_path, ev["email_backup"], "Бэкап настроек")
        status_text += f" Почта: {mail_msg}"
    return RedirectResponse(url=f"/admin?tab=backup&msg={status_text}", status_code=303)


@fastapi_app.post("/admin/backup/save-questions")
async def admin_backup_save_questions(
    recipient_email: str = Form(""),
    smtp_host: str = Form("smtp.yandex.ru"),
    smtp_port: int = Form(465),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    send_email_flag: Optional[str] = Form(None),
):
    ev = load_event_data()
    ev["email_backup"] = {
        "recipient_email": recipient_email.strip(),
        "smtp_host": smtp_host.strip(),
        "smtp_port": int(smtp_port or 465),
        "smtp_user": smtp_user.strip(),
        "smtp_password": smtp_password.strip(),
    }
    save_event_data(ev)
    backup_path = create_questions_backup_file()
    status_text = f"Пакет вопросов сохранён в backups/{backup_path.name}."
    if send_email_flag == "on":
        ok, mail_msg = send_backup_via_email(backup_path, ev["email_backup"], "Пакет вопросов")
        status_text += f" Почта: {mail_msg}"
    return RedirectResponse(url=f"/admin?tab=backup&msg={status_text}", status_code=303)


@fastapi_app.get("/admin/backup/download-settings")
def admin_backup_download_settings():
    backup_path = create_settings_backup_file()
    return FileResponse(path=str(backup_path), filename=backup_path.name, media_type="application/json")


@fastapi_app.get("/admin/backup/download-questions")
def admin_backup_download_questions():
    backup_path = create_questions_backup_file()
    return FileResponse(path=str(backup_path), filename=backup_path.name, media_type="application/json")


@fastapi_app.post("/admin/backup/upload-settings")
async def admin_backup_upload_settings(settings_file: UploadFile = File(...)):
    raw = await settings_file.read()
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if "event_data" in parsed and isinstance(parsed["event_data"], dict):
            save_event_data(parsed["event_data"])
        if "quiz_settings" in parsed and isinstance(parsed["quiz_settings"], dict):
            qcfg = load_quiz_config()
            for k, v in parsed["quiz_settings"].items():
                if k != "questions":
                    qcfg[k] = v
            save_quiz_config(qcfg)
        engine.sync_teams_from_config()
        await broadcast_all()
        msg = "Файл настроек успешно загружен."
    except Exception as exc:
        msg = f"Ошибка чтения файла настроек: {exc}"
    return RedirectResponse(url=f"/admin?tab=backup&msg={msg}", status_code=303)


@fastapi_app.post("/admin/backup/upload-questions")
async def admin_backup_upload_questions(questions_file: UploadFile = File(...)):
    raw = await questions_file.read()
    try:
        parsed = json.loads(raw.decode("utf-8"))
        qs = parsed.get("questions") if isinstance(parsed, dict) else parsed
        if not isinstance(qs, list):
            raise ValueError("Ожидался список вопросов")
        qcfg = load_quiz_config()
        qcfg["questions"] = qs
        save_quiz_config(qcfg)
        await broadcast_all()
        msg = "Пакет вопросов успешно загружен."
    except Exception as exc:
        msg = f"Ошибка чтения пакета вопросов: {exc}"
    return RedirectResponse(url=f"/admin?tab=backup&msg={msg}", status_code=303)


@sio.event
async def broadcast_join(sid, data=None):
    await sio.enter_room(sid, "broadcast_room")
    await sio.emit("broadcast_state", engine.build_broadcast_state(), to=sid)


@sio.event
async def player_join(sid, data):
    session_token = str(data.get("session_token", "")).strip()
    nickname = str(data.get("nickname", "")).strip()
    team_id = str(data.get("team_id", "")).strip()
    force_curator = bool(data.get("force_curator", False))

    res = await engine.join_team(session_token, nickname, team_id, sid, force_curator=force_curator)
    if not res["ok"]:
        await sio.emit("join_error", {"error": res["error"]}, to=sid)
        return

    for old_tid in engine.teams:
        await sio.leave_room(sid, old_tid)
    await sio.enter_room(sid, team_id)

    player = res["player"]
    await sio.emit(
        "joined",
        {
            "session_token": player.session_token,
            "nickname": player.nickname,
            "team_id": player.team_id,
            "is_curator": player.is_curator,
        },
        to=sid,
    )
    await broadcast_all()


@sio.event
async def player_vote(sid, data):
    session_token = str(data.get("session_token", "")).strip()
    option_key = str(data.get("option", "")).strip()
    res = await engine.cast_vote(session_token, option_key)
    if not res["ok"]:
        await sio.emit("action_error", {"error": res["error"]}, to=sid)
        return
    await broadcast_all()


@sio.event
async def curator_submit_answer(sid, data):
    session_token = str(data.get("session_token", "")).strip()
    option_key = data.get("option")
    answer_text = data.get("answer_text")
    res = await engine.curator_submit(session_token, option_key, answer_text=answer_text)
    if not res["ok"]:
        await sio.emit("action_error", {"error": res["error"]}, to=sid)
        return
    await broadcast_all()


@sio.event
async def control_grade_open(sid, data=None):
    d = data or {}
    team_id = str(d.get("team_id", "")).strip()
    verdict = str(d.get("verdict", "")).strip()
    await engine.grade_open_answer(team_id, verdict)
    await broadcast_all()


@sio.event
async def control_allow_curator_change(sid, data=None):
    team_id = str((data or {}).get("team_id", "")).strip()
    await engine.allow_curator_change(team_id)
    await broadcast_all()


@sio.event
async def control_override_team_result(sid, data=None):
    d = data or {}
    team_id = str(d.get("team_id", "")).strip()
    new_option = str(d.get("option", "")).strip()
    new_text = d.get("answer_text")
    new_score = int(d.get("score", 0))
    question_id = str(d.get("question_id", "")).strip() or None
    await engine.override_team_result(
        team_id,
        new_option,
        new_score,
        question_id=question_id,
        new_text=new_text,
    )
    await broadcast_all()


@sio.event
async def control_set_quiz_ready(sid, data=None):
    ready = bool((data or {}).get("ready", True))
    ev = load_event_data()
    ev["quiz_ready_mode"] = ready
    save_event_data(ev)
    await sio.emit("quiz_ready_updated", {"quiz_ready_mode": ready})


@sio.event
async def control_start_quiz(sid, data=None):
    await engine.start_quiz()
    await broadcast_all()


@sio.event
async def control_next_question(sid, data=None):
    await engine.start_next_question()
    await broadcast_all()


@sio.event
async def control_close_question(sid, data=None):
    await engine.close_question_and_score()
    await broadcast_all()


@sio.event
async def control_finish_quiz(sid, data=None):
    await engine.finish_quiz()
    await broadcast_all()


@sio.event
async def control_next_podium_step(sid, data=None):
    await engine.reveal_next_podium_step()
    await broadcast_all()


@sio.event
async def control_show_full_podium(sid, data=None):
    await engine.show_full_podium_now()
    await broadcast_all()


@sio.event
async def control_reset_game(sid, data=None):
    await engine.reset_game()
    await broadcast_all()


app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
