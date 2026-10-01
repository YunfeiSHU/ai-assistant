"""本地 CPU 推理的 torch 线程数限制（``TORCH_NUM_THREADS``）。

BGE embedding 与 reranker 都是本地 CPU 推理，而 torch 默认把线程数设成逻辑核数 ——
一次 8MB 文档的向量化会把整机核吃满，同机的 MySQL / Redis / 网关一起被拖慢：实测
连跑两遍全套时 M1 41s→75s、M2 71s→186s，整体慢 2~3 倍（``docs/09`` §2.5）。
用「单次向量化变慢」换「整机不被饿死」。

用 ``torch.set_num_threads`` 而不是 ``OMP_NUM_THREADS``：环境变量必须在 torch 被
import 之前设置才生效，而本项目在 import 期就会经
``langchain_text_splitters → transformers → torch`` 把它拉进来，等读 ``.env`` 时已经晚了。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("app.rag.torch_threads")

__all__ = ["apply_torch_num_threads"]


def apply_torch_num_threads(num_threads: int) -> bool:
    """把 torch 的 CPU 线程数限制为 ``num_threads``。

    Args:
        num_threads: 线程数上限；``<= 0`` 表示不干预（保留 torch 默认，也是本项目默认值）。

    Returns:
        是否真的调用了 ``torch.set_num_threads``。

    Notes:
        torch 未安装时静默跳过并记一条 warning：``EMBEDDING_PROVIDER=hash`` 时根本
        不会用到 torch，不该因为「没装 torch」让调用方失败。
        这是进程级全局设置，两个 provider 调同一个值，所以谁先加载谁生效。
    """
    if num_threads <= 0:
        return False
    try:
        import torch
    except ImportError:  # pragma: no cover - 只有完全没装 torch 的环境会走到
        logger.warning("torch.num_threads_skipped", extra={"reason": "torch_not_installed"})
        return False
    torch.set_num_threads(int(num_threads))
    logger.info(
        "torch.num_threads_applied",
        extra={"num_threads": int(num_threads), "torch_version": torch.__version__},
    )
    return True
