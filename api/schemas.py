
"""api/schemas.py —— 对外 HTTP 契约（Pydantic v2）"""
from pydantic import BaseModel, Field


class IssueRequest(BaseModel):
    """提交一个开发 Issue。"""
    issue: str = Field(..., min_length=1, description="要 DevMate 处理的开发任务描述")
    user_id: str = Field("anonymous", description="提交者标识")
    channel: str = Field("api", description="来源渠道（api/cli/web/feishu…）")
    thread_id: str | None = Field(
        None, description="会话线程 ID；传入相同 thread_id 可在同一对话上下文继续（断点续跑）"
    )
    repo_path: str | None = Field(
        None, description="目标仓库本地路径；不传则默认修改本项目自己（yanfaAgent）"
    )
    repo_url: str | None = Field(
        None, description="目标仓库远程 URL；传了会自动 clone 到临时目录并作为开发目标"
    )
    branch: str | None = Field(
        None, description="目标分支；配合 repo_url 使用"
    )


class IssueResponse(BaseModel):
    """Issue 处理结果。"""
    thread_id: str = Field(..., description="本次会话的线程 ID（下次带上它可续聊）")
    reply: str = Field(..., description="DevMate 的最终回复")
    approved: bool | None = Field(None, description="若有 reviewer 审查结论，是否通过")
    repo_path: str | None = Field(None, description="改动落地的本地仓库路径（指定 repo_path/repo_url 时返回）")
    review_id: str | None = Field(None, description="待审批的 review_id（指定仓库时返回，供 approve/reject 端点用）")


class ReviewRequest(BaseModel):
    """审批请求。"""
    review_id: str = Field(..., description="待审批改动的 review_id")


class ReviewResponse(BaseModel):
    """审批结果。"""
    status: str = Field(..., description="success / error")
    detail: str | None = Field(None, description="结果说明（commit sha、push 输出或错误信息）")


class HealthResponse(BaseModel):
    status: str
    detail: str | None = None