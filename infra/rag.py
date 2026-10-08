"""infra/rag.py —— 代码库 RAG：embedding + pgvector 语义检索。

把目标仓库的代码/文档切块、embedding 后存进 PGVector，给 Agent 挂一个检索工具，
让 Researcher 子代理能语义检索相关代码（而非靠文件名/关键字硬找）。

技术要点：
- embedding 用 DashScope 的 text-embedding-v3（走 OpenAI 兼容端点）；
- 向量库用 pgvector（langchain-postgres 的 PGVector），持久化、跨副本共享；
- 检索工具通过 langchain 的 @tool 暴露给 Agent，跑在宿主进程（检索），文件操作仍在沙箱。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.tools import tool
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import OpenAI

from infra.settings import get_settings
from infra.logging import get_logger

logger = get_logger()

_EXCLUDED_DIRS = {
    ".git", ".venv", "__pycache__", "node_modules",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
}
_CODE_SUFFIXES = {
    ".py", ".cpp", ".h", ".hpp", ".cc", ".md", ".proto", ".txt",
    ".js", ".ts", ".go", ".java", ".rs", ".json", ".yaml", ".yml", ".toml",
}


class DashScopeEmbeddings(Embeddings):
    """直接调 DashScope 兼容端点的 embedding。

    为什么不用 langchain_openai.OpenAIEmbeddings：它在 DashScope 兼容端点上会触发
    input.contents 格式报错（封装层兼容问题），底层 openai 客户端反而正常。
    """

    def __init__(self, model: str, api_key: str, base_url: str) -> None:
        self._model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in resp.data]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def get_embeddings() -> DashScopeEmbeddings:
    """DashScope 的 embedding 模型（OpenAI 兼容端点）。"""
    s = get_settings()
    return DashScopeEmbeddings(
        model=s.embedding_model,
        api_key=s.api_key.get_secret_value(),
        base_url=s.base_url,
    )


def collection_for(repo_path: str) -> str:
    """按仓库路径派生一个唯一 collection 名，避免不同仓库的向量混在一起。"""
    s = get_settings()
    digest = hashlib.md5(str(repo_path).encode()).hexdigest()[:8]
    return f"{s.rag_collection}_{digest}"


def get_vector_store(collection: str) -> PGVector:
    """构建 PGVector 向量库实例。"""
    s = get_settings()
    return PGVector(
        embeddings=get_embeddings(),
        collection_name=collection,
        connection=s.database_url,
        use_jsonb=True,
    )


def _collect_docs(root: Path) -> list[Document]:
    """收集仓库里的文本文件，切块成 Document（带 source 元数据）。"""
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
    docs: list[Document] = []
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        parts = fp.relative_to(root).parts
        if any(p in _EXCLUDED_DIRS or p.startswith(".") for p in parts):
            continue
        if fp.suffix.lower() not in _CODE_SUFFIXES:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not text.strip():
            continue
        source = fp.relative_to(root).as_posix()
        docs.extend(splitter.create_documents([text], metadatas=[{"source": source}]))
    return docs


def index_repo(repo_path: str, collection: str) -> int:
    """把仓库索引进向量库，返回索引的文档块数。"""
    docs = _collect_docs(Path(repo_path))
    if not docs:
        logger.warning("RAG：仓库 {} 没有可索引的文本文件", repo_path)
        return 0
    get_vector_store(collection).add_documents(docs)
    logger.info("RAG：已索引 {} 个代码块进 collection {}", len(docs), collection)
    return len(docs)


def build_retrieve_tool(collection: str):
    """返回一个供 Agent 调用的语义检索工具。"""
    store = get_vector_store(collection)

    @tool
    def retrieve_code(query: str) -> str:
        """在目标代码库里语义检索与 query 最相关的代码/文档片段，用于定位代码、了解现状或确认 API 用法。"""
        docs = store.similarity_search(query, k=4)
        if not docs:
            return "未检索到相关内容。"
        return "\n\n---\n\n".join(
            f"[{d.metadata.get('source', '?')}]\n{d.page_content}" for d in docs
        )

    return retrieve_code


def experience_collection() -> str:
    """历史经验的 collection 名（全局共享，跨仓库复用经验）。"""
    return get_settings().rag_experience_collection


def record_experience(issue: str, solution: str, repo_path: str = "") -> None:
    """把一次任务的「issue + 解法」存进经验库（只 embedding issue 文本，解法放元数据）。"""
    store = get_vector_store(experience_collection())
    store.add_texts(
        [issue],
        metadatas=[{"solution": solution[:2000], "repo_path": repo_path}],
    )
    logger.info("RAG：已记录一条历史经验（issue 长度 {}）", len(issue))


def build_experience_tool():
    """返回一个检索历史经验的工具：相似 issue → 当时解法。"""
    store = get_vector_store(experience_collection())

    @tool
    def retrieve_experience(query: str) -> str:
        """检索历史研发任务经验，返回与 query 语义相似的历史 issue 及其当时的解决方案，用于参考以前类似问题是怎么解决的。"""
        docs = store.similarity_search(query, k=3)
        if not docs:
            return "暂无相关历史经验。"
        return "\n\n---\n\n".join(
            f"【历史任务】{d.page_content}\n【当时解法】{d.metadata.get('solution', '无')}"
            for d in docs
        )

    return retrieve_experience
