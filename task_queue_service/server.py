import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import json
import sqlite3
import pymysql

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel

# -----------------------------------------------------------------------------
# 配置区域
# -----------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
WORKFLOW_ROOT = PROJECT_ROOT / "workflows"
DATA_ROOT = PROJECT_ROOT / "data"
DB_DRIVER = os.getenv("AGLM_DB_DRIVER", "sqlite").lower()
DB_PATH = Path(os.getenv("AGLM_DB_PATH", DATA_ROOT / "xiaomu.db"))
DB_HOST = os.getenv("AGLM_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("AGLM_DB_PORT", "3306"))
DB_USER = os.getenv("AGLM_DB_USER", "xm_user")
DB_PASSWORD = os.getenv("AGLM_DB_PASSWORD", "xm_pass")
DB_NAME = os.getenv("AGLM_DB_NAME", "xiaomu")

REDIS_HOST = os.getenv("AGLM_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("AGLM_REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("AGLM_REDIS_DB", "0"))
TASK_QUEUE_KEY = os.getenv("AGLM_TASK_QUEUE", "aglm:task_queue")
TASK_KEY_PREFIX = os.getenv("AGLM_TASK_PREFIX", "aglm:task")
WORKER_COUNT = int(os.getenv("AGLM_WORKER_COUNT", "2"))
BRPOP_TIMEOUT = int(os.getenv("AGLM_BRPOP_TIMEOUT", "10"))
DEFAULT_CMD_TIMEOUT = int(os.getenv("AGLM_CMD_TIMEOUT", "300"))

# -----------------------------------------------------------------------------
# Redis 轻量客户端 (仅覆盖必要命令)
# -----------------------------------------------------------------------------


class SimpleRedisClient:
    def __init__(self, host: str, port: int, db: int = 0, default_timeout: int = 5):
        self.host = host
        self.port = port
        self.db = db
        self.default_timeout = default_timeout

    def _read_response(self, fp):
        prefix = fp.read(1)
        if not prefix:
            raise RuntimeError("Empty Redis response")

        if prefix == b"+":
            return fp.readline().rstrip(b"\r\n").decode("utf-8")
        if prefix == b"-":
            raise RuntimeError(fp.readline().rstrip(b"\r\n").decode("utf-8"))
        if prefix == b":":
            return int(fp.readline().rstrip(b"\r\n"))
        if prefix == b"$":
            length = int(fp.readline().rstrip(b"\r\n"))
            if length == -1:
                return None
            data = fp.read(length)
            fp.read(2)  # CRLF
            return data.decode("utf-8")
        if prefix == b"*":
            length = int(fp.readline().rstrip(b"\r\n"))
            if length == -1:
                return None
            return [self._read_response(fp) for _ in range(length)]

        raise RuntimeError(f"Unsupported Redis prefix: {prefix}")

    def _execute(self, command_parts: List[Any], timeout: Optional[int] = None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout or self.default_timeout)
        try:
            sock.connect((self.host, self.port))

            req = f"*{len(command_parts)}\r\n"
            for part in command_parts:
                part_str = str(part)
                req += f"${len(part_str.encode('utf-8'))}\r\n{part_str}\r\n"

            sock.sendall(req.encode("utf-8"))
            fp = sock.makefile("rb")
            return self._read_response(fp)
        finally:
            sock.close()

    def select_db(self):
        self._execute(["SELECT", self.db])

    def lpush(self, key: str, value: str) -> int:
        self.select_db()
        return int(self._execute(["LPUSH", key, value]) or 0)

    def brpop(self, key: str, timeout: int) -> Optional[Tuple[str, str]]:
        self.select_db()
        resp = self._execute(["BRPOP", key, timeout], timeout=timeout + 2)
        if resp is None:
            return None
        if isinstance(resp, list) and len(resp) == 2:
            return resp[0], resp[1]
        return None

    def hset(self, key: str, mapping: Dict[str, Any]) -> int:
        self.select_db()
        parts: List[Any] = ["HSET", key]
        for field, value in mapping.items():
            parts.extend([field, value])
        return int(self._execute(parts) or 0)

    def hget(self, key: str, field: str) -> Optional[str]:
        self.select_db()
        resp = self._execute(["HGET", key, field])
        if resp is None:
            return None
        return str(resp)

    def llen(self, key: str) -> int:
        self.select_db()
        return int(self._execute(["LLEN", key]) or 0)


# -----------------------------------------------------------------------------
# 模型与任务结构
# -----------------------------------------------------------------------------


class TaskRequest(BaseModel):
    user: str
    content: str
    task_type: Optional[str] = None
    script_args: Optional[List[str]] = None


class FinishRequest(BaseModel):
    task_id: str
    status: str
    result: Optional[str] = None
    user: Optional[str] = None
    notify: bool = True


@dataclass
class WorkflowDefinition:
    name: str
    build_command: Callable[[Dict[str, Any]], List[str]]
    timeout: int
    description: str

    def command(self, payload: Dict[str, Any]) -> List[str]:
        return self.build_command(payload)


# -----------------------------------------------------------------------------
# 意图识别与工作流注册表
# -----------------------------------------------------------------------------


def _build_deployment_check_cmd(payload: Dict[str, Any]) -> List[str]:
    base_url = os.getenv("PHONE_AGENT_BASE_URL", os.getenv("AGLM_MODEL_BASE_URL", "http://localhost:8000/v1"))
    model = os.getenv("PHONE_AGENT_MODEL", os.getenv("AGLM_MODEL_NAME", "autoglm-phone-9b"))
    api_key = os.getenv("PHONE_AGENT_API_KEY", "EMPTY")
    messages_file = os.getenv("AGLM_DEPLOY_MESSAGES_FILE", str(SCRIPTS_ROOT / "sample_messages.json"))

    return [
        sys.executable,
        str(SCRIPTS_ROOT / "check_deployment_cn.py"),
        "--base-url",
        base_url,
        "--apikey",
        api_key,
        "--model",
        model,
        "--messages-file",
        messages_file,
    ]


def _build_report_stub_cmd(payload: Dict[str, Any]) -> List[str]:
    report_hint = payload.get("content", "")
    return [
        sys.executable,
        "-c",
        f"print('Report placeholder for: {report_hint}')",
    ]


def _build_echo_cmd(payload: Dict[str, Any]) -> List[str]:
    content = payload.get("content", "")
    intent = payload.get("intent", "general")
    return [
        sys.executable,
        "-c",
        f"print('Received intent={intent}: {content}')",
    ]


def _build_travel_plan_cmd(payload: Dict[str, Any]) -> List[str]:
    cmd: List[str] = [sys.executable, str(WORKFLOW_ROOT / "travel_plan.py")]

    script_args = payload.get("script_args") or []
    if script_args:
        cmd.extend(str(a) for a in script_args)
    else:
        # 将原始需求传递给前端执行器做解析
        note = payload.get("content") or ""
        if note:
            cmd.extend(["--note", note])

    # 透传模型参数（如配置）
    base_url = os.getenv("PHONE_AGENT_BASE_URL") or os.getenv("AGLM_MODEL_BASE_URL")
    model = os.getenv("PHONE_AGENT_MODEL") or os.getenv("AGLM_MODEL_NAME")
    api_key = os.getenv("PHONE_AGENT_API_KEY")
    device_id = os.getenv("PHONE_AGENT_DEVICE_ID")

    if base_url:
        cmd.extend(["--base-url", base_url])
    if api_key:
        cmd.extend(["--apikey", api_key])
    if model:
        cmd.extend(["--model", model])
    if device_id:
        cmd.extend(["--device-id", device_id])

    return cmd


WORKFLOW_REGISTRY: Dict[str, WorkflowDefinition] = {
    "deployment_check": WorkflowDefinition(
        name="deployment_check",
        build_command=_build_deployment_check_cmd,
        timeout=int(os.getenv("AGLM_DEPLOY_TIMEOUT", str(DEFAULT_CMD_TIMEOUT))),
        description="Model health check via scripts/check_deployment_cn.py",
    ),
    "report_stub": WorkflowDefinition(
        name="report_stub",
        build_command=_build_report_stub_cmd,
        timeout=120,
        description="Placeholder workflow for data/report requests",
    ),
    "travel_plan": WorkflowDefinition(
        name="travel_plan",
        build_command=_build_travel_plan_cmd,
        timeout=1800,
        description="Multi-city travel plan workflow using phone agent apps",
    ),
    "echo": WorkflowDefinition(
        name="echo",
        build_command=_build_echo_cmd,
        timeout=60,
        description="Fallback workflow to echo user content",
    ),
}

INTENT_RULES = [
    {
        "intent": "deployment_check",
        "workflow": "deployment_check",
        "keywords": ["部署", "上线", "发布", "deployment", "health", "健康", "接口", "模型"],
    },
    {
        "intent": "report_query",
        "workflow": "report_stub",
        "keywords": ["查询", "报表", "统计", "数据", "report", "流量"],
    },
    {
        "intent": "travel_plan",
        "workflow": "travel_plan",
        "keywords": ["旅游", "旅行", "行程", "攻略", "机票", "航班", "高铁", "火车", "12306", "携程", "美团", "住宿", "酒店", "比价"],
    },
]

DEFAULT_INTENT = {"intent": "general", "workflow": "echo"}


def detect_intent(content: str) -> Dict[str, str]:
    normalized = content.lower()
    for rule in INTENT_RULES:
        for keyword in rule["keywords"]:
            if keyword.lower() in normalized:
                return {"intent": rule["intent"], "workflow": rule["workflow"]}
    return DEFAULT_INTENT.copy()


def register_dynamic_script_workflow(task_type: str, script_args: Optional[List[str]] = None) -> Optional[str]:
    """
    If a script named scripts/{task_type}.py exists, register a workflow that calls it.
    Returns the workflow name if registered.
    """
    if task_type in WORKFLOW_REGISTRY:
        return task_type

    script_path = WORKFLOW_ROOT / f"{task_type}.py"
    if not script_path.is_file():
        return None

    def _build_dynamic_cmd(payload: Dict[str, Any]) -> List[str]:
        args_from_request = payload.get("script_args") or script_args or []
        cmd: List[str] = [sys.executable, str(script_path)]
        if isinstance(args_from_request, list):
            cmd.extend(str(a) for a in args_from_request)
        elif isinstance(args_from_request, str):
            cmd.append(args_from_request)

        # 若未提供额外参数，默认将 content 作为单一参数传递
        if not args_from_request and payload.get("content"):
            cmd.append(str(payload["content"]))
        return cmd

    WORKFLOW_REGISTRY[task_type] = WorkflowDefinition(
        name=task_type,
        build_command=_build_dynamic_cmd,
        timeout=DEFAULT_CMD_TIMEOUT,
        description=f"Dynamic script workflow for {script_path.name}",
    )
    return task_type


# -----------------------------------------------------------------------------
# 执行与调度
# -----------------------------------------------------------------------------


redis_client = SimpleRedisClient(REDIS_HOST, REDIS_PORT, REDIS_DB)
app = FastAPI()
worker_threads: List[threading.Thread] = []


# -----------------------------------------------------------------------------
# 持久化层 (SQLite 作为示例，可替换为 MySQL/Postgres)
# -----------------------------------------------------------------------------


def get_db_conn():
    if DB_DRIVER == "mysql":
        return pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
    # default sqlite
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        if DB_DRIVER == "mysql":
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id VARCHAR(64) PRIMARY KEY,
                    user VARCHAR(255),
                    type VARCHAR(255),
                    status VARCHAR(64),
                    redis_key VARCHAR(255),
                    created_at DOUBLE,
                    updated_at DOUBLE,
                    last_checkpoint TEXT,
                    resume_hint TEXT,
                    retries INT DEFAULT 0,
                    payload_json MEDIUMTEXT,
                    result_summary MEDIUMTEXT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    task_id VARCHAR(64),
                    phase VARCHAR(255),
                    status VARCHAR(64),
                    input MEDIUMTEXT,
                    output MEDIUMTEXT,
                    checkpoint_token TEXT,
                    created_at DOUBLE,
                    INDEX idx_task_id (task_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    user TEXT,
                    type TEXT,
                    status TEXT,
                    redis_key TEXT,
                    created_at REAL,
                    updated_at REAL,
                    last_checkpoint TEXT,
                    resume_hint TEXT,
                    retries INTEGER DEFAULT 0,
                    payload_json TEXT,
                    result_summary TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    phase TEXT,
                    status TEXT,
                    input TEXT,
                    output TEXT,
                    checkpoint_token TEXT,
                    created_at REAL
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


def db_execute(sql: str, params: Tuple[Any, ...] = (), fetch: str = ""):
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if not getattr(conn, "autocommit", False):
            conn.commit()
        if fetch == "one":
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
        if fetch == "all":
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        return None
    finally:
        conn.close()


def persist_task_record(task_id: str, user: str, workflow: str, task_type: Optional[str], payload: Dict[str, Any]):
    payload_json = json.dumps(payload, ensure_ascii=False)
    now = time.time()
    db_execute(
        """
        INSERT OR REPLACE INTO tasks (id, user, type, status, redis_key, created_at, updated_at, payload_json)
        VALUES (?, ?, ?, COALESCE((SELECT status FROM tasks WHERE id = ?), 'pending'), ?, ?, ?, ?)
        """,
        (
            task_id,
            user,
            task_type or workflow,
            task_id,
            f"{TASK_KEY_PREFIX}:{task_id}",
            now,
            now,
            payload_json,
        ),
    )


def update_task_record(task_id: str, status: str, result: str = "", resume_hint: str = "", checkpoint: str = ""):
    now = time.time()
    db_execute(
        """
        UPDATE tasks
        SET status = ?, updated_at = ?, result_summary = ?, resume_hint = COALESCE(NULLIF(?, ''), resume_hint), last_checkpoint = COALESCE(NULLIF(?, ''), last_checkpoint)
        WHERE id = ?
        """,
        (status, now, result, resume_hint, checkpoint, task_id),
    )


def record_task_event(task_id: str, phase: str, status: str, input_text: str = "", output_text: str = "", checkpoint_token: str = ""):
    now = time.time()
    db_execute(
        """
        INSERT INTO task_events (task_id, phase, status, input, output, checkpoint_token, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, phase, status, input_text, output_text, checkpoint_token, now),
    )


def load_task_record(task_id: str) -> Optional[Dict[str, Any]]:
    return db_execute("SELECT * FROM tasks WHERE id = ?", (task_id,), fetch="one")


def summarize_task(task_id: str) -> Dict[str, Any]:
    meta = load_task_record(task_id) or {}
    redis_status = redis_client.hget(task_status_key(task_id), "status")
    result = redis_client.hget(task_status_key(task_id), "final_result")
    merged = {
        "task_id": task_id,
        "status": redis_status or meta.get("status"),
        "user": meta.get("user"),
        "type": meta.get("type"),
        "workflow": meta.get("workflow") or meta.get("type"),
        "result": result or meta.get("result_summary"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "resume_hint": meta.get("resume_hint"),
        "last_checkpoint": meta.get("last_checkpoint"),
    }
    return merged


def task_status_key(task_id: str) -> str:
    return f"{TASK_KEY_PREFIX}:{task_id}"


def trigger_reply(user: str, message: str):
    extra_args: List[str] = []
    base_url = os.getenv("PHONE_AGENT_BASE_URL")
    api_key = os.getenv("PHONE_AGENT_API_KEY")
    model = os.getenv("PHONE_AGENT_MODEL")

    if base_url:
        extra_args.extend(["--base-url", base_url])
    if api_key:
        extra_args.extend(["--apikey", api_key])
    if model:
        extra_args.extend(["--model", model])

    cmd = [sys.executable, str(SCRIPTS_ROOT / "reply_msg.py"), "--user", user, "--message", message] + extra_args
    try:
        subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False)
    except Exception as exc:
        print(f"[!] Failed to trigger reply for {user}: {exc}")


def run_workflow(task_payload: Dict[str, Any]) -> Tuple[str, str]:
    workflow_name = task_payload.get("workflow", "echo")
    workflow = WORKFLOW_REGISTRY.get(workflow_name, WORKFLOW_REGISTRY["echo"])

    try:
        cmd = workflow.command(task_payload)
    except Exception as exc:
        return "failed", f"构建命令失败: {exc}"

    print(f"[*] Running workflow {workflow.name} -> {cmd}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=workflow.timeout,
        )
    except subprocess.TimeoutExpired:
        return "failed", "执行超时"
    except Exception as exc:
        return "failed", f"执行异常: {exc}"

    output = proc.stdout.strip() or proc.stderr.strip() or "无输出"
    output = output[-2000:]  # 防止消息过长
    status = "success" if proc.returncode == 0 else "failed"
    return status, output


def get_task_metadata(task_id: str) -> Dict[str, Optional[str]]:
    fields = ["user", "workflow", "intent", "task_type", "content"]
    meta: Dict[str, Optional[str]] = {}
    for field in fields:
        try:
            meta[field] = redis_client.hget(task_status_key(task_id), field)
        except Exception:
            meta[field] = None
    return meta


def finalize_task(task_payload: Dict[str, Any], status: str, result: str, notify: bool = True):
    task_id = task_payload.get("id")
    if not task_id:
        return

    meta = get_task_metadata(task_id)
    user = task_payload.get("user") or meta.get("user")
    workflow = task_payload.get("workflow") or meta.get("workflow") or "unknown"

    result_text = (result or "无详细结果").strip()
    result_text = result_text[-2000:]

    redis_client.hset(
        task_status_key(task_id),
        {
            "status": status,
            "finished_at": str(time.time()),
            "final_result": result_text,
            "workflow": workflow,
            "user": user or "",
        },
    )

    update_task_record(task_id, status=status, result=result_text)
    record_task_event(task_id, phase=workflow, status=status, output_text=result_text)

    if notify and user:
        reply_msg = f"任务 {task_id} ({workflow}) {status}。\n结果: {result_text}"
        trigger_reply(user, reply_msg)


def worker_loop(worker_id: int):
    print(f"[*] Worker {worker_id} started, waiting for tasks...")
    while True:
        try:
            item = redis_client.brpop(TASK_QUEUE_KEY, BRPOP_TIMEOUT)
            if item is None:
                continue

            _, payload_raw = item
            if not payload_raw:
                continue

            task_payload = json.loads(payload_raw)
            task_id = task_payload["id"]

            redis_client.hset(
                task_status_key(task_id),
                {
                    "status": "running",
                    "started_at": str(time.time()),
                    "worker": str(worker_id),
                },
            )

            record_task_event(task_id, phase="start", status="running", input_text=task_payload.get("content", ""))
            update_task_record(task_id, status="running")

            status, result = run_workflow(task_payload)

            finalize_task(task_payload, status, result, notify=True)
        except Exception as exc:
            print(f"[!] Worker {worker_id} error: {exc}")
            time.sleep(2)


def ensure_workers():
    if worker_threads:
        return
    for idx in range(WORKER_COUNT):
        t = threading.Thread(target=worker_loop, args=(idx,), daemon=True)
        t.start()
        worker_threads.append(t)


def resolve_workflow(content: str, task_type: Optional[str], script_args: Optional[List[str]]) -> Dict[str, str]:
    if task_type:
        # 先尝试匹配已有 workflow
        if task_type in WORKFLOW_REGISTRY:
            return {"intent": task_type, "workflow": task_type}

        # 再尝试动态脚本注册
        registered = register_dynamic_script_workflow(task_type, script_args)
        if registered:
            return {"intent": task_type, "workflow": registered}

    return detect_intent(content)


def enqueue_task(user: str, content: str, task_type: Optional[str], script_args: Optional[List[str]]) -> Dict[str, Any]:
    intent = resolve_workflow(content, task_type, script_args)
    workflow_name = intent["workflow"]
    task_id = f"AGLM-{uuid.uuid4().hex[:8].upper()}"

    payload = {
        "id": task_id,
        "user": user,
        "content": content,
        "intent": intent["intent"],
        "workflow": workflow_name,
        "created_at": time.time(),
        "task_type": task_type,
        "script_args": script_args or [],
    }

    queue_length = redis_client.lpush(TASK_QUEUE_KEY, json.dumps(payload, ensure_ascii=False))
    redis_client.hset(
        task_status_key(task_id),
        {
            "status": "pending",
            "created_at": str(time.time()),
            "intent": intent["intent"],
            "workflow": workflow_name,
            "user": user,
            "content": content,
            "task_type": task_type or "",
        },
    )

    persist_task_record(task_id, user, workflow_name, task_type, payload)
    record_task_event(task_id, phase="enqueue", status="pending", input_text=content)

    return {"task_id": task_id, "queue_length": queue_length, "intent": intent}


@app.on_event("startup")
def startup_event():
    init_db()
    ensure_workers()


@app.post("/enqueue")
async def enqueue(task: TaskRequest, background_tasks: BackgroundTasks):
    try:
        result = enqueue_task(task.user, task.content, task.task_type, task.script_args)
    except Exception as exc:
        print(f"[!] Failed to enqueue task: {exc}")
        return {"status": "error", "msg": str(exc)}

    background_tasks.add_task(ensure_workers)
    return {
        "status": "accepted",
        "task_id": result["task_id"],
        "queue_length": result["queue_length"],
        "intent": result["intent"],
        "task_type": task.task_type or result["intent"].get("workflow"),
    }


# 兼容旧路径
@app.post("/webhook")
async def receive_task(task: TaskRequest, background_tasks: BackgroundTasks):
    return await enqueue(task, background_tasks)


@app.post("/finish")
async def finish(finish_req: FinishRequest):
    meta = get_task_metadata(finish_req.task_id)
    payload = {
        "id": finish_req.task_id,
        "user": finish_req.user or meta.get("user"),
        "workflow": meta.get("workflow"),
        "task_type": meta.get("task_type"),
    }

    finalize_task(payload, finish_req.status, finish_req.result or "", notify=finish_req.notify)
    return {"status": "ok", "task_id": finish_req.task_id}


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/task/{task_id}")
async def get_task(task_id: str):
    summary = summarize_task(task_id)
    events = db_execute(
        "SELECT id, phase, status, input, output, checkpoint_token, created_at FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 20",
        (task_id,),
        fetch="all",
    ) or []
    return {"task": summary, "events": events}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
