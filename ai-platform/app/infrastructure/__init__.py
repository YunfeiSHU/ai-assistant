"""基础设施适配层：外部依赖（Redis / MySQL / Milvus / MinIO / 可观测性）的具体实现。

**为什么单独一层**：这些模块都直接依赖某个具体的中间件（驱动、DSN、连接池），
与业务规则无关。把它们收拢到 ``app/infrastructure`` 之后，依赖方向变成单向的
「上层 → infrastructure → core」，领域包（``app/memory`` / ``app/rag`` …）里只留
协议与纯逻辑，换实现不用翻遍全仓。

子包按**中间件**而不是按业务切分（``mysql`` / ``redis`` / ``storage`` /
``observability``），因为同一个中间件往往被多个业务模块共用 —— 例如 MySQL 引擎
既服务于本包的 ``storage``（KB / 文档 / 切片仓储），也服务于 ``app.memory`` 的长期记忆仓储。
"""
