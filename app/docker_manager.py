import docker
import tarfile
import zipfile
import io
import os
import shutil

client = docker.from_env()

def ping_docker():
    """
    Проверяет, жив ли Docker и может ли FastAPI им управлять.
    """
    try:
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

def get_student_containers():
    """
    Возвращает список всех контейнеров, созданных нашим хостингом.
    """
    try:
        containers = client.containers.list(all=True, filters={"label": "traefik.enable=true"})
        projects = []
        for c in containers:
            projects.append({
                "id": c.short_id,
                "name": c.name,
                "status": c.status,
            })
        return projects
    except Exception as e:
        print(f"Ошибка при получении списка: {e}")
        return []

def is_subdomain_available(subdomain: str):
    """
    Проверяет, не занят ли уже такой поддомен.
    """
    containers = get_student_containers()
    project_name = f"student-app-{subdomain}"
    for c in containers:
        if c['name'] == project_name:
            return False
    return True

def create_student_container(subdomain: str, zip_path: str):
    """
    Запускает контейнер и импортирует туда файлы из ZIP-архива пользователя.
    """
    project_name = f"student-app-{subdomain}"
    domain = f"{subdomain}.localhost"
    network_name = "hosting_paas_network"

    try:
        # 1. Создаем TAR-архив в памяти из ZIP-архива (Docker SDK принимает только TAR)
        tar_stream = io.BytesIO()
        temp_dir = f"/tmp/{project_name}_extracted"
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        with tarfile.open(fileobj=tar_stream, mode='w') as tar:
            for root, _, files in os.walk(temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Сохраняем относительный путь, чтобы файлы легли ровно в /usr/share/nginx/html
                    arcname = os.path.relpath(file_path, temp_dir)
                    tar.add(file_path, arcname=arcname)
                    
        # Очищаем временную папку
        shutil.rmtree(temp_dir)
        tar_stream.seek(0)

        # 2. Метки для Traefik (динамическая маршрутизация)
        labels = {
            "traefik.enable": "true",
            f"traefik.http.routers.{project_name}.rule": f"Host(`{domain}`)",
            f"traefik.http.services.{project_name}.loadbalancer.server.port": "80"
        }

        # 3. Запуск контейнера Nginx в фоновом режиме
        container = client.containers.run(
            "nginx:alpine",
            name=project_name,
            detach=True,
            mem_limit="128m",
            nano_cpus=500000000,
            network=network_name,
            labels=labels
        )
        
        # 4. Копируем файлы внутрь контейнера
        container.put_archive("/usr/share/nginx/html", tar_stream)
        
        return {
            "status": "success", 
            "container_id": container.short_id, 
            "message": f"Проект {project_name} успешно запущен!"
        }
    except docker.errors.APIError as e:
        return {"status": "error", "message": f"Ошибка Docker: {str(e)}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def delete_student_container(container_id: str):
    """
    Принудительно останавливает и удаляет контейнер по его ID.
    """
    try:
        container = client.containers.get(container_id)
        container.remove(force=True)
        return True
    except Exception as e:
        print(f"Ошибка при удалении: {e}")
        return False