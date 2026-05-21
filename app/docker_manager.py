import docker
import tarfile
import zipfile
import io
import os
import shutil

client = docker.from_env()
NETWORK_NAME = "paas_web_network"
PLATFORM_SERVICE_CONTAINERS = 3

def get_student_containers():
    try:
        containers = client.containers.list(all=True)
        return [
            {"id": c.short_id, "name": c.name.replace("student-app-", ""), "status": c.status} 
            for c in containers if c.name.startswith("student-app-")
        ]
    except:
        return []

def create_student_container(subdomain: str, zip_path: str):
    project_name = f"student-app-{subdomain}"
    temp_dir = f"/tmp/deploy_{subdomain}"
    
    try:
        # 1. Очистка
        try:
            client.containers.get(project_name).remove(force=True)
        except: pass

        # 2. Распаковка архива
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        os.makedirs(temp_dir)
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(temp_dir)
        
        # Ищем папку с index.html
        root_dir = temp_dir
        for r, d, f in os.walk(temp_dir):
            if "index.html" in f:
                root_dir = r
                break
        
        # 3. Создаем tar для Nginx
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode='w') as tar:
            for file in os.listdir(root_dir):
                full_path = os.path.join(root_dir, file)
                tar.add(full_path, arcname=file)
        tar_stream.seek(0)

        # 4. Запуск контейнера
        container = client.containers.run(
            "nginx:alpine",
            name=project_name,
            detach=True,
            network=NETWORK_NAME,
            restart_policy={"Name": "unless-stopped"}
        )
        
        # 5. Загрузка файлов
        container.put_archive("/usr/share/nginx/html", tar_stream)
        
        return {"status": "success", "message": "Сайт готов!"}

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)

def delete_student_container(container_id: str):
    try:
        # Находим контейнер по короткому ID или имени
        c = client.containers.get(container_id)
        c.remove(force=True)
        return True
    except:
        return False

def ping_docker():
    try:
        client.ping()
        info = client.info()
        containers_running = max(
            info.get("ContainersRunning", 0) - PLATFORM_SERVICE_CONTAINERS,
            0,
        )
        return {
            "status": "success",
            "message": "Docker активен",
            "docker_version": info.get("ServerVersion", "неизвестно"),
            "containers_running": containers_running,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Docker не найден: {e}",
            "docker_version": "неизвестно",
            "containers_running": 0,
        }

def is_subdomain_available(subdomain: str):
    try:
        client.containers.get(f"student-app-{subdomain}")
        return False
    except:
        return True
