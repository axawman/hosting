from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
import random

# ДОБАВИЛИ get_student_containers и delete_student_container в импорт
from app.docker_manager import ping_docker, create_student_container, get_student_containers, delete_student_container

app = FastAPI(title="Студенческий Хостинг")
templates = Jinja2Templates(directory="app/templates")

@app.get("/")
async def read_root(request: Request):
    docker_status = ping_docker()
    # Получаем список проектов
    active_projects = get_student_containers()
    
    return templates.TemplateResponse(
        request=request,
        name="index.html", 
        context={
            "title": "Панель управления хостингом",
            "docker_status": docker_status,
            "projects": active_projects # Передаем список в шаблон
        }
    )

# НОВЫЙ ЭНДПОИНТ ДЛЯ СОЗДАНИЯ ПРОЕКТА
@app.post("/deploy-test")
async def deploy_test_project(request: Request):
    project_id = random.randint(1000, 9999)
    # Генерируем тестовые данные (позже студент будет вводить их сам)
    project_name = f"test-student-app-{project_id}"
    # Для теста будем использовать локальный домен, например test.localhost
    domain = f"test-{project_id}.localhost"
    
    # Запускаем контейнер
    result = create_student_container(project_name, domain)
    
    # Возвращаем пользователя на главную страницу (пока без вывода логов, просто перезагрузка)
    # В реальном проекте тут лучше возвращать JSON или выводить сообщение об успехе
    print(result) # Выведет статус в терминал сервера Uvicorn
    return RedirectResponse(url="/", status_code=303)

# НОВЫЙ ЭНДПОИНТ ДЛЯ УДАЛЕНИЯ ПРОЕКТА
@app.post("/delete/{container_id}")
async def delete_project(container_id: str):
    delete_student_container(container_id)
    return RedirectResponse(url="/", status_code=303)