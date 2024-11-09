import json
from fastapi import APIRouter, HTTPException
import os
import aiofiles
from loguru import logger
import traceback
from typing import Dict, Any
from pathlib import Path
import subprocess
import uuid
import psutil
import asyncio
import httpx
from fastapi import Request
from sse_starlette.sse import EventSourceResponse
from ..storage.json_file import load_byzer_sql_from_json, save_byzer_sql_to_json
from .request_types import AddByzerSQLRequest, RunSQLRequest, RunSQLRequest
from jproperties import Properties

router = APIRouter()

@router.post("/run/script")
async def run_script(request: RunSQLRequest):
    """Execute SQL script on Byzer SQL engine."""
    try:
        async with httpx.AsyncClient() as client:
            # 构建请求参数
            params = {
                "sql": request.sql,
                "owner": "admin", # 默认使用admin账户
                "jobType": "script",
                "executeMode": "query",
                "jobName": f"test_sql_{uuid.uuid4()}",
                "includeSchema": True,
                "sessionPerRequest": True,
                "sessionPerUser": True,
            }

            response = await client.post(
                f"{request.engine_url}/run/script",
                data=params,
                timeout=3600  # 设置1小时超时
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to execute SQL: {response.text}"
                )

            return response.json()

    except Exception as e:
        logger.error(f"Error executing SQL: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Failed to execute SQL: {str(e)}"
        )


@router.get("/byzer-sql")
async def list_byzer_sql():
    """List all Byzer SQL services."""
    services = await load_byzer_sql_from_json()
    return [{"name": name, **info} for name, info in services.items()]


@router.post("/byzer-sql/check-installation")
async def check_byzer_sql_installation(request: dict):
    """Check if byzer.sh exists in the specified directory."""
    install_dir = request.get("install_dir")
    if not install_dir:
        raise HTTPException(status_code=400, detail="install_dir is required")

    byzer_sh_path = os.path.join(install_dir, "bin", "byzer.sh")
    return {"has_byzer_sh": os.path.exists(byzer_sh_path)}


@router.post("/byzer-sql/add")
async def add_byzer_sql(request: AddByzerSQLRequest):
    """Add a new Byzer SQL service."""
    services = await load_byzer_sql_from_json()

    if request.name in services:
        raise HTTPException(
            status_code=400, detail=f"Byzer SQL {request.name} already exists"
        )

    # Check if directory exists and validate its structure
    if not os.path.exists(request.install_dir):
        os.makedirs(request.install_dir)

    # Check if bin/byzer.sh exists in the specified directory
    byzer_sh_path = os.path.join(request.install_dir, "bin", "byzer.sh")
    has_byzer_sh = os.path.exists(byzer_sh_path)

    new_service = {
        "status": "stopped",
        **request.model_dump(),
        "has_byzer_sh": has_byzer_sh,
    }

    services[request.name] = new_service
    await save_byzer_sql_to_json(services)
    return {"message": f"Byzer SQL {request.name} added successfully"}


# 全局变量存储各个下载任务的进度
download_progress_store = {}


@router.get("/api/download-progress/{task_id}")
async def download_progress(request: Request, task_id: str):
    """SSE endpoint for download progress updates"""

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break

            if task_id in download_progress_store:
                progress_data = download_progress_store[task_id]
                # Convert progress_data to JSON string and ensure it's properly formatted
                data = json.dumps(progress_data)

                # Send in SSE format with proper headers
                yield {"event": "message", "data": data}

                if progress_data.get("completed", False):
                    del download_progress_store[task_id]
                    break

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@router.post("/byzer-sql/download")
async def download_byzer_sql(request: Dict[str, str]):
    """Download and extract Byzer SQL package."""
    download_url = request["download_url"]
    install_dir = request["install_dir"]
    task_id = str(uuid.uuid4())
    download_progress_store[task_id] = {"task_id": task_id}

    async def download_and_extract():
        try:
            import aiohttp
            import aiofiles
            import time

            start_time = time.time()

            def calculate_speed(downloaded: int, start: float) -> float:
                elapsed = time.time() - start
                return downloaded / elapsed if elapsed > 0 else 0

            def calculate_eta(downloaded: int, total: int, start: float) -> float:
                elapsed = time.time() - start
                speed = downloaded / elapsed if elapsed > 0 else 0
                remaining = total - downloaded
                return remaining / speed if speed > 0 else 0

            # Download the file
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3600)
            ) as session:
                try:
                    async with session.get(download_url) as response:
                        if response.status != 200:
                            raise Exception(f"Failed to download: {response.status}")

                        # Get total file size
                        total_size = int(response.headers.get("content-length", 0))
                        tar_path = os.path.join(install_dir, "byzer.tar.gz")
                        downloaded_size = 0

                        # Download chunks asynchronously
                        async with aiofiles.open(tar_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(1024):
                                await f.write(chunk)
                                downloaded_size += len(chunk)
                                progress = int((downloaded_size / total_size) * 100)
                                download_progress_store[task_id] = {
                                    "task_id": task_id,
                                    "type": "download",
                                    "progress": progress,
                                    "downloaded_size": downloaded_size,
                                    "total_size": total_size,
                                    "speed": calculate_speed(
                                        downloaded_size, start_time
                                    ),
                                    "estimated_time": calculate_eta(
                                        downloaded_size, total_size, start_time
                                    ),
                                }
                except asyncio.TimeoutError:
                    download_progress_store[task_id] = {
                        "task_id": task_id,
                        "error": "下载超时",
                    }
                    raise Exception("Download timeout")

            # Extract the file asynchronously using tar command
            logger.info("Starting extraction...")

            # Use tar command to list files to count total members
            result = subprocess.run(
                ["tar", "-tzf", tar_path], capture_output=True, text=True
            )
            total_members = len(result.stdout.splitlines())

            # Create pipe to monitor extraction progress
            process = subprocess.Popen(
                ["tar", "-xzf", tar_path, "-C", install_dir, "--strip-components=1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Monitor progress
            current_member = 0
            while True:
                if process.poll() is not None:
                    break

                current_member += 1
                progress = int((current_member / total_members) * 100)
                download_progress_store[task_id] = {
                    "task_id": task_id,
                    "type": "extract",
                    "progress": min(progress, 100),
                }
                await asyncio.sleep(0.1)

            if process.returncode != 0:
                stderr = process.stderr.read().decode()
                raise Exception(f"Extraction failed: {stderr}")

            # Remove the tar file and set permissions
            await asyncio.to_thread(os.remove, tar_path)
            start_script = os.path.join(install_dir, "bin", "byzer.sh")
            if os.path.exists(start_script):
                await asyncio.to_thread(os.chmod, start_script, 0o755)

            download_progress_store[task_id] = {"task_id": task_id, "completed": True}

        except Exception as e:
            logger.error(f"Error during download/extraction: {str(e)}")
            logger.error(traceback.format_exc())
            download_progress_store[task_id] = {"task_id": task_id, "error": str(e)}

    # Start the download process asynchronously
    asyncio.create_task(download_and_extract())
    return {"message": "Download started", "task_id": task_id}


@router.delete("/byzer-sql/{service_name}")
async def delete_byzer_sql(service_name: str):
    """Delete a Byzer SQL service."""
    services = await load_byzer_sql_from_json()

    if service_name not in services:
        raise HTTPException(
            status_code=404, detail=f"Byzer SQL {service_name} not found"
        )

    service_info = services[service_name]
    if service_info["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a running service. Please stop it first.",
        )

    del services[service_name]
    await save_byzer_sql_to_json(services)

    # Try to delete log files if they exist
    try:
        log_files = [f"logs/{service_name}.out", f"logs/{service_name}.err"]
        for log_file in log_files:
            if os.path.exists(log_file):
                os.remove(log_file)
    except Exception as e:
        logger.warning(
            f"Failed to delete log files for service {service_name}: {str(e)}"
        )

    return {"message": f"Byzer SQL {service_name} deleted successfully"}


@router.get("/byzer-sql/{service_name}/config")
async def get_byzer_sql_config(service_name: str):
    """Get the configuration of a Byzer SQL service."""
    services = await load_byzer_sql_from_json()

    if service_name not in services:
        raise HTTPException(
            status_code=404, detail=f"Byzer SQL {service_name} not found"
        )

    service_info = services[service_name]
    config_file = os.path.join(
        service_info["install_dir"], "conf", "byzer.properties.override"
    )

    if not os.path.exists(config_file):
        return {"config": {}}

    config = {}
    try:
        properties = Properties()
        async with aiofiles.open(config_file, "rb") as f:
            content = await f.read()
            properties.load(content)

        # Convert properties to dict
        for item in properties.items():
            key, value = item
            config[key] = str(value.data)

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to read config file: {str(e)}"
        )

    return {"config": config}


@router.put("/byzer-sql/{service_name}/config")
async def update_byzer_sql_config(service_name: str, config: dict):
    """Update the configuration of a Byzer SQL service."""
    services = await load_byzer_sql_from_json()

    if service_name not in services:
        raise HTTPException(
            status_code=404, detail=f"Byzer SQL {service_name} not found"
        )

    service_info = services[service_name]
    if service_info["status"] == "running":
        raise HTTPException(
            status_code=400, detail="Cannot update config while service is running"
        )

    config_file = os.path.join(
        service_info["install_dir"], "conf", "byzer.properties.override"
    )
    config_dir = os.path.dirname(config_file)

    if not os.path.exists(config_dir):
        os.makedirs(config_dir)

    try:
        properties = Properties()

        # Add all config items to properties
        for key, value in config.items():
            properties[key] = value

        # Write to file
        async with aiofiles.open(config_file, "wb") as f:
            properties.store(f, encoding="utf-8")

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to write config file: {str(e)}"
        )

    return {"message": "Configuration updated successfully"}


@router.put("/byzer-sql/{service_name}")
async def update_byzer_sql(service_name: str, request: AddByzerSQLRequest):
    """Update an existing Byzer SQL service."""
    services = await load_byzer_sql_from_json()

    if service_name not in services:
        raise HTTPException(
            status_code=404, detail=f"Byzer SQL {service_name} not found"
        )

    service_info = services[service_name]
    if service_info["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail="Cannot update a running service. Please stop it first.",
        )

    service_info.update(request.model_dump())
    services[service_name] = service_info
    await save_byzer_sql_to_json(services)

    return {"message": f"Byzer SQL {service_name} updated successfully"}


@router.post("/byzer-sql/{service_name}/{action}")
async def manage_byzer_sql(service_name: str, action: str):
    """Start or stop a Byzer SQL service."""
    services = await load_byzer_sql_from_json()

    if service_name not in services:
        raise HTTPException(
            status_code=404, detail=f"Byzer SQL {service_name} not found"
        )

    if action not in ["start", "stop"]:
        raise HTTPException(
            status_code=400, detail="Invalid action. Use 'start' or 'stop'"
        )

    service_info = services[service_name]
    install_dir = service_info["install_dir"]

    if not os.path.exists(os.path.join(install_dir, "bin", "byzer.sh")):
        raise HTTPException(
            status_code=400,
            detail="Invalid installation directory. Missing byzer.sh script.",
        )

    try:
        os.makedirs("logs", exist_ok=True)
        stdout_log = open(os.path.join("logs", f"{service_name}.out"), "w")
        stderr_log = open(os.path.join("logs", f"{service_name}.err"), "w")

        if action == "start":
            start_script = os.path.join(install_dir, "bin", "byzer.sh")
            # Execute start command
            subprocess.run(
                [start_script, "start"],
                stdout=stdout_log,
                stderr=stderr_log,
                cwd=install_dir,
                check=True,
            )

            # Get PID from pid file
            pid_file = os.path.join(install_dir, "pid")
            try:
                # Wait a bit for the pid file to be created
                async for _ in range(60):
                    if os.path.exists(pid_file):
                        break
                    await asyncio.sleep(0.5)

                if os.path.exists(pid_file):
                    async with aiofiles.open(pid_file, "r") as f:
                        pid = int(await f.read().strip())
                    service_info["status"] = "running"
                    service_info["process_id"] = pid
                else:
                    raise Exception("PID file not created after 5 seconds")
            except Exception as e:
                logger.error(f"Failed to read PID file: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"Failed to read PID file: {str(e)}"
                )

        else:  # stop
            if "process_id" in service_info:
                stop_script = os.path.join(install_dir, "bin", "byzer.sh")
                subprocess.run([stop_script, "stop"], check=True, cwd=install_dir)
                service_info["status"] = "stopped"
                del service_info["process_id"]

        services[service_name] = service_info
        await save_byzer_sql_to_json(services)
        return {"message": f"Byzer SQL {service_name} {action}ed successfully"}

    except Exception as e:
        logger.error(f"Failed to {action} Byzer SQL: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500, detail=f"Failed to {action} Byzer SQL: {str(e)}"
        )


@router.get("/byzer-sql/{service_name}/status")
async def get_byzer_sql_status(service_name: str):
    """Get the status of a specified Byzer SQL service."""
    services = await load_byzer_sql_from_json()
    if service_name not in services:
        raise HTTPException(status_code=404, detail=f"Byzer SQL {service_name} not found")

    service_info = services[service_name]
    install_dir = service_info["install_dir"]
    pid_file = os.path.join(install_dir, "pid")
    
    # Check if pid file exists and process is running
    is_alive = False
    process_id = None
    if os.path.exists(pid_file):
        try:
            async with aiofiles.open(pid_file, 'r') as f:
                content = await f.read()
                process_id = int(content.strip())
                try:
                    process = psutil.Process(process_id)
                    is_alive = process.is_running()
                except psutil.NoSuchProcess:
                    is_alive = False
                    # Remove pid file if process is not running
                    try:
                        os.remove(pid_file)
                    except:
                        pass
        except:
            is_alive = False

    # Update the status based on whether the process is alive
    status = "running" if is_alive else "stopped"
    service_info["status"] = status
    if is_alive and process_id:
        service_info["process_id"] = process_id
    elif "process_id" in service_info:
        del service_info["process_id"]
        
    services[service_name] = service_info
    await save_byzer_sql_to_json(services)

    return {
        "service": service_name,
        "status": status,
        "process_id": process_id if is_alive else None,
        "is_alive": is_alive,
        "success": True,
    }
    }


@router.get("/byzer-sql/{service_name}/logs/{log_type}/{offset}")
async def get_byzer_sql_logs(
    service_name: str, log_type: str, offset: int = 0
) -> Dict[str, Any]:
    """Get the logs for a specific Byzer SQL service."""
    services = await load_byzer_sql_from_json()
    if service_name not in services:
        raise HTTPException(
            status_code=404, detail=f"Byzer SQL {service_name} not found"
        )

    if log_type not in ["byzer", "shell", "check-env"]:
        raise HTTPException(status_code=400, detail="Invalid log type")

    install_dir = services[service_name]["install_dir"]
    if log_type == "byzer":
        log_file = os.path.join(install_dir, "logs", "byzer.out")
    elif log_type == "shell":
        log_file = os.path.join(install_dir, "logs", "shell.stderr")
    else:
        log_file = os.path.join(install_dir, "logs", "check-env.error")

    try:
        if not os.path.exists(log_file):
            return {"content": "", "exists": False, "offset": 0}

        file_size = os.path.getsize(log_file)

        if offset < 0:
            read_size = min(abs(offset), file_size)
            async with aiofiles.open(
                log_file, mode="r", encoding="utf-8", errors="ignore"
            ) as f:
                if read_size < file_size:
                    await f.seek(file_size - read_size)
                content = await f.read(read_size)
                current_offset = file_size
            return {"content": content, "exists": True, "offset": current_offset}
        else:
            if offset > file_size:
                return {"content": "", "exists": True, "offset": file_size}

            async with aiofiles.open(
                log_file, mode="r", encoding="utf-8", errors="ignore"
            ) as f:
                await f.seek(offset)
                content = await f.read()
                current_offset = await f.tell()
            return {"content": content, "exists": True, "offset": current_offset}

    except Exception as e:
        logger.error(f"Error reading log file: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500, detail=f"Failed to read log file: {str(e)}"
        )
