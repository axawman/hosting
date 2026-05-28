from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import os
import shutil
import zipfile

from app.db import (
    AdminSettings,
    Project,
    User,
    UserSession,
    get_db,
    hash_password,
    hash_session_token,
    init_db,
    make_session_token,
    normalize_email,
    verify_password,
)
from app.docker_manager import (
    ping_docker, 
    create_student_container, 
    get_student_containers, 
    delete_student_container,
    set_student_container_state,
    is_subdomain_available
)

app = FastAPI(title="Студенческий Хостинг")
templates = Jinja2Templates(directory="app/templates")
SESSION_COOKIE = "hosting_session"


@app.on_event("startup")
async def startup():
    init_db()


def redirect_home(error: str = None, auth: str = None):
    params = []
    if error:
        params.append(f"error={quote(error)}")
    if auth:
        params.append(f"auth={quote(auth)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"/{suffix}", status_code=303)


def current_user_from_request(request: Request, db: Session):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None

    session = db.query(UserSession).filter(UserSession.token_hash == hash_session_token(token)).first()
    if not session:
        return None

    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at < datetime.now(timezone.utc):
        db.delete(session)
        db.commit()
        return None

    return session.user


def admin_required(request: Request, db: Session):
    user = current_user_from_request(request, db)
    if not user:
        return None, redirect_home("Сначала войдите в аккаунт", "login")
    if not user.is_admin:
        return None, redirect_home("Недостаточно прав")
    return user, None


def get_admin_settings(db: Session):
    settings = db.query(AdminSettings).filter(AdminSettings.id == 1).first()
    if settings:
        return settings

    settings = AdminSettings(id=1)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def effective_limits(user: User, settings: AdminSettings):
    return {
        "max_projects": user.max_projects or settings.default_max_projects,
        "disk_limit_mb": user.disk_limit_mb or settings.default_disk_limit_mb,
        "memory_limit_mb": user.memory_limit_mb or settings.default_memory_limit_mb,
    }


def empty_to_none(value: str, minimum: int = 1):
    value = value.strip()
    if not value:
        return None
    return max(int(value), minimum)


def get_zip_size_mb(zip_path: str):
    total_bytes = 0
    with zipfile.ZipFile(zip_path, "r") as archive:
        for item in archive.infolist():
            total_bytes += item.file_size
    return max((total_bytes + 1024 * 1024 - 1) // (1024 * 1024), 1)


@app.get("/")
async def read_root(
    request: Request,
    error: str = None,
    auth: str = None,
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    docker_status = ping_docker() if user and user.is_admin else None
    settings = get_admin_settings(db) if user else None
    users = []
    owned_projects = []
    active_projects = []

    if user:
        project_query = db.query(Project)
        if not user.is_admin:
            project_query = project_query.filter(Project.owner_id == user.id)
        owned_projects = project_query.order_by(Project.created_at.desc()).all()
        docker_projects = {
            project["name"]: project
            for project in get_student_containers([project.name for project in owned_projects])
        }
        active_projects = [
            {
                "id": project.id,
                "name": project.name,
                "status": docker_projects.get(project.name, {}).get("status", "not found"),
                "container_id": project.container_id or docker_projects.get(project.name, {}).get("id", ""),
                "owner_email": project.owner.email,
                "disk_used_mb": project.disk_used_mb,
                "memory_limit_mb": project.memory_limit_mb,
            }
            for project in owned_projects
        ]

        if user.is_admin:
            users = [
                {
                    "id": account.id,
                    "email": account.email,
                    "is_admin": account.is_admin,
                    "max_projects": account.max_projects,
                    "disk_limit_mb": account.disk_limit_mb,
                    "memory_limit_mb": account.memory_limit_mb,
                    "project_count": db.query(Project).filter(Project.owner_id == account.id).count(),
                }
                for account in db.query(User).order_by(User.created_at.desc()).all()
            ]
    
    return templates.TemplateResponse(
        request=request,
        name="index.html", 
        context={
            "title": "Панель управления хостингом",
            "docker_status": docker_status,
            "projects": active_projects,
            "user": user,
            "users": users,
            "settings": settings,
            "limits": effective_limits(user, settings) if user and settings else None,
            "error": error,
            "auth": auth,
        }
    )


@app.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = normalize_email(email)
    if len(password) < 8:
        return redirect_home("Пароль должен быть не короче 8 символов", "register")

    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        return redirect_home("Пользователь с такой почтой уже существует", "register")

    user = User(email=email, password_hash=hash_password(password), is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)

    token = make_session_token(db, user)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if not user or not verify_password(password, user.password_hash):
        return redirect_home("Неверная почта или пароль", "login")

    token = make_session_token(db, user)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = db.query(UserSession).filter(UserSession.token_hash == hash_session_token(token)).first()
        if session:
            db.delete(session)
            db.commit()

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response

@app.post("/deploy")
async def deploy_project(
    request: Request, 
    subdomain: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    if not user:
        return redirect_home("Сначала войдите в аккаунт", "login")

    subdomain = subdomain.lower().strip()
    
    # 1. Проверки названия проекта
    if not subdomain.replace("-", "").isalnum():
        return redirect_home("Имя проекта может содержать только латинские буквы, цифры и дефис")
        
    if db.query(Project).filter(Project.name == subdomain).first():
        return redirect_home("Проект с таким именем уже существует")

    if not is_subdomain_available(subdomain):
        return redirect_home("Проект с таким именем уже существует")
        
    if not file.filename.endswith('.zip'):
        return redirect_home("Загрузите файл в формате ZIP")

    settings = get_admin_settings(db)
    limits = effective_limits(user, settings)
    project_count = db.query(Project).filter(Project.owner_id == user.id).count()
    if not user.is_admin and project_count >= limits["max_projects"]:
        return redirect_home(f"Достигнут лимит проектов: {limits['max_projects']}")

    # 2. Сохранение файла
    os.makedirs("/tmp/uploads", exist_ok=True)
    zip_path = f"/tmp/uploads/{subdomain}.zip"
    
    try:
        with open(zip_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        try:
            disk_used_mb = get_zip_size_mb(zip_path)
        except zipfile.BadZipFile:
            return redirect_home("Загрузите корректный ZIP-архив")

        if not user.is_admin and disk_used_mb > limits["disk_limit_mb"]:
            return redirect_home(f"Архив занимает {disk_used_mb} МБ после распаковки. Лимит: {limits['disk_limit_mb']} МБ")
            
        # 3. Деплой (с валидацией внутри)
        result = create_student_container(subdomain, zip_path, limits["memory_limit_mb"])
    finally:
        # 4. Гарантированно удаляем ZIP с сервера после работы
        if os.path.exists(zip_path):
            os.remove(zip_path)
    
    if result["status"] == "error":
        return redirect_home(result["message"])

    db.add(
        Project(
            name=subdomain,
            container_id=result.get("container_id"),
            disk_used_mb=disk_used_mb,
            memory_limit_mb=limits["memory_limit_mb"],
            owner_id=user.id,
        )
    )
    db.commit()
        
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete/{project_id}")
async def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user_from_request(request, db)
    if not user:
        return redirect_home("Сначала войдите в аккаунт", "login")

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return redirect_home("Проект не найден")

    if not user.is_admin and project.owner_id != user.id:
        return redirect_home("Недостаточно прав для удаления этого проекта")

    deleted = delete_student_container(project.container_id or project.name)
    if not deleted:
        delete_student_container(project.name)
    db.delete(project)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/default-limits")
async def update_default_limits(
    request: Request,
    default_max_projects: int = Form(...),
    default_disk_limit_mb: int = Form(...),
    default_memory_limit_mb: int = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = admin_required(request, db)
    if redirect:
        return redirect

    settings = get_admin_settings(db)
    settings.default_max_projects = max(default_max_projects, 1)
    settings.default_disk_limit_mb = max(default_disk_limit_mb, 1)
    settings.default_memory_limit_mb = max(default_memory_limit_mb, 32)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/users/{user_id}/limits")
async def update_user_limits(
    user_id: int,
    request: Request,
    max_projects: str = Form(""),
    disk_limit_mb: str = Form(""),
    memory_limit_mb: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = admin_required(request, db)
    if redirect:
        return redirect

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return redirect_home("Пользователь не найден")

    try:
        target_user.max_projects = empty_to_none(max_projects)
        target_user.disk_limit_mb = empty_to_none(disk_limit_mb)
        target_user.memory_limit_mb = empty_to_none(memory_limit_mb, 32)
    except ValueError:
        return redirect_home("Лимиты должны быть числами")
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    request: Request,
    is_admin: str = Form(""),
    db: Session = Depends(get_db),
):
    user, redirect = admin_required(request, db)
    if redirect:
        return redirect

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return redirect_home("Пользователь не найден")
    if target_user.id == user.id:
        return redirect_home("Нельзя изменить роль текущего администратора")

    target_user.is_admin = is_admin == "on"
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/projects/{project_id}/{action}")
async def update_project_state(
    project_id: int,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user, redirect = admin_required(request, db)
    if redirect:
        return redirect

    if action not in {"start", "stop", "restart"}:
        return redirect_home("Неизвестное действие с контейнером")

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return redirect_home("Проект не найден")

    changed = set_student_container_state(project.container_id or project.name, action)
    if not changed:
        return redirect_home("Не удалось изменить состояние контейнера")

    return RedirectResponse(url="/", status_code=303)
