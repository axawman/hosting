import docker
import tarfile
import zipfile
import io
import os
import shutil

client = docker.from_env()

# Фиксированное имя сети, которое мы задали в docker-compose
NETWORK_NAME = "paas_web_network"

def ping_docker():
    try:
        info = client.info()
        return {
            "status": "success",
            "message": "Docker подключен!",
            "docker_version": info.get("ServerVersion"),
            "containers_running": info.get("ContainersRunning")
        }
    except Exception as e:
        return {"status": "error", "message": f"Ошибка подключения: {str(e)}"}

def get_student_containers():
    try:
        containers = client.containers.list(all=True, filters={"label": "traefik.enable=true"})
        return [{"id": c.short_id, "name": c.name, "status": c.status} for c in containers]
    except Exception as e:
        print(f"Ошибка при получении списка: {e}")
        return []

def is_subdomain_available(subdomain: str):
    project_name = f"student-app-{subdomain}"
    for c in get_student_containers():
        if c['name'] == project_name:
            return False
    return True

def validate_and_extract_zip(zip_path: str, temp_dir: str):
    """Распаковывает архив и находит корень (где лежит index.html)"""
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(temp_dir)
        
    items = os.listdir(temp_dir)
    valid_items = [i for i in items if not i.startswith('__MACOSX') and not i.startswith('.')]
    
    root_dir = temp_dir
    if len(valid_items) == 1 and os.path.isdir(os.path.join(temp_dir, valid_items[0])):
        root_dir = os.path.join(temp_dir, valid_items[0])
        
    if not os.path.isfile(os.path.join(root_dir, 'index.html')):
        return None, "Файл index.html не найден. Поместите его в корень архива."
        
    return root_dir, "OK"

def create_student_container(subdomain: str, zip_path: str):
    """Создает контейнер в единой сети с Traefik"""
    project_name = f"student-app-{subdomain}"
    domain = f"{subdomain}.localhost"
    temp_dir = f"/tmp/{project_name}_extracted"
    
    try:
        # Проверка: существует ли наша сеть
        try:
            client.networks.get(NETWORK_NAME)
        except docker.errors.NotFound:
            return {"status": "error", "message": f"Сеть {NETWORK_NAME} не найдена. Перезапустите docker-compose."}

        root_dir, validation_msg = validate_and_extract_zip(zip_path, temp_dir)
        if not root_dir:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return {"status": "error", "message": validation_msg}

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode='w') as tar:
            for root, _, files in os.walk(root_dir):
                for file in files:
                    if file.startswith('._') or '__MACOSX' in root:
                        continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, root_dir)
                    tar.add(file_path, arcname=arcname)
                    
        shutil.rmtree(temp_dir)
        tar_stream.seek(0)

        # Строгие правила Traefik с указанием точной сети
        labels = {
            "traefik.enable": "true",
            f"traefik.http.routers.{project_name}.rule": f"Host(`{domain}`)",
            f"traefik.http.routers.{project_name}.entrypoints": "web",
            f"traefik.http.services.{project_name}.loadbalancer.server.port": "80",
            "traefik.docker.network": NETWORK_NAME
        }

        container = client.containers.run(
            "nginx:alpine",
            name=project_name,
            detach=True,
            mem_limit="128m",
            nano_cpus=500000000,
            network=NETWORK_NAME,
            labels=labels,
            restart_policy={"Name": "unless-stopped"} # Перезапуск при падении
        )
        
        container.put_archive("/usr/share/nginx/html", tar_stream)
        
        return {
            "status": "success", 
            "container_id": container.short_id, 
            "message": f"Проект {project_name} успешно запущен!"
        }
    except Exception as e:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        return {"status": "error", "message": str(e)}

def delete_student_container(container_id: str):
    try:
        container = client.containers.get(container_id)
        container.remove(force=True)
        return True
    except Exception as e:
        print(f"Ошибка удаления: {e}")
        return False