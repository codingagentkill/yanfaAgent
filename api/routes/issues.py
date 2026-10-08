"""api/routes/issues.py —— 提交 Issue 的业务路由"""
import asyncio
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from gateway.auth import require_api_key
from channels.base import InboundMessage
from channels.handler import handle_message

from agent.main import agent_session
from api.schemas import IssueRequest, IssueResponse, ReviewRequest, ReviewResponse
from api.deps import get_checkpointer, get_pool, get_store
from api.review import commit_and_push, create_pending, get_pending, pop_pending
from infra.logging import get_logger
from infra.settings import get_settings

logger = get_logger()

router = APIRouter(prefix="/issues", tags=["issues"])


def clone_repo(repo_url: str, branch: str | None = None) -> Path:
    """克隆远程仓库到临时目录，返回本地路径（改动会落在这里，供用户 review/推送）。"""
    dest = Path(tempfile.gettempdir()) / "yanfa_repos" / uuid4().hex[:12]
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo_url, str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"克隆仓库失败：{proc.stderr.strip()}")
    return dest


@router.post("", response_model=IssueResponse)
async def submit_issue(
    req: IssueRequest,
    checkpointer=Depends(get_checkpointer),   # 注入 lifespan 建好的共享资源
    store=Depends(get_store),
    pool=Depends(get_pool),
) -> IssueResponse:
    # thread_id：客户端传了就用它（续聊/断点续跑），没传就新建
    thread_id = req.thread_id or uuid4().hex

    # 确定目标仓库：repo_url 优先（clone 到临时目录），否则 repo_path，否则默认本项目自己
    repo_path = req.repo_path
    if req.repo_url:
        repo_path = str(await asyncio.to_thread(clone_repo, req.repo_url, req.branch))

    # 构建 agent（轻组装），传入共享的持久化资源
    async with agent_session(
            thread_id=thread_id,
            checkpointer=checkpointer,
            store=store,
            user_id=req.user_id,
            channel=req.channel,
            repo_path=repo_path,
    ) as agent:
        # 异步调用，带 thread_id —— 同一 thread_id 下次请求会带着历史上下文继续
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": req.issue}]},
            config={"configurable": {"thread_id": thread_id}},
        )

    reply = result["messages"][-1].content if result.get("messages") else ""

    # RAG：把这次任务的 issue + 解法记进经验库（供后续任务检索参考）
    if get_settings().rag_enabled:
        from infra.rag import record_experience
        try:
            await asyncio.to_thread(record_experience, req.issue, reply, repo_path or "")
        except Exception as e:  # noqa: BLE001 记录失败不影响主流程
            logger.warning("记录历史经验失败：{}", e)

    review_id = await create_pending(pool, repo_path, req.issue) if repo_path else None
    return IssueResponse(thread_id=thread_id, reply=reply, repo_path=repo_path, review_id=review_id)


@router.post("/approve", response_model=ReviewResponse)
async def approve_changes(req: ReviewRequest, pool=Depends(get_pool)) -> ReviewResponse:
    """人工审批通过后，执行 git add / commit / push。"""
    pending = await get_pending(pool, req.review_id)
    if pending is None:
        return ReviewResponse(status="error", detail="review_id 不存在或已处理")
    try:
        result = await asyncio.to_thread(commit_and_push, pending["repo_path"], pending["issue"])
        await pop_pending(pool, req.review_id)
        return ReviewResponse(status="success", detail=f"sha={result['sha']} | {result['push_output']}")
    except Exception as e:  # noqa: BLE001 审批失败原因要如实返回
        return ReviewResponse(status="error", detail=str(e))


@router.post("/reject", response_model=ReviewResponse)
async def reject_changes(req: ReviewRequest, pool=Depends(get_pool)) -> ReviewResponse:
    """人工审批拒绝，丢弃待审批记录（改动保留在工作区，由用户自行回退）。"""
    pending = await pop_pending(pool, req.review_id)
    if pending is None:
        return ReviewResponse(status="error", detail="review_id 不存在或已处理")
    return ReviewResponse(status="success", detail="已丢弃待审批记录，改动仍保留在工作区")


@router.post("/secure")
async def submit_issue_secure(
    req: IssueRequest,
    request: Request,
    tenant_id: str = Depends(require_api_key),   # ← 鉴权返回租户 id
):
    """需要 gateway-API-Key 的端点；租户身份贯穿到会话隔离。"""
    inbound = InboundMessage(
        channel=req.channel,
        user_id=req.user_id,
        text=req.issue,
        conversation_id=req.thread_id or req.user_id,
    )
    reply = await handle_message(
        inbound,
        request.app.state.checkpointer,
        request.app.state.store,
        tenant_id=tenant_id,                     # ← 一路传到 thread 派生，按租户隔离
    )
    return {"reply": reply, "tenant": tenant_id}





# # 通用模式：把同步阻塞调用包成不阻塞事件循环的协程
# import asyncio
#
# def some_blocking_sync_call(x):     # 假设这是个只有同步接口的库
#     ...
#
# async def use_it(x):
#     # ❌ 直接 await 不了（它不是协程）；直接调会阻塞事件循环
#     # ✅ 丢到线程池，事件循环不被卡住
#     result = await asyncio.to_thread(some_blocking_sync_call, x)
#     return result



