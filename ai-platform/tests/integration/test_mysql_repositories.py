"""MySQL KB / 文档 / 切片仓储的集成测试（``docs/09`` §2.1~2.3）。

这一层要证明的是「SQL 写对了」，具体是四类**内存实现证明不了**的事：

1. **行值游标** ``(created_at, id) < (...)`` 在真库上是否真的按预期过滤
   （写成 ``a AND b OR c`` 那种形式在真库上会静默多返回，而在内存实现里
   因为写法完全不同，根本不会暴露）。
2. **唯一键冲突**是否被映射成业务错误（``409``）而不是 500 —— 靠 ``uk_kb_user_name``
   与 ``uk_doc_dedupe`` 两张约束真实触发。
3. **软删过滤**：``deleted_at IS NULL`` 与生成列 ``deleted_key`` 的配合，
   尤其是「同名可以重建」这条（``docs/09`` §2.1 的生成列就是为了它）。
4. **JSON 列**往返：``metadata`` 存进去再读出来必须是同一个 dict。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from tests.conftest import build_settings

from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.memory.context_store import now_iso
from app.storage.base import Chunk, Document, KnowledgeBase
from app.storage.memory import encode_created_cursor, encode_index_cursor

pytestmark = pytest.mark.usefixtures("mysql_dsn")


def _sha(seed: str) -> str:
    """定长 64 位十六进制（列是 ``CHAR(64)``，写短的会被空格补齐）。"""
    return hashlib.sha256(seed.encode()).hexdigest()


def _kb(user_id: str, **overrides: Any) -> KnowledgeBase:
    moment = now_iso()
    values: dict[str, Any] = {
        "id": new_id("kb"),
        "user_id": user_id,
        "name": f"kb-{new_id('kb')[-6:]}",
        "created_at": moment,
        "updated_at": moment,
    }
    values.update(overrides)
    return KnowledgeBase(**values)


def _doc(kb: KnowledgeBase, **overrides: Any) -> Document:
    moment = now_iso()
    values: dict[str, Any] = {
        "id": new_id("doc"),
        "kb_id": kb.id,
        "user_id": kb.user_id,
        "doc_name": "handbook.md",
        "file_ext": "md",
        "mime_type": "text/markdown",
        "size_bytes": 128,
        "object_key": f"kb/{kb.id}/{new_id('doc')}.md",
        "content_sha256": _sha(new_id("doc")),
        "created_at": moment,
        "updated_at": moment,
    }
    values.update(overrides)
    return Document(**values)


def _chunk(doc: Document, index: int, **overrides: Any) -> Chunk:
    content = f"第 {index} 段正文"
    values: dict[str, Any] = {
        "id": new_id("chk"),
        "doc_id": doc.id,
        "kb_id": doc.kb_id,
        "user_id": doc.user_id,
        "chunk_index": index,
        "content": content,
        "content_sha256": f"{index:064d}",
        "char_start": index * 10,
        "char_end": index * 10 + len(content),
        "token_count": 4,
        "metadata": {"doc_name": doc.doc_name, "chunk_size": 512, "merged": False},
        "created_at": now_iso(),
    }
    values.update(overrides)
    return Chunk(**values)


# ---------------------------------------------------------------------------
# 门面自检
# ---------------------------------------------------------------------------


async def test_missing_tables_is_empty_on_a_migrated_database(rag_repos: Any) -> None:
    """建表脚本跑过之后 ``missing_tables()`` 必须为空。

    这条用例的价值不是「SQL 对不对」，而是**部署自检的判据**：它为空才说明
    001/002/003 三个脚本都跑过了。PR 里改了表结构却忘了写迁移时，
    这里会先红，而不是等到某个接口 503。
    """
    assert await rag_repos.missing_tables() == []
    await rag_repos.ping()


# ---------------------------------------------------------------------------
# knowledge_base
# ---------------------------------------------------------------------------


async def test_kb_roundtrip_preserves_every_column(rag_repos: Any, cleanup_user: str) -> None:
    """写入 → 读出，所有列逐一对齐（含 JSON 元数据与计数列）。"""
    kb = _kb(cleanup_user, description="手册库", chunk_size=256, metadata={"owner": "qa"})
    await rag_repos.knowledge_bases.add(kb)

    loaded = await rag_repos.knowledge_bases.get(kb.id, cleanup_user)
    assert loaded.name == kb.name
    assert loaded.description == "手册库"
    assert loaded.chunk_size == 256
    assert loaded.metadata == {"owner": "qa"}
    # 时间戳格式必须与内存后端**完全一致**（2026-09-28T16:05:26.866Z），
    # 否则契约测试通过、前端解析真机数据时失败
    assert loaded.created_at == kb.created_at
    assert loaded.created_at.endswith("Z")


async def test_duplicate_kb_name_returns_conflict(rag_repos: Any, cleanup_user: str) -> None:
    """同名 KB → ``409``（唯一键 ``uk_kb_user_name`` 的真实触发）。"""
    name = f"dup-{new_id('kb')[-6:]}"
    await rag_repos.knowledge_bases.add(_kb(cleanup_user, name=name))

    with pytest.raises(AppError) as excinfo:
        await rag_repos.knowledge_bases.add(_kb(cleanup_user, name=name))
    assert excinfo.value.code is ErrorCode.KB_NAME_CONFLICT


async def test_name_can_be_reused_after_soft_delete(rag_repos: Any, cleanup_user: str) -> None:
    """软删后同名可重建 —— 生成列 ``deleted_key`` 的存在意义（``docs/09`` §2.1）。"""
    name = f"reuse-{new_id('kb')[-6:]}"
    first = await rag_repos.knowledge_bases.add(_kb(cleanup_user, name=name))
    await rag_repos.knowledge_bases.soft_delete(first.id, cleanup_user)

    second = await rag_repos.knowledge_bases.add(_kb(cleanup_user, name=name))
    assert second.id != first.id
    # 已删的那条不可见（``deleted_at IS NULL`` 过滤生效）
    with pytest.raises(AppError) as excinfo:
        await rag_repos.knowledge_bases.get(first.id, cleanup_user)
    assert excinfo.value.code is ErrorCode.KB_NOT_FOUND


async def test_list_is_scoped_to_user_and_paginates_with_cursor(
    rag_repos: Any, cleanup_user: str
) -> None:
    """列表按 ``(created_at, id)`` 倒序 + 游标翻页（行值比较）。"""
    created = []
    for _ in range(3):
        created.append(await rag_repos.knowledge_bases.add(_kb(cleanup_user)))

    page1, has_more = await rag_repos.knowledge_bases.list(cleanup_user, limit=2)
    assert len(page1) == 2
    assert has_more is True
    cursor = encode_created_cursor(page1[-1].created_at, page1[-1].id)
    page2, has_more2 = await rag_repos.knowledge_bases.list(cleanup_user, limit=2, cursor=cursor)
    ids = {item.id for item in page1} | {item.id for item in page2}
    assert len(page2) == 1
    assert has_more2 is False
    assert {item.id for item in created} == ids


async def test_get_does_not_leak_across_users(rag_repos: Any, cleanup_user: str) -> None:
    """跨用户读 → ``404``（不是 403：403 会确认「这个 ID 存在」）。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    with pytest.raises(AppError) as excinfo:
        await rag_repos.knowledge_bases.get(kb.id, new_id("u"))
    assert excinfo.value.code is ErrorCode.KB_NOT_FOUND


async def test_count_reflects_live_rows_only(rag_repos: Any, cleanup_user: str) -> None:
    """计数只算未删除的（软删的 KB 不能继续占配额）。"""
    assert await rag_repos.knowledge_bases.count(cleanup_user) == 0
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    assert await rag_repos.knowledge_bases.count(cleanup_user) == 1
    await rag_repos.knowledge_bases.soft_delete(kb.id, cleanup_user)
    assert await rag_repos.knowledge_bases.count(cleanup_user) == 0


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------


async def test_document_dedupe_by_sha256(rag_repos: Any, cleanup_user: str) -> None:
    """同 KB 同内容 → ``409 DOCUMENT_DUPLICATE``（``uk_doc_dedupe``）。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    sha = _sha(new_id("doc"))
    await rag_repos.documents.add(_doc(kb, content_sha256=sha))

    with pytest.raises(AppError) as excinfo:
        await rag_repos.documents.add(_doc(kb, content_sha256=sha))
    assert excinfo.value.code is ErrorCode.DOCUMENT_DUPLICATE

    found = await rag_repos.documents.find_by_sha256(kb.id, sha)
    assert found is not None and found.content_sha256 == sha


async def test_document_save_ignores_id_and_created_at(rag_repos: Any, cleanup_user: str) -> None:
    """``save`` 不能改主键与创建时间（不可变列的更新会破坏游标分页的稳定性）。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    doc = await rag_repos.documents.add(_doc(kb))

    doc.status = "INDEXED"
    doc.chunk_count = 7
    doc.metadata = {"pages": 2}
    saved = await rag_repos.documents.save(doc)

    assert saved.status == "INDEXED"
    assert saved.chunk_count == 7
    assert saved.metadata == {"pages": 2}
    assert saved.id == doc.id
    assert saved.created_at == doc.created_at
    assert saved.updated_at != ""


async def test_live_documents_excludes_deleted(rag_repos: Any, cleanup_user: str) -> None:
    """``live_documents`` 供重建计数用，必须排除已软删的。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    keep = await rag_repos.documents.add(_doc(kb))
    gone = await rag_repos.documents.add(_doc(kb))
    await rag_repos.documents.soft_delete(gone.id, cleanup_user)

    live = await rag_repos.documents.live_documents(kb.id)
    assert {item.id for item in live} == {keep.id}
    assert await rag_repos.documents.count_in_kb(kb.id) == 1


# ---------------------------------------------------------------------------
# document_chunk
# ---------------------------------------------------------------------------


async def test_replace_for_document_is_atomic_and_idempotent(
    rag_repos: Any, cleanup_user: str
) -> None:
    """重建切片：旧的全删、新的一次写入；重复执行结果不变。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    doc = await rag_repos.documents.add(_doc(kb))

    await rag_repos.chunks.replace_for_document(doc.id, [_chunk(doc, i) for i in range(3)])
    assert await rag_repos.chunks.count_for_document(doc.id) == 3

    # 同一批再写一次：不是「6 条」也不是唯一键报错
    await rag_repos.chunks.replace_for_document(doc.id, [_chunk(doc, i) for i in range(3)])
    assert await rag_repos.chunks.count_for_document(doc.id) == 3

    # 换一批（更少）：旧的必须消失
    await rag_repos.chunks.replace_for_document(doc.id, [_chunk(doc, 0)])
    assert await rag_repos.chunks.count_for_document(doc.id) == 1


async def test_chunk_metadata_json_survives_roundtrip(rag_repos: Any, cleanup_user: str) -> None:
    """``metadata``（切分参数固化）必须原样存回来。

    这一列是 003 脚本补的；如果只跑了 001，这里会以
    ``503 DEPENDENCY_UNAVAILABLE`` 的形式暴露「表结构落后于代码」。
    """
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    doc = await rag_repos.documents.add(_doc(kb))
    await rag_repos.chunks.replace_for_document(doc.id, [_chunk(doc, 0, metadata={"merged": True})])

    chunks, _ = await rag_repos.chunks.list_for_document(doc.id, cleanup_user, limit=10)
    assert chunks[0].metadata == {"merged": True}


async def test_chunk_cursor_paginates_over_chunk_index(rag_repos: Any, cleanup_user: str) -> None:
    """切片游标就是 ``chunk_index`` 本身（不是时间戳）。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    doc = await rag_repos.documents.add(_doc(kb))
    await rag_repos.chunks.replace_for_document(doc.id, [_chunk(doc, i) for i in range(5)])

    first, has_more = await rag_repos.chunks.list_for_document(doc.id, cleanup_user, limit=2)
    assert [c.chunk_index for c in first] == [0, 1]
    assert has_more is True
    second, has_more2 = await rag_repos.chunks.list_for_document(
        doc.id, cleanup_user, limit=2, cursor=encode_index_cursor(first[-1].chunk_index)
    )
    assert [c.chunk_index for c in second] == [2, 3]
    assert has_more2 is True


async def test_count_for_kb_excludes_soft_deleted_documents(
    rag_repos: Any, cleanup_user: str
) -> None:
    """KB 级切片计数要 JOIN ``document`` 过滤软删 —— 否则删了文档计数不掉。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    doc = await rag_repos.documents.add(_doc(kb))
    await rag_repos.chunks.replace_for_document(doc.id, [_chunk(doc, i) for i in range(4)])
    assert await rag_repos.chunks.count_for_kb(kb.id) == 4

    await rag_repos.documents.soft_delete(doc.id, cleanup_user)
    assert await rag_repos.chunks.count_for_kb(kb.id) == 0


async def test_delete_for_document_removes_all_chunks(rag_repos: Any, cleanup_user: str) -> None:
    """删除文档切片（幂等：删两次不报错）。"""
    kb = await rag_repos.knowledge_bases.add(_kb(cleanup_user))
    doc = await rag_repos.documents.add(_doc(kb))
    await rag_repos.chunks.replace_for_document(doc.id, [_chunk(doc, 0), _chunk(doc, 1)])

    await rag_repos.chunks.delete_for_document(doc.id)
    await rag_repos.chunks.delete_for_document(doc.id)
    assert await rag_repos.chunks.count_for_document(doc.id) == 0


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------


def test_build_repositories_uses_mysql_when_real(mysql_settings: Any) -> None:
    """``INFRA_BACKEND=real`` 必须拿到真实 SQL 仓储（而不是 503 占位）。"""
    from app.storage import build_repositories

    repos = build_repositories(mysql_settings)
    assert repos.knowledge_bases.__class__.__name__ == "MySqlKnowledgeBaseRepo"


def test_build_repositories_falls_back_when_driver_missing(monkeypatch: Any) -> None:
    """驱动缺失 → 降级到 503 占位实现，而不是让进程起不来（``docs/10``）。"""
    from app.storage import build_repositories

    def _boom(_settings: Any) -> Any:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "未安装 sqlalchemy")

    monkeypatch.setattr("app.storage.mysql.MySqlRagRepository", _boom)
    repos = build_repositories(build_settings(infra_backend="real"))
    assert repos.knowledge_bases.__class__.__name__ == "UnavailableKnowledgeBaseRepo"
