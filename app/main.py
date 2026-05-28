from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import os
import shutil

from app.db import (
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


@app.get("/")
async def read_root(
    request: Request,
    error: str = None,
    auth: str = None,
    db: Session = Depends(get_db),
):
    user = current_user_from_request(request, db)
    docker_status = ping_docker()
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
            }
            for project in owned_projects
        ]
    
    return templates.TemplateResponse(
        request=request,
        name="index.html", 
        context={
            "title": "Панель управления хостингом",
            "docker_status": docker_status,
            "projects": active_projects,
            "user": user,
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

    # 2. Сохранение файла
    os.makedirs("/tmp/uploads", exist_ok=True)
    zip_path = f"/tmp/uploads/{subdomain}.zip"
    
    try:
        with open(zip_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # 3. Деплой (с валидацией внутри)
        result = create_student_container(subdomain, zip_path)
    finally:
        # 4. Гарантированно удаляем ZIP с сервера после работы
        if os.path.exists(zip_path):
            os.remove(zip_path)
    
    if result["status"] == "error":
        return redirect_home(result["message"])

    db.add(Project(name=subdomain, container_id=result.get("container_id"), owner_id=user.id))
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
