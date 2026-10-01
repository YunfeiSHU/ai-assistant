"""本地 CPU 推理的 torch 线程数限制（``TORCH_NUM_THREADS``）。

**为什么需要这个开关**：BGE embedding 与 BGE reranker 都是**本地 CPU 推理**，
而 ``torch`` 默认把线程数设成逻辑核数 —— 一次 8MB 文档的向量化会把整机的核吃满，
同一台机器上的 MySQL / Redis / 网关 / 验收脚本一起被拖慢。实测（docs/09 §2.5）：
连跑两遍全套时 M1 41s→75s、M2 71s→186s，整体慢 2~3 倍，看起来像「产品变慢了」。
把线程数压到 ``2`` 之类的小值，是用「**单次**向量化变慢」换「整机不被饿死」。

**为什么用 ``torch.set_num_threads`` 而不是 ``OMP_NUM_THREADS``**：环境变量必须在
torch 被 **import 之前**设置才生效，而本项目在 import 期就会经
``langchain_text_splitters → transformers → torch`` 把它拉进来（与
``app/core/config.py::apply_hf_endpoint`` 遇到的是同一类「import 顺序导致配置失效」
问题），等到读 ``.env`` 时已经晚了。``torch.set_num_threads`` 是运行期 API，
什么时候调都算数。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("app.rag.torch_threads")

__all__ = ["apply_torch_num_threads"]


def apply_torch_num_threads(num_threads: int) -> bool:
    """把 torch 的 CPU 线程数限制为 ``num_threads``。

    Args:
        num_threads: 线程数上限；``<= 0`` 表示**不干预**（保留 torch 默认，
            也是本项目的默认值 —— 单机独占时默认值才是吞吐最优的）。

    Returns:
        是否真的调用了 ``torch.set_num_threads``。

    Notes:
        * ``torch`` 未安装时静默跳过并记一条 warning：``EMBEDDING_PROVIDER=hash``
          时根本不会用到 torch，不该因为「没装 torch」让调用方失败。
        * 这是**进程级全局设置**，与调用它的 provider 实例无关 —— 两个 provider
          （embedding / reranker）调同一个值，所以谁先加载谁生效，语义一致。
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
