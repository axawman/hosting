from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
import os
import shutil

from app.docker_manager import (
    ping_docker, 
    create_student_container, 
    get_student_containers, 
    delete_student_container,
    is_subdomain_available
)

app = FastAPI(title="Студенческий Хостинг")
templates = Jinja2Templates(directory="app/templates")

@app.get("/")
async def read_root(request: Request, error: str = None):
    docker_status = ping_docker()
    active_projects = get_student_containers()
    
    return templates.TemplateResponse(
        request=request,
        name="index.html", 
        context={
            "title": "Панель управления хостингом",
            "docker_status": docker_status,
            "projects": active_projects,
            "error": error
        }
    )

@app.post("/deploy")
async def deploy_project(
    request: Request, 
    subdomain: str = Form(...),
    file: UploadFile = File(...)
):
    subdomain = subdomain.lower().strip()
    
    # 1. Проверки названия поддомена
    if not subdomain.replace("-", "").isalnum():
        return RedirectResponse(url="/?error=Имя поддомена может содержать только латинские буквы, цифры и дефис", status_code=303)
        
    if not is_subdomain_available(subdomain):
        return RedirectResponse(url="/?error=Этот поддомен уже занят", status_code=303)
        
    if not file.filename.endswith('.zip'):
        return RedirectResponse(url="/?error=Загрузите файл в формате ZIP", status_code=303)

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
        return RedirectResponse(url=f"/?error={result['message']}", status_code=303)
        
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete/{container_id}")
async def delete_project(container_id: str):
    delete_student_container(container_id)
    return RedirectResponse(url="/", status_code=303)