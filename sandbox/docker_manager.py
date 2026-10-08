"""sandbox/docker_manager.py —— 加固容器的创建/seed/销毁

runtime 参数是隔离级别开关：runc(加固容器) / runsc(gVisor) / kata(microVM)。
换 runtime 就换隔离级别，DockerSandbox 代码不变。
这里用 asyncio.create_subprocess_exec 管容器生命周期(应用自己的异步逻辑)。
"""
from __future__ import annotations

import asyncio
import io
import os
import tarfile
import uuid
from pathlib import Path

from infra.settings import get_settings
from infra.logging import get_logger
from sandbox.docker_sandbox import DockerSandbox
import asyncio
import subprocess

import time
from obs.metrics import SANDBOX_CREATE_DURATION


logger = get_logger()


async def _run(cmd: list[str]) -> tuple[int, str]:
    def _sync_run():
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        output = result.stdout or result.stderr
        return result.returncode, output.strip()

    return await asyncio.to_thread(_sync_run)



async def create_one_sandbox() -> DockerSandbox:
    """以加固参数起一个常驻容器，返回 DockerSandbox。"""

    start = time.perf_counter()
    s = get_settings()
    name = f"ivc-sbx-{uuid.uuid4().hex[:12]}"
    workdir = s.sandbox_workdir

    # tmpfs 工作目录：可写 + 可执行 + 限大小 + 属主对齐非 root 用户(uid=1000)
    tmpfs_opt = f"{workdir}:rw,exec,size=512m,uid=1000"

    code, out = await _run([
        "docker", "run", "-d", "--name", name,
        "--runtime", s.sandbox_runtime,             # ← 换它即换隔离级别
        "--network", "none",                        # 断网
        "--cap-drop", "ALL",                        # 丢能力
        "--security-opt", "no-new-privileges",      # 禁提权
        "--read-only",                              # 只读根
        "--tmpfs", tmpfs_opt,                       # 唯一可写区
        "--tmpfs", "/tmp:rw,exec,size=128m,uid=1000",  # 额外给 /tmp 可写(部分工具需要)
        "--memory", s.sandbox_mem_limit,
        "--memory-swap", s.sandbox_mem_limit,
        "--pids-limit", str(s.sandbox_pids_limit),
        "--cpus", s.sandbox_cpus,
        "--user", "1000:1000",                      # 非 root
        "-w", workdir,
        s.sandbox_image, "sleep", "infinity",       # 常驻，等 docker exec
    ])
    if code != 0:
        raise RuntimeError(f"起沙箱容器失败：{out}")
    logger.info("加固容器已起：{}（runtime={}）", name, s.sandbox_runtime)
    SANDBOX_CREATE_DURATION.observe(time.perf_counter() - start)
    return DockerSandbox(container_id=name, workdir=workdir)



# sandbox/docker_manager.py（追加：只读 reviewer 用的沙箱）
async def create_readonly_sandbox(source_sandbox: DockerSandbox) -> DockerSandbox:
    """
    给 reviewer 起一个只读容器：把 source_sandbox 的工作目录内容复制进来后设为只读。
    实现：① 起一个新容器；② 从源容器把代码 cp 出来再 cp 进新容器；
         ③ 在新容器内 chmod -R a-w 工作目录,使 execute 跑 echo>file 也写不进。
    这样 reviewer 即使用 execute 也改不了代码——靠文件系统权限,不靠 prompt。
    """
    import tempfile
    s = get_settings()
    name = f"ivc-ro-{uuid.uuid4().hex[:12]}"
    workdir = s.sandbox_workdir
    code, out = await _run([
        "docker", "run", "-d", "--name", name,
        "--runtime", s.sandbox_runtime,
        "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--read-only",
        "--tmpfs", f"{workdir}:rw,exec,size=512m,uid=1000",
        "--tmpfs", "/tmp:rw,exec,size=128m,uid=1000",
        "--memory", s.sandbox_mem_limit, "--memory-swap", s.sandbox_mem_limit,
        "--pids-limit", str(s.sandbox_pids_limit), "--cpus", s.sandbox_cpus,
        "--user", "1000:1000", "-w", workdir,
        s.sandbox_image, "sleep", "infinity",
    ])
    if code != 0:
        raise RuntimeError(f"起只读沙箱失败：{out}")
    ro = DockerSandbox(container_id=name, workdir=workdir)
    # 把源容器代码搬进来(经宿主中转),然后把工作目录设为不可写
    # 把源容器代码搬出来，再通过 upload_files 写进 reviewer 容器

    import subprocess

    # 源沙箱工作目录 → 打成 tar 数据
    proc = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", source_sandbox.container_id,"tar", "-C", workdir, "-cf", "-", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"导出源沙箱代码失败：{proc.stderr.decode(errors='replace')}")

    # tar 数据 → reviewer 沙箱工作目录
    proc2 = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", "-i", name, "tar", "-C", workdir, "-xf", "-"],
        input=proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc2.returncode != 0:
        raise RuntimeError(f"写入 reviewer 沙箱失败：{proc2.stderr.decode(errors='replace')}")


    # chmod 去掉写位(用 root 改,因为非 root 改不动自己没权限的位)
    code, out = await _run(["docker", "exec","-u", "1000:1000",name,"chmod", "-R", "a-w",workdir])
    if code != 0:
        raise RuntimeError(f"设置 reviewer 只读失败：{out}")
    logger.info("只读 reviewer 沙箱已起：{}", name)
    return ro


async def seed_project(
    sandbox: DockerSandbox,
    root: Path | None = None,
) -> None:
    """把目标仓库（root）的代码 seed 进沙箱工作目录，并额外 seed 框架 skills/AGENTS.md。

    - root 是要开发的目标仓库本地路径；不传则默认本项目自己（yanfaAgent）。
    - seed 整棵树（剪枝跳过 .git/.venv/__pycache__ 等垃圾目录和所有隐藏文件/目录），
      让 Agent 能看到目标仓库的真实结构，而不是写死的 app/ tests/。
    - 框架的 skills/ 与 AGENTS.md 始终额外 seed 进沙箱，保证 Agent 有 TDD/调试等能力。
    """
    framework_root = Path(__file__).resolve().parent.parent
    root = root or framework_root
    workdir = sandbox.workdir

    excluded_dirs = {
        ".git", ".venv", "__pycache__", "node_modules",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    }
    excluded_suffixes = {".pyc", ".pyo", ".DS_Store"}

    files_to_upload: list[tuple[str, bytes, int]] = []  # (相对路径, 内容, mode)

    # ① 目标仓库整棵树（剪枝跳过垃圾/隐藏目录，记录 mode 以保留可执行位）
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in excluded_dirs and not d.startswith(".")
        ]
        for name in filenames:
            if name.startswith(".") or os.path.splitext(name)[1] in excluded_suffixes:
                continue
            fp = Path(dirpath) / name
            files_to_upload.append((fp.relative_to(root).as_posix(), fp.read_bytes(), fp.stat().st_mode))

    # ② 框架 skills + AGENTS.md（Agent 的能力与约束，始终带上，覆盖目标同名文件）
    for fp in (framework_root / "skills").rglob("*"):
        if fp.is_file():
            files_to_upload.append((fp.relative_to(framework_root).as_posix(), fp.read_bytes(), fp.stat().st_mode))
    agents_md = framework_root / "AGENTS.md"
    if agents_md.is_file():
        files_to_upload.append(("AGENTS.md", agents_md.read_bytes(), agents_md.stat().st_mode))

    # ③ 打包成 tar（保留 mode，避免 cat > 丢可执行位），一次性流式注入容器
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, content, mode in files_to_upload:
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            info.mode = mode
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(content))
    tar_bytes = buf.getvalue()

    proc = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", "-i", sandbox.container_id, "tar", "-C", workdir, "-xf", "-"],
        input=tar_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"seed 项目失败：{proc.stderr.decode(errors='replace')}")
    logger.info("项目已 seed 进容器 {}（{} 个文件）", sandbox.container_id, len(files_to_upload))




async def destroy_sandbox(sandbox: DockerSandbox) -> None:
    """销毁容器（用完即弃）。tmpfs 工作目录随容器一起消失，不残留。"""
    await _run(["docker", "rm", "-f", sandbox.container_id])
    logger.info("容器已销毁：{}", sandbox.container_id)


async def export_sandbox_changes(
    sandbox: DockerSandbox,
    root: Path | None = None,
) -> None:
    """请求结束前，把沙箱里 agent 改动的文件反向拷回宿主，便于 git diff 查看。

    沙箱里 seed 的是目标仓库整棵树 + 框架 skills/AGENTS.md；agent 的改动只发生在沙箱里，
    正常流程沙箱销毁后改动会随 tmpfs 一起消失。这里在销毁前把改动（排除框架的
    skills/AGENTS.md）拷回目标仓库，让用户能在宿主直接看到 agent 实际改了什么。

    注意：不能用 docker cp —— 在 --read-only + tmpfs 的加固容器上，docker cp 找不到
    tmpfs 挂载里的文件（报 Could not find the file）。这里用 docker exec tar 流式导出
    到宿主再解包（与 create_readonly_sandbox 同款做法，已实测可行）。
    """
    root = root or Path(__file__).resolve().parent.parent
    # ① 在容器里 tar 出整个工作目录（排除框架的 skills/AGENTS.md）
    tar_proc = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", sandbox.container_id, "tar", "-C", sandbox.workdir,
         "-cf", "-", "--exclude=./skills", "--exclude=./AGENTS.md",
         "--exclude=./.pycache", "--exclude=./.pytest_cache", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if tar_proc.returncode != 0:
        logger.warning("导出沙箱改动失败：{}", tar_proc.stderr.decode(errors="replace"))
        return
    # ② 在宿主解包到目标仓库根目录（合并/覆盖同名文件）
    untar = await asyncio.to_thread(
        subprocess.run,
        ["tar", "-C", str(root), "-xf", "-"],
        input=tar_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if untar.returncode != 0:
        logger.warning("回写宿主失败：{}", untar.stderr.decode(errors="replace"))
    else:
        logger.info("已把沙箱改动导出到宿主：{}", root)

