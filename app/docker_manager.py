import docker

client = docker.from_env()

def ping_docker():
    """
    Проверяет, жив ли Docker и может ли FastAPI им управлять.
    """
    try:
        # Пытаемся запросить системную информацию
        info = client.info()
        return {
            "status": "success",
            "message": "Docker подключен!",
            "docker_version": info.get("ServerVersion"),
            "containers_running": info.get("ContainersRunning")
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Ошибка подключения к Docker: {str(e)}"
        }

def create_student_container(project_name: str, domain: str):
    """
    Запускает изолированный контейнер для проекта студента с лимитами.
    """
    try:
        # Для теста берем готовый образ Nginx. 
        # Позже здесь будет образ, собранный из кода студента.
        image = "nginx:alpine" 

        # Метки для Traefik (динамическая маршрутизация)
        labels = {
            "traefik.enable": "true",
            f"traefik.http.routers.{project_name}.rule": f"Host(`{domain}`)",
            f"traefik.http.services.{project_name}.loadbalancer.server.port": "80"
        }

        # ВАЖНО: Имя сети зависит от названия папки твоего проекта.
        # Если папка называется volsu_hosting, Docker Compose назовет сеть volsu_hosting_paas_network
        # Уточни это имя командой `docker network ls` в терминале, если контейнер не запустится.
        network_name = "volsu_hosting_paas_network"

        container = client.containers.run(
            image,
            name=project_name,
            detach=True,             # Запуск в фоновом режиме
            mem_limit="128m",        # Жесткий лимит оперативной памяти (128 МБ)
            nano_cpus=500000000,     # Лимит процессора (0.5 ядра)
            network=network_name,    # Подключаем в одну сеть с Traefik
            labels=labels            # Передаем правила маршрутизации
        )
        
        return {
            "status": "success", 
            "container_id": container.short_id, 
            "message": f"Проект {project_name} успешно запущен!"
        }
    except docker.errors.APIError as e:
        return {"status": "error", "message": f"Ошибка Docker: {str(e)}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def get_student_containers():
    """
    Возвращает список всех контейнеров, созданных нашим хостингом.
    """
    try:
        # Ищем только те контейнеры, у которых есть наша метка traefik.enable=true
        containers = client.containers.list(all=True, filters={"label": "traefik.enable=true"})
        
        projects = []
        for c in containers:
            projects.append({
                "id": c.short_id,
                "name": c.name,
                "status": c.status, # running, exited и т.д.
            })
        return projects
    except Exception as e:
        print(f"Ошибка при получении списка: {e}")
        return []

def delete_student_container(container_id: str):
    """
    Принудительно останавливает и удаляет контейнер по его ID.
    """
    try:
        container = client.containers.get(container_id)
        container.remove(force=True) # force=True сначала останавливает, потом удаляет
        return True
    except Exception as e:
        print(f"Ошибка при удалении: {e}")
        return False