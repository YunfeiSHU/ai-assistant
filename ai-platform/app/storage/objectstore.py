"""对象存储：MinIO 适配器 + 内存实现（``docs/09`` §5.1）。

真实适配器**懒导入** ``minio``：驱动没装时不影响 memory 后端启动，
只有在 ``INFRA_BACKEND=real`` 且真的调用时才报明确的错误
（而不是 import 期就把整个应用带崩）。
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Any

from app.config import Settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger("app.storage.object_store")


class InMemoryObjectStore:
    """进程内对象存储（本地/测试）。"""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            self._objects[key] = data

    async def get(self, key: str) -> bytes:
        with self._lock:
            data = self._objects.get(key)
        if data is None:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "对象不存在", {"object_key": key})
        return data

    async def delete(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._objects


#: ``S3Error`` 里属于「依赖/配置不对」而不是「业务对象不存在」的错误码。
#: 它们必须报 503：把凭据错报成 500 会让人去翻代码，而修法其实是改 ``.env``。
_MINIO_DEPENDENCY_CODES = frozenset(
    {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "NoSuchBucket"}
)


def _classify_minio_error(exc: Exception) -> AppError | None:
    """MinIO 异常 → 领域错误；返回 ``None`` 表示「业务层自行判断」。

    分三类：

    * **连不上**（``MaxRetryError`` / 超时等非 ``S3Error``）→ ``503``：
      容器没起、端口写错、网络不通，都属于依赖故障（``docs/10`` 故障矩阵）。
    * **连上但认证/桶不对**（:data:`_MINIO_DEPENDENCY_CODES`）→ ``503``：
      同样是部署配置问题，不是客户端能通过重试解决的事情。
    * **其它 ``S3Error``**（``NoSuchKey`` 等）→ ``None``：由调用方按业务语义处理
      （例如 ``get`` 要报 ``404 DOCUMENT_NOT_FOUND``）。
    """
    try:
        from minio.error import S3Error
    except ImportError:  # pragma: no cover - 未装驱动时走不到这里
        return None
    if isinstance(exc, S3Error):
        if exc.code in _MINIO_DEPENDENCY_CODES:
            return AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "对象存储不可用（MinIO 拒绝访问，请检查 MINIO_ACCESS_KEY/SECRET_KEY）",
                {"component": "minio", "code": exc.code},
            )
        return None
    return AppError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "对象存储不可用（MinIO 连接失败）",
        {"component": "minio", "error": type(exc).__name__},
    )


class MinioObjectStore:
    """MinIO 适配器（Bucket 私有，流式上传/下载）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        """懒加载 MinIO 客户端（缺失驱动时给出明确的配置级错误）。"""
        if self._client is None:
            try:
                from minio import Minio
            except ImportError as exc:  # pragma: no cover - 需要真实部署才走到
                raise AppError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "未安装 minio 依赖，无法使用 INFRA_BACKEND=real 的对象存储",
                ) from exc
            self._client = Minio(
                self._settings.minio_endpoint,
                access_key=self._settings.minio_access_key,
                secret_key=self._settings.minio_secret_key,
                secure=self._settings.minio_secure,
            )
            self._ensure_bucket(self._client)
        return self._client

    def _ensure_bucket(self, client: Any) -> None:
        bucket = self._settings.minio_bucket
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info("minio.bucket_created", extra={"bucket": bucket})

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        import anyio

        # MinIO SDK 是同步的；放进线程池避免阻塞事件循环（SSE 对话与它同进程）
        def _put() -> None:
            self.client.put_object(
                self._settings.minio_bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type or "application/octet-stream",
            )

        try:
            await anyio.to_thread.run_sync(_put)
        except AppError:
            raise
        except Exception as exc:
            # 不分类的话，MinIO 没起来会从上传接口冒出一个 URLError 变成 500，
            # 而「上传失败」在使用者看来与权限、文件格式是同一类问题 —— 503 才说明
            # "这是我们这边的依赖挂了"。
            mapped = _classify_minio_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def get(self, key: str) -> bytes:
        import anyio

        def _get() -> bytes:
            response = self.client.get_object(self._settings.minio_bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        try:
            return await anyio.to_thread.run_sync(_get)
        except AppError:
            raise
        except Exception as exc:
            # 先判依赖类错误：否则「MinIO 挂了」会被当成 404 —— 用户看到的是
            # "文档不存在"，会去删了重传，而真正坏掉的东西一直没被发现
            mapped = _classify_minio_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "对象不存在", {"object_key": key}) from exc

    async def delete(self, key: str) -> None:
        import anyio
        from minio.error import S3Error

        def _delete() -> None:
            try:
                self.client.remove_object(self._settings.minio_bucket, key)
            except S3Error as exc:
                if exc.code not in ("NoSuchKey", "NoSuchBucket"):
                    raise
                # 不存在也算删除成功：删除任务必须幂等（docs/09 §6）

        await anyio.to_thread.run_sync(_delete)

    async def exists(self, key: str) -> bool:
        import anyio

        def _stat() -> bool:
            try:
                self.client.stat_object(self._settings.minio_bucket, key)
            except Exception:
                return False
            return True

        return await anyio.to_thread.run_sync(_stat)


def build_object_store(settings: Settings) -> Any:
    """按 ``INFRA_BACKEND`` 选择实现。"""
    if settings.infra_backend == "real":
        return MinioObjectStore(settings)
    return InMemoryObjectStore()


__all__ = ["InMemoryObjectStore", "MinioObjectStore", "build_object_store"]
