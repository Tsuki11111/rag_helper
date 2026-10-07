"""
文件导入 Web 服务

把 LangGraph 知识库导入流程（PDF/MD → 解析 → 切分 → 向量化 → Milvus入库）
包装成 HTTP 接口，并提供可视化上传页面。

核心特性：
1. 文件上传后、跑图前，先按文件内容 SHA-256 校验是否已导入过（重复则跳过）
2. 支持 force=true 强制重新导入
3. 后台任务执行导入图，前端轮询 /status 获取节点级进度
"""
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.clients.minio_utils import get_minio_client
from app.core.error_policy import degrade
from app.core.logger import logger
from app.core.usage_tracker import usage_context
from app.import_process.agent.main_graph import get_import_app, import_thread_id
from app.clients.mongo_checkpoint_utils import (
    KIND_MONGO,
    get_checkpointer,
    graph_config,
    note_checkpointer_failure,
)
from app.import_process.agent.state import get_default_state
from app.clients.mongo_dedup_utils import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    check_document_exists,
    save_document_record,
    update_document_result,
)
from app.utils.auth_utils import clear_session_cookie, current_tenant, set_session_cookie
from app.utils.file_hash_utils import calc_file_hash
from app.utils.path_util import PROJECT_ROOT
from app.utils.task_utils import (
    add_done_task,
    add_running_task,
    clear_running_task_list,
    get_done_task_list,
    get_running_task_list,
    get_task_status,
    update_task_status,
)

# 允许上传的文件后缀
ALLOWED_EXTENSIONS = {".pdf", ".md"}
# 单文件大小上限（200MB），防止异常大文件
MAX_FILE_SIZE = 200 * 1024 * 1024

# 服务名，用于降级日志的标识（本模块不在图内，没有节点名可用）
NODE_NAME = "file_import_service"

app = FastAPI(
    title="File Import Service",
    description="知识库导入服务：上传 PDF/MD → 解析 → 切分 → 向量化 → Milvus入库（含重复文档检测）"
)

# 跨域配置：允许前端页面独立部署时调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    """
    服务启动时预热去重工具

    MongoDB 的集合与索引在 DocumentDedupTool 实例化时自动创建（幂等），
    这里主动取一次单例，把连接建立提前到启动阶段，避免首次上传时才连。
    """
    from app.clients.mongo_dedup_utils import get_dedup_tool
    get_dedup_tool()
    logger.info("去重工具已就绪（MongoDB）")

    # 一个用户都没有时，所有数据接口都会 401，这里提前把话说明白，省得一头雾水
    from app.clients.mongo_user_utils import count_users
    if count_users() == 0:
        logger.warning(
            "还没有任何用户，数据接口将全部返回 401。"
            "先建一个：.venv/Scripts/python.exe -m app.clients.mongo_user_utils add <称呼>"
        )

    # 上次没跑完的导入接着跑（中间状态在检查点里）—— 放最后，且它自己不会阻塞启动
    resume_pending_imports()

    logger.info("File Import Service 启动完成")


class LoginRequest(BaseModel):
    """登录请求：提交访问密钥"""
    key: str


@app.post("/login", summary="登录：校验访问密钥并写入会话 Cookie")
async def login(payload: LoginRequest, response: Response):
    """
    用访问密钥换一个 HttpOnly Cookie

    走 Cookie 而不是让前端存密钥发请求头，是因为查询服务的流式接口用 `EventSource`，
    它**无法自定义请求头**，只有 Cookie 能被浏览器自动携带；同理页面上十几处 fetch 也一行不用改。
    """
    function_name = sys._getframe().f_code.co_name
    from app.clients.mongo_user_utils import verify_key

    user = verify_key(payload.key)
    if not user:
        logger.warning(f"[{function_name}] 登录失败：密钥无效或已撤销")
        raise HTTPException(status_code=401, detail="访问密钥无效或已撤销")

    set_session_cookie(response, payload.key)
    logger.info(f"[{function_name}] 登录成功：{user['name']}（{user['role']}，租户 {user['tenant_id']}）")
    return {"code": 200, "name": user["name"], "role": user["role"]}


@app.post("/logout", summary="退出登录")
async def logout(response: Response):
    """清掉会话 Cookie"""
    clear_session_cookie(response)
    return {"code": 200}


@app.get("/import.html", response_class=FileResponse, summary="文件上传页面")
async def get_import_page():
    """返回前端上传页面（页面本身公开，数据接口才要密钥）"""
    html_abs_path = PROJECT_ROOT / "app/import_process/page/import.html"
    if not os.path.exists(html_abs_path):
        logger.error(f"前端页面文件不存在：{html_abs_path}")
        raise HTTPException(status_code=404, detail="import.html page not found")
    return FileResponse(path=html_abs_path, media_type="text/html")


def run_graph_task(task_id: str, local_dir: str, local_file_path: str, file_hash: str,
                   tenant_id: str = None, resume: bool = False):
    """
    LangGraph 全流程后台任务

    由 BackgroundTasks 触发，不阻塞 HTTP 响应。
    逐节点流式执行图，每完成一个节点就更新任务进度，供前端轮询。
    执行结束后把结果（产品名、切片数）回填到去重记录。

    整个执行过程包在 `usage_context` 里：一次导入 = 一个 trace，导入期间的所有模型调用
    （视觉模型读图、产品名识别、切片嵌入、图谱抽取）都归到这条 trace 与上传者租户上。

    :param task_id: 任务唯一ID（同时决定 LangGraph 的 thread_id：`import_<task_id>`）
    :param local_dir: 该任务的本地工作目录
    :param local_file_path: 上传文件的本地绝对路径
    :param file_hash: 文件SHA-256，用于回填去重记录
    :param tenant_id: 上传者租户，来自访问密钥
    :param resume: True 表示**续跑**（服务重启后接着上次没跑完的图，见 resume_pending_imports）。
        此时不再喂初始状态 —— 状态在检查点里，传 None 让 LangGraph 从最后一个完成的节点继续
    """
    function_name = sys._getframe().f_code.co_name
    thread_id = import_thread_id(task_id)
    update_task_status(task_id, "processing")
    logger.info(
        f"[{task_id}] {'续跑' if resume else '开始执行'}LangGraph全流程，文件：{local_file_path}"
    )

    with usage_context(session_id=task_id, tenant_id=tenant_id, node="import_graph") as acc:
        try:
            if resume:
                # 状态在检查点里：传 None 即「不喂新输入，从最后一个完成的节点继续」
                # （实测：已完成节点不会重跑，MinerU 那一步跑完的就不会被重烧）
                stream_input = None
                # 进度存在共享存储里、跨进程可见，所以上一个进程留下的 `running` 是幽灵
                # （那个节点已经不在跑了）。清掉它，但**保留 `done`** —— 重启后还能看见
                # 已完成到哪一站，正是把进度搬出内存换来的收益
                clear_running_task_list(task_id)
            else:
                # 构造图初始状态
                stream_input = get_default_state()
                stream_input["task_id"] = task_id
                stream_input["local_dir"] = local_dir
                stream_input["local_file_path"] = local_file_path
                # file_hash / tenant_id 一起进 state：续跑时得从检查点读回它们才能收尾
                stream_input["file_hash"] = file_hash
                stream_input["tenant_id"] = tenant_id or ""

            # 流式执行：每完成一个节点就记录，前端轮询可见进度
            for event in get_import_app().stream(stream_input, graph_config(thread_id)):
                for node_name, _node_result in event.items():
                    logger.info(f"[{task_id}] 节点执行完成：{node_name}")
                    add_done_task(task_id, node_name)

            # 收尾用的最终状态**从检查点读**，而不是靠上面的事件拼：
            # 续跑时前面几个节点不会再执行，光靠事件收不到它们的产出（chunks / item_name），
            # 那样会把切片数报成 0、产品名报成空
            final_values = (get_import_app().get_state(graph_config(thread_id)).values or {})

            # 回填导入结果到去重记录
            chunks = final_values.get("chunks") or []
            update_document_result(
                file_hash=file_hash,
                status=STATUS_COMPLETED,
                item_name=final_values.get("item_name") or "",
                chunk_count=len(chunks),
            )
            update_task_status(task_id, "completed")
            logger.info(f"[{task_id}] 全流程执行完毕，入库切片数：{len(chunks)}")

        except Exception as e:
            # 若失败源于检查点写不进去（Mongo 中途挂了），让后续运行改用内存 saver
            note_checkpointer_failure(e)
            # 标记 failed：让用户能重新上传同一文件（去重只拦截 processing/completed）
            update_document_result(file_hash=file_hash, status=STATUS_FAILED)
            update_task_status(task_id, "failed")
            logger.error(f"[{task_id}] 全流程执行失败：{str(e)}", exc_info=True)
            # **放弃这个线程**：否则「重启自动续跑」会把这次失败的任务捡起来重跑 ——
            # 对确定性失败（文件损坏、参数错、节点超时）就是每次重启白烧一遍。
            # 失败已经告诉用户了（去重记录标 failed，可重新上传），留着半成品没有价值
            _abandon_import_thread(thread_id, task_id)

    # 导入的 token 花销远大于一次问答（整篇文档的嵌入 + 图谱抽取），值得单独报一行
    logger.info(f"[{task_id}] 本次导入记账：{acc.text()}")


def _abandon_import_thread(thread_id: str, task_id: str) -> None:
    """
    删掉某个导入线程的检查点 —— 「这次导入失败了，别再自动续跑它」

    与 `resume_pending_imports` 是一对：那边按 `next` 非空捡起未完成的导入重跑，
    这边保证**失败的**不会被反复捡起来。只在失败时调；正常完成的留着（7 天 TTL 清）。

    清理本身失败不影响已经报出去的失败状态，只记一条警告。
    """
    try:
        saver, kind = get_checkpointer()
        if kind != KIND_MONGO:
            return      # 内存版 saver 本来就不跨进程，没有需要清的东西
        saver.delete_thread(thread_id)
        logger.info(f"[{task_id}] 已放弃该导入的检查点（重启不会再重试它）")
    except Exception as e:
        logger.warning(f"[{task_id}] 清理失败导入的检查点失败（忽略）：{e}")


def _pending_import_thread_ids() -> List[str]:
    """
    取出所有**导入**线程的 thread_id（`import_` 前缀）

    直接查 `checkpoints` 集合，而不是走 saver 的 list 接口：这里只要线程名，一条 distinct 就够。
    **给 Mongo 一个 2 秒的短超时** —— 库挂着时不能把服务启动拖住。
    """
    from pymongo import MongoClient

    client = MongoClient(os.getenv("MONGO_URL"), serverSelectionTimeoutMS=2000)
    try:
        db = client[os.getenv("MONGO_DB_NAME")]
        return sorted(db["checkpoints"].distinct("thread_id", {"thread_id": {"$regex": "^import_"}}))
    finally:
        client.close()


def resume_pending_imports() -> None:
    """
    服务启动时，把上次没跑完的导入接着跑完

    未完成的导入**只能从检查点里找**：重启后 `task_utils` 的内存进度、SSE 连接都没了，
    MongoDB 的 `imported_documents` 也只记了个 `status=processing`、不带线程与进度。
    而图上「跑到哪个节点、已经产出哪些 chunks」都在检查点里 —— 所以能接着跑，
    不让那几分钟的 MinerU 白费。

    判据：`import_` 前缀的线程里，快照 `next` **非空**的就是「跑到一半没了」的。
    `invoke/stream(None, config)` 会从最后一个完成的节点继续，**已完成的不重跑**（实测过）。

    **不阻塞启动**：每个未完成的任务丢到一个后台线程里跑（MinerU 要几分钟）。
    """
    function_name = sys._getframe().f_code.co_name
    try:
        _saver, kind = get_checkpointer()
        if kind != KIND_MONGO:
            # 检查点当前是内存降级版 —— 那本来就没有可供续跑的线程（进程一重启就没了）
            logger.warning("[导入续跑] 检查点当前是内存版（Mongo 不可用），跳过续跑扫描")
            return
        threads = _pending_import_thread_ids()
    except Exception as e:
        logger.warning(f"[导入续跑] 扫描未完成的导入失败，跳过：{e}")
        return

    if not threads:
        logger.info("[导入续跑] 没有未完成的导入")
        return

    app = get_import_app()
    for thread_id in threads:
        try:
            snapshot = app.get_state(graph_config(thread_id))
            if not snapshot.next:
                continue        # next 为空 = 已经跑完，跳过
            values = snapshot.values or {}
            task_id = values.get("task_id") or thread_id[len("import_"):]
            logger.warning(
                f"[导入续跑] 发现未完成的导入 {task_id}，下一个节点 {snapshot.next}，接着跑"
            )
            threading.Thread(
                target=run_graph_task,
                args=(
                    task_id,
                    values.get("local_dir") or "",
                    values.get("local_file_path") or "",
                    values.get("file_hash") or "",
                ),
                kwargs={"tenant_id": values.get("tenant_id") or None, "resume": True},
                name=f"resume-{task_id}",
                daemon=True,
            ).start()
        except Exception as e:
            logger.error(f"[导入续跑] 续跑 {thread_id} 失败：{e}", exc_info=True)


def _save_upload_file(file: UploadFile, dest_path: str) -> None:
    """
    把上传文件分块写入磁盘（避免大文件占满内存）
    :param file: FastAPI的UploadFile对象
    :param dest_path: 目标绝对路径
    """
    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer, length=1024 * 1024)


@app.post("/upload", summary="文件上传接口")
async def upload_files(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(..., description="待导入的文件（PDF/MD），支持多选"),
    force: bool = Form(False, description="true表示即使检测到重复也强制重新导入"),
    user: dict = Depends(current_tenant),
):
    """
    文件上传接口（含重复文档检测）

    流程：接收文件 → 存本地并计算哈希 → 查重 → 未重复才启动图

    :return: task_ids（已受理的任务）、duplicates（被跳过的重复文件）
    """
    function_name = sys._getframe().f_code.co_name
    date_based_root_dir = os.path.join(PROJECT_ROOT / "output", datetime.now().strftime("%Y%m%d"))
    task_ids: List[str] = []
    duplicates: List[Dict[str, Any]] = []
    failed_files: List[Dict[str, str]] = []

    logger.info(f"[{function_name}] 收到上传请求，文件数={len(files)}，force={force}")

    for file in files:
        original_name = file.filename or "unnamed"
        ext = os.path.splitext(original_name)[1].lower()

        # 1. 后缀校验
        if ext not in ALLOWED_EXTENSIONS:
            logger.warning(f"[{function_name}] 跳过不支持的文件类型：{original_name}（{ext}）")
            failed_files.append({"filename": original_name, "reason": f"仅支持 {ALLOWED_EXTENSIONS}，当前为 {ext}"})
            continue

        # 2. 先落到临时目录
        task_id = str(uuid.uuid4())
        task_local_dir = os.path.join(date_based_root_dir, task_id)
        os.makedirs(task_local_dir, exist_ok=True)
        local_file_abs_path = os.path.join(task_local_dir, original_name)
        try:
            _save_upload_file(file, local_file_abs_path)
        except Exception as e:
            # 单个文件保存失败只记进失败列表，其余文件继续；编程错误由 degrade 上抛
            degrade(NODE_NAME, f"保存上传文件[{original_name}]", None, e)
            failed_files.append({"filename": original_name, "reason": f"文件保存失败：{e}"})
            continue

        # 3. 大小校验
        file_size = os.path.getsize(local_file_abs_path)
        if file_size > MAX_FILE_SIZE:
            logger.warning(f"[{function_name}] 文件过大：{original_name}（{file_size}字节）")
            failed_files.append({"filename": original_name, "reason": f"文件超过{MAX_FILE_SIZE // 1024 // 1024}MB上限"})
            shutil.rmtree(task_local_dir, ignore_errors=True)
            continue

        # 4. 计算内容哈希并查重
        file_hash = calc_file_hash(local_file_abs_path)
        file_title = os.path.splitext(original_name)[0]
        existing = check_document_exists(file_hash)

        if existing and not force:
            # 命中重复且未强制：清掉刚落的临时文件，记入 duplicates 返回给前端
            logger.warning(f"[{function_name}] 文件已导入过，跳过：{original_name}")
            duplicates.append({
                "filename": original_name,
                "file_title": existing.get("file_title", ""),
                "item_name": existing.get("item_name", ""),
                "chunk_count": existing.get("chunk_count", 0),
                "status": existing.get("status", ""),
                "imported_at": existing.get("imported_at", ""),
            })
            shutil.rmtree(task_local_dir, ignore_errors=True)
            continue

        if existing and force:
            logger.warning(f"[{function_name}] 文件已导入过，但force=true，将重新导入：{original_name}")

        # 5. 标记上传阶段
        add_running_task(task_id, "upload_file")

        # 6. 上传原始文件到 MinIO 做持久化（失败不中断，本地文件仍可处理）
        minio_pdf_base_dir = os.getenv("MINIO_PDF_DIR", "pdf_files")
        minio_object_name = f"{minio_pdf_base_dir}/{datetime.now().strftime('%Y%m%d')}/{original_name}"
        try:
            minio_client = get_minio_client()
            if minio_client is None:
                raise RuntimeError("MinIO客户端不可用")
            minio_client.fput_object(
                bucket_name=os.getenv("MINIO_BUCKET_NAME", "knowledge-base-files"),
                object_name=minio_object_name,
                file_path=local_file_abs_path,
                content_type=file.content_type,
            )
            logger.info(f"[{task_id}] 文件已上传MinIO：{minio_object_name}")
        except Exception as e:
            # MinIO 是"顺手备份"，失败不影响本地处理
            degrade(NODE_NAME, "上传原始文件到 MinIO", None, e)

        add_done_task(task_id, "upload_file")

        # 7. 先写去重记录（processing），使并发上传同一文件时第二个请求被判为重复
        save_document_record(file_hash, file_title, status=STATUS_PROCESSING)

        # 8. 启动后台导入任务
        background_tasks.add_task(
            run_graph_task, task_id, task_local_dir, local_file_abs_path, file_hash,
            user.get("tenant_id"),
        )
        task_ids.append(task_id)
        logger.info(f"[{task_id}] 已加入后台任务队列，文件名：{original_name}")

    return {
        "code": 200,
        "message": f"受理 {len(task_ids)} 个文件，跳过 {len(duplicates)} 个重复文件",
        "task_ids": task_ids,
        "duplicates": duplicates,
        "failed": failed_files,
    }


@app.get("/status/{task_id}", summary="任务状态查询", dependencies=[Depends(current_tenant)])
async def get_task_progress(task_id: str):
    """
    查询单个任务的处理进度（前端每2秒轮询）

    数据来自共享存储（Redis，见 app/utils/task_utils.py）—— 所以**别的进程写的进度也读得到**，
    服务重启后接着跑的那个 `resume_pending_imports` 能看见重启前跑到哪一站。
    读是纯操作：不建 key、不刷新存活时间，否则这个 2 秒一次的轮询会让进度永远回收不掉。
    """
    return {
        "code": 200,
        "task_id": task_id,
        "status": get_task_status(task_id),
        "done_list": get_done_task_list(task_id),
        "running_list": get_running_task_list(task_id),
    }


@app.get("/documents", summary="已导入文档列表", dependencies=[Depends(current_tenant)])
async def list_imported_documents():
    """
    列出已导入的文档（以 Milvus 为准聚合，历史文档也能列出）

    注意：不能只读 SQLite 去重记录，因为命令行导入的文档没有记录。
    """
    from app.utils.document_admin import list_imported_documents as aggregate_documents
    documents = aggregate_documents()
    return {"code": 200, "total": len(documents), "documents": documents}


@app.get("/documents/graph", summary="查询单文档知识图谱", dependencies=[Depends(current_tenant)])
async def get_document_graph(file_title: str):
    """
    读取一份文档在 Neo4j 里的实体-关系图，供前端可视化

    用查询参数而非路径参数：中文文档名会自动 percent-decode，
    也不会与 DELETE /documents/{file_title} 的路由语义纠缠。

    统一返回 HTTP 200 并带 available 字段——Neo4j 不可用或该文档没有图谱时
    available=false，前端只需判这一个字段，不必处理非 2xx。
    """
    function_name = sys._getframe().f_code.co_name
    from app.clients.neo4j_utils import read_doc_graph

    logger.info(f"[{function_name}] 查询图谱：{file_title}")
    result = read_doc_graph(file_title)

    if not result["available"]:
        return {
            "code": 503,
            "available": False,
            "file_title": file_title,
            "message": "Neo4j 不可用，或该文档还没有图谱数据",
            "nodes": [],
            "edges": [],
            "stats": result["stats"],
        }

    return {
        "code": 200,
        "available": True,
        "file_title": file_title,
        "nodes": result["nodes"],
        "edges": result["edges"],
        "stats": result["stats"],
    }


@app.delete("/documents/{file_title}", summary="撤回已导入文档", dependencies=[Depends(current_tenant)])
async def revoke_document_api(file_title: str, confirm: bool = False):
    """
    撤回一份已导入文档：删除 Milvus 切片与产品名、SQLite 去重记录、本地产物、MinIO 对象

    删除不可逆。默认 confirm=false，只返回将被删除的内容清单供前端二次确认；
    显式传 confirm=true 才真正执行。
    """
    function_name = sys._getframe().f_code.co_name
    from app.utils.document_admin import locate_local_artifacts, revoke_document

    targets = locate_local_artifacts(file_title)

    # 未确认：只预览将要删除的内容，不执行
    if not confirm:
        logger.info(f"[{function_name}] 撤回预览（未执行）：{file_title}")
        return {
            "code": 200,
            "confirmed": False,
            "file_title": file_title,
            "preview": {
                "local_paths": targets,
                "note": "以上为本地产物；Milvus 与 MinIO 中的相关数据也将在确认后一并删除",
            },
        }

    # 已确认：执行撤回
    result = revoke_document(file_title)
    if not result.get("success"):
        logger.error(f"[{function_name}] 撤回存在失败项：{result}")
    return {"code": 200, "confirmed": True, "result": result}


if __name__ == "__main__":
    """
    服务启动入口
    注意：8000端口已被Attu（Milvus可视化）占用，本服务使用8001
    """
    logger.info("File Import Service 服务启动中（端口8001）...")
    uvicorn.run(app=app, host="127.0.0.1", port=8001)
