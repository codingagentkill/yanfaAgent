"""api/review.py —— 改动的人工审批：pending 记录（PostgreSQL）+ commit/push。

Agent 改完代码后先不 commit/push，而是登记一条待审批记录（落 PostgreSQL，重启不丢）并
返回 review_id；用户 review 后调用 approve 端点才真正 commit + push（拒绝则删除记录）。
"""
from __future__ import annotations

import subprocess
from uuid import uuid4


async def setup_pending_table(pool) -> None:
    """建 pending_reviews 表（幂等，首次启动时调用）。"""
    async with pool.connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_reviews (
                review_id   TEXT PRIMARY KEY,
                repo_path   TEXT NOT NULL,
                issue       TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


async def create_pending(pool, repo_path: str, issue: str) -> str:
    """登记一条待审批的改动，返回 review_id。"""
    review_id = uuid4().hex
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO pending_reviews (review_id, repo_path, issue) VALUES (%s, %s, %s)",
            (review_id, repo_path, issue),
        )
    return review_id


async def get_pending(pool, review_id: str) -> dict | None:
    """取出待审批记录（不删除）。"""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT repo_path, issue FROM pending_reviews WHERE review_id = %s",
            (review_id,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def pop_pending(pool, review_id: str) -> dict | None:
    """取出并删除待审批记录。"""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT repo_path, issue FROM pending_reviews WHERE review_id = %s",
            (review_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        await conn.execute("DELETE FROM pending_reviews WHERE review_id = %s", (review_id,))
    return dict(row)


def commit_and_push(repo_path: str, message: str) -> dict:
    """在 repo_path 上执行 git add -A / commit / push，返回结果 dict。

    依赖宿主机已配置好 git 凭据（SSH key / token），push 用宿主机的身份执行。
    """
    def run(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(args, capture_output=True, text=True, cwd=repo_path)
        return proc.returncode, (proc.stdout or proc.stderr).strip()

    rc, out = run(["git", "add", "-A"])
    if rc != 0:
        raise RuntimeError(f"git add 失败：{out}")

    rc, out = run(["git", "commit", "-m", message])
    if rc != 0 and "nothing to commit" not in out:
        raise RuntimeError(f"git commit 失败：{out}")

    rc, out = run(["git", "push"])
    if rc != 0:
        # 本地仓库的分支可能没配 upstream（例如直接给 repo_path 而非 clone 的场景），
        # 退回显式推送 origin 的当前分支。
        rc, out = run(["git", "push", "origin", "HEAD"])
        if rc != 0:
            raise RuntimeError(f"git push 失败：{out}")

    rc, sha = run(["git", "rev-parse", "HEAD"])
    return {"sha": sha if rc == 0 else "", "push_output": out}
