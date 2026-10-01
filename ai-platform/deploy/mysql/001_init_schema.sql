-- =============================================================================
-- ai-platform · MySQL 建表脚本
--
-- 权威定义：docs/09-数据存储模型.md §2（需求 REQ-DATA-001）
-- 覆盖范围：**整个系统共用的库** `ai_platform`（不只是 AI 侧）。
--   · 本脚本负责 ai-platform 持有的 9 张表；
--   · 网关（go-services）的 6 张独占表在 `go-services/deploy/mysql/001_gateway_tables.sql`，
--     同一个库，执行顺序：先本脚本，再它；
--   · `idempotency_record` / `audit_log` 是**两服务共享**的表 —— 同库同名只能有一张，
--     所以本脚本给出的是**并集**定义（网关要的 method / request_hash / ip / user_agent
--     也在里面），两侧都用这一份，**不要再各写一份**。
--
-- 已有旧库升级：本脚本只做 `CREATE ... IF NOT EXISTS`，不会给已存在的表补列，
--   所以从「分库版本」迁过来时须额外跑一次 `002_align_shared_tables.sql`。
--
-- 执行方式（推荐，避免 PowerShell 管道的编码问题）：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source 001_init_schema.sql"
--
-- 幂等：全部 CREATE ... IF NOT EXISTS，可重复执行；**不含任何 DROP**。
--
-- 全局约定
--   * 字符集 utf8mb4 / 排序规则 utf8mb4_0900_ai_ci（MySQL 8.0）
--   * 时间列一律 DATETIME(3)，存 **UTC**；应用侧序列化为 ...Z
--   * 主键为带前缀的 ULID（VARCHAR(32)）：kb_ / doc_ / chk_ / task_ / mem_ / cv_
--   * **不建物理外键**（docs/09 §6）：跨服务（会话台账在网关）与跨存储
--     （Milvus / Redis / MinIO）的一致性由应用层与孤儿清理任务保证，
--     物理外键只会在删除派生数据时挡住路
--   * 业务表一律带 user_id（租户隔离，docs/09 §6）；除 idempotency_record
--     外均建 (user_id, ...) 前缀索引
--   * 软删除表（knowledge_base / document）用 deleted_at，查询 MUST 带
--     `deleted_at IS NULL`；Milvus 向量 / Redis Key / MinIO 对象则**立即物理删除**
-- =============================================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS `ai_platform`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `ai_platform`;


-- -----------------------------------------------------------------------------
-- 2.1 knowledge_base
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `knowledge_base` (
  `id`              VARCHAR(32)  NOT NULL COMMENT 'kb_*',
  `user_id`         VARCHAR(64)  NOT NULL,
  `name`            VARCHAR(64)  NOT NULL,
  `description`     VARCHAR(500) NOT NULL DEFAULT '',
  `chunk_size`      INT          NOT NULL DEFAULT 512,
  `chunk_overlap`   INT          NOT NULL DEFAULT 64,
  `embedding_model` VARCHAR(128) NULL,
  `embedding_dim`   INT          NOT NULL DEFAULT 1024,
  `retrieval_top_k` INT          NOT NULL DEFAULT 20,
  `rerank_top_n`    INT          NOT NULL DEFAULT 5,
  `score_threshold` FLOAT        NOT NULL DEFAULT 0,
  `status`          VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT 'active|indexing|failed',
  `document_count`  INT          NOT NULL DEFAULT 0,
  `chunk_count`     BIGINT       NOT NULL DEFAULT 0,
  `metadata`        JSON         NULL,
  `created_at`      DATETIME(3)  NOT NULL,
  `updated_at`      DATETIME(3)  NOT NULL,
  `deleted_at`      DATETIME(3)  NULL,
  -- 唯一键里的 NULL 不参与比较，所以**不能**直接对 (user_id, name, deleted_at)
  -- 建唯一键：未删除的行 deleted_at 全是 NULL，任意多条同名都能插进去
  -- （实测：第二条同名插入的 ROW_COUNT() = 1，约束形同虚设）。
  -- 这个生成列把 NULL 换成哨兵时刻，既保住「同名只能有一条未删除」，
  -- 又保住「软删后可以重建同名」。应用侧 SELECT 时应显式列名，忽略本列。
  `deleted_key`     DATETIME(3)  GENERATED ALWAYS AS (IFNULL(`deleted_at`, '1970-01-01 00:00:00.000')) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_kb_user_name` (`user_id`, `name`, `deleted_key`),
  KEY `idx_kb_user_created` (`user_id`, `created_at` DESC)
) ENGINE=InnoDB COMMENT='知识库';

-- 计数列（document_count / chunk_count）是**冗余缓存**：任务完成时用
-- `UPDATE ... SET x = (SELECT COUNT(*) ...)` 重算，不做 +1 累加（避免重试漂移）。


-- -----------------------------------------------------------------------------
-- 2.2 document
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `document` (
  `id`             VARCHAR(32)   NOT NULL COMMENT 'doc_*',
  `kb_id`          VARCHAR(32)   NOT NULL,
  `user_id`        VARCHAR(64)   NOT NULL,
  `doc_name`       VARCHAR(512)  NOT NULL,
  `file_ext`       VARCHAR(16)   NOT NULL,
  `mime_type`      VARCHAR(128)  NOT NULL,
  `size_bytes`     BIGINT        NOT NULL,
  `page_count`     INT           NULL,
  `object_key`     VARCHAR(1024) NOT NULL COMMENT 'MinIO key',
  `content_sha256` CHAR(64)      NOT NULL,
  `status`         VARCHAR(16)   NOT NULL COMMENT 'PENDING|PARSING|CHUNKING|EMBEDDING|INDEXED|FAILED',
  -- chunk_count = 实际入库数；chunks_total = 切分产出数（截断前）。
  -- 两者不等（且 truncated=1）就是「被 MAX_DOC_CHUNKS 截断了」——
  -- 静默截断会让「入库成功」变成假的（docs/10 的 UP-01）。
  `chunk_count`    INT           NOT NULL DEFAULT 0,
  `char_count`     INT           NOT NULL DEFAULT 0,
  `chunks_total`   INT           NULL COMMENT '切分产出切片数（截断前）；NULL=还没走到切分',
  `truncated`      TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '是否因 MAX_DOC_CHUNKS 丢弃尾部切片',
  `chunk_size`     INT           NOT NULL,
  `chunk_overlap`  INT           NOT NULL,
  `task_id`        VARCHAR(32)   NULL,
  `error_code`     VARCHAR(64)   NULL,
  `error_message`  VARCHAR(1000) NULL,
  `metadata`       JSON          NULL,
  `indexed_at`     DATETIME(3)   NULL,
  `created_at`     DATETIME(3)   NOT NULL,
  `updated_at`     DATETIME(3)   NOT NULL,
  `deleted_at`     DATETIME(3)   NULL,
  `deleted_key`    DATETIME(3)   GENERATED ALWAYS AS (IFNULL(`deleted_at`, '1970-01-01 00:00:00.000')) STORED,
  PRIMARY KEY (`id`),
  -- 含 deleted_key 而不是只靠 (kb_id, content_sha256)：
  --   未删除时 → 同一文件在同一个库里只能有一份（去重生效）；
  --   已软删后 → 允许重新上传（否则用户删了文档就再也传不回同一份，
  --             而列表里又看不见那条已删记录，只能报「文件已存在」）。
  -- 反过来，若唯一键直接包含可空的 deleted_at，去重会**完全失效**（见 knowledge_base）。
  UNIQUE KEY `uk_doc_dedupe` (`kb_id`, `content_sha256`, `deleted_key`),
  KEY `idx_doc_kb_status` (`kb_id`, `status`),
  KEY `idx_doc_user_created` (`user_id`, `created_at` DESC)
) ENGINE=InnoDB COMMENT='文档元数据';


-- -----------------------------------------------------------------------------
-- 2.3 document_chunk
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `document_chunk` (
  `id`             VARCHAR(32)  NOT NULL COMMENT 'chk_*',
  `doc_id`         VARCHAR(32)  NOT NULL,
  `kb_id`          VARCHAR(32)  NOT NULL,
  `user_id`        VARCHAR(64)  NOT NULL,
  `chunk_index`    INT          NOT NULL,
  `content`        MEDIUMTEXT   NOT NULL,
  `content_sha256` CHAR(64)     NOT NULL,
  `char_start`     INT          NOT NULL,
  `char_end`       INT          NOT NULL,
  `page`           INT          NULL,
  `heading_path`   VARCHAR(512) NULL,
  `token_count`    INT          NOT NULL,
  `metadata`       JSON         NULL COMMENT '切分参数固化：doc_name/chunk_size/chunk_overlap/merged',
  `created_at`     DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_chunk_doc_idx` (`doc_id`, `chunk_index`),
  KEY `idx_chunk_doc` (`doc_id`),
  KEY `idx_chunk_kb` (`kb_id`)
) ENGINE=InnoDB COMMENT='文档切片（仅元数据/管理后台用，检索走 Milvus）';

-- content 与 Milvus 中的 content 重复存储是有意为之：MySQL 供管理后台与
-- 删除审计使用，检索只走 Milvus（docs/09 §2.3）。


-- -----------------------------------------------------------------------------
-- 2.4 task
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `task` (
  `id`            VARCHAR(32)  NOT NULL COMMENT 'task_*',
  `type`          VARCHAR(32)  NOT NULL COMMENT 'document_ingest|document_delete|summary_build|memory_extract',
  `status`        VARCHAR(16)  NOT NULL COMMENT 'QUEUED|RUNNING|SUCCEEDED|FAILED|CANCELED',
  `user_id`       VARCHAR(64)  NOT NULL,
  `resource_type` VARCHAR(32)  NOT NULL,
  `resource_id`   VARCHAR(64)  NOT NULL,
  `payload`       JSON         NULL,
  `progress`      SMALLINT     NOT NULL DEFAULT 0,
  `stage`         VARCHAR(32)  NULL,
  -- 切片计数（docs/10 UP-02）：进度条的分母/分子。
  -- 只对 document_ingest / kb_reindex 类任务有意义，其他类型恒为 0。
  -- 注：当前任务仓储的实现在 Redis（app/tasks/redis_store.py），这两列是为
  -- 「换回 MySQL 权威存储」预留的；加在这里是为了避免「代码有字段、DDL 没列」的
  -- 漂移（历史上已因同一原因补过一次 document_chunk.metadata）。
  `chunks_total`  INT          NOT NULL DEFAULT 0 COMMENT '本次待向量化切片数（截断后）',
  `chunks_done`   INT          NOT NULL DEFAULT 0 COMMENT '已完成向量化的切片数',
  `retry_count`   INT          NOT NULL DEFAULT 0,
  `max_retries`   INT          NOT NULL DEFAULT 3,
  `version`       BIGINT       NOT NULL DEFAULT 0 COMMENT '乐观锁',
  `idem_key`      CHAR(64)     NOT NULL,
  `error`         JSON         NULL,
  `queued_at`     DATETIME(3)  NULL,
  `started_at`    DATETIME(3)  NULL,
  `finished_at`   DATETIME(3)  NULL,
  `created_at`    DATETIME(3)  NOT NULL,
  `updated_at`    DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  -- 幂等键的唯一约束是「重复投递不出第二个任务」的**唯一**保证
  -- （应用层是先查后插，并发下只能靠这里兜底）
  UNIQUE KEY `uk_task_idem` (`idem_key`),
  KEY `idx_task_user_created` (`user_id`, `created_at` DESC),
  KEY `idx_task_status_created` (`status`, `created_at`),
  KEY `idx_task_resource` (`resource_type`, `resource_id`)
) ENGINE=InnoDB COMMENT='异步任务';

-- payload 里的幂等键（应用层 make_idem_key）形如
-- sha256(type|user_id|resource_id|turn_marker)，故 idem_key 为 CHAR(64)。
-- 注意 M5 的教训：turn_marker 必须进幂等键，否则第 2 轮起被当成重复投递。


-- -----------------------------------------------------------------------------
-- 2.5 user_memory
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `user_memory` (
  `id`                     VARCHAR(32)   NOT NULL COMMENT 'mem_*',
  `user_id`                VARCHAR(64)   NOT NULL,
  `kind`                   VARCHAR(16)   NOT NULL COMMENT 'preference|fact',
  `content`                VARCHAR(2000) NOT NULL,
  `content_sha256`         CHAR(64)      NOT NULL,
  `confidence`             FLOAT         NOT NULL DEFAULT 1.0,
  `source`                 VARCHAR(32)   NOT NULL DEFAULT 'auto' COMMENT 'auto|manual',
  `source_conversation_id` VARCHAR(64)   NULL,
  `hit_count`              INT           NOT NULL DEFAULT 1,
  `expired`                TINYINT(1)    NOT NULL DEFAULT 0,
  `expires_at`             DATETIME(3)   NULL,
  `created_at`             DATETIME(3)   NOT NULL,
  `updated_at`             DATETIME(3)   NOT NULL,
  PRIMARY KEY (`id`),
  -- 精确去重的落点：唯一键建立在**规范化后**的哈希上
  -- （app/memory/long_term.py::content_sha256 → normalise_content），
  -- 否则「同一句话多一个空格」会被存成两条
  UNIQUE KEY `uk_mem_user_hash` (`user_id`, `content_sha256`),
  KEY `idx_mem_user_kind` (`user_id`, `kind`, `expired`)
) ENGINE=InnoDB COMMENT='长期记忆';

-- content 长度 2000 远大于应用侧 MEMORY_CONTENT_MAX_CHARS（默认 500）：
-- 留出余量，避免以后放宽校验时还要改表。


-- -----------------------------------------------------------------------------
-- 2.6 conversation_summary
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `conversation_summary` (
  `conversation_id`      VARCHAR(64) NOT NULL,
  `user_id`              VARCHAR(64) NOT NULL,
  `content`              TEXT        NOT NULL COMMENT '四段结构摘要',
  `covered_until`        DATETIME(3) NOT NULL COMMENT '摘要覆盖到的消息时间',
  `source_message_count` INT         NOT NULL,
  `token_count`          INT         NOT NULL,
  `version`              BIGINT      NOT NULL DEFAULT 0,
  `created_at`           DATETIME(3) NOT NULL,
  `updated_at`           DATETIME(3) NOT NULL,
  PRIMARY KEY (`conversation_id`),
  KEY `idx_summary_user` (`user_id`)
) ENGINE=InnoDB COMMENT='对话摘要';

-- conversation_id 不设外键：会话台账在 Go 网关侧，Python 只持有引用
-- （docs/09 §2.6）。Redis 里的 summary:{conversation_id} 只是缓存，本表是权威。


-- -----------------------------------------------------------------------------
-- 2.7 idempotency_record
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `idempotency_record` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `user_id`       VARCHAR(64)  NOT NULL,
  `method`        VARCHAR(8)   NOT NULL DEFAULT '' COMMENT 'GET|POST|...；ai-platform 侧不填',
  `path`          VARCHAR(255) NOT NULL,
  `idem_key`      VARCHAR(128) NOT NULL,
  `request_hash`  CHAR(64)     NOT NULL DEFAULT '' COMMENT 'sha256(body)，防「同键换请求体」；ai-platform 侧不填',
  `status_code`   SMALLINT     NOT NULL,
  `response_body` JSON         NOT NULL,
  `created_at`    DATETIME(3)  NOT NULL,
  `expires_at`    DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_idem` (`user_id`, `method`, `path`, `idem_key`),
  KEY `idx_idem_expire` (`expires_at`)
) ENGINE=InnoDB COMMENT='幂等响应回放（两服务共享）';

-- `method` / `request_hash` 是网关要的，两边共用这张表所以在这里一并定义。
-- 给它们 `DEFAULT ''` 是为了让 ai-platform 侧不写这两列时也能插入：
-- 旧写法（只填 user_id / path / idem_key）仍然受 `uk_idem` 保护，不会因缺列而报错。


-- -----------------------------------------------------------------------------
-- 2.8 user_settings
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `user_settings` (
  `user_id`        VARCHAR(64) NOT NULL,
  `memory_enabled` TINYINT(1)  NOT NULL DEFAULT 1,
  `memory_top_n`   INT         NOT NULL DEFAULT 3,
  `cleared_at`     DATETIME(3) NULL COMMENT '最近一次清空全部记忆的时刻（24h 冷却期起点）',
  `created_at`     DATETIME(3) NOT NULL,
  `updated_at`     DATETIME(3) NOT NULL,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB COMMENT='用户级设置（缺行即全默认，不预建）';

-- cleared_at 对应 app/memory/preferences.py::MemoryPreference.cleared_at
-- （REQ-MEM-007：清空后 24h 内不重新抽取旧内容）。
-- docs/09-§2.8 的原表格漏了这一列，已在 docs/09-§2.9 说明。


-- -----------------------------------------------------------------------------
-- 2.8 audit_log
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `audit_log` (
  `id`            BIGINT       NOT NULL AUTO_INCREMENT,
  `user_id`       VARCHAR(64)  NULL COMMENT '系统操作为 NULL',
  `action`        VARCHAR(64)  NOT NULL,
  `resource_type` VARCHAR(32)  NOT NULL DEFAULT '',
  `resource_id`   VARCHAR(64)  NOT NULL DEFAULT '',
  `ip`            VARCHAR(45)  NULL COMMENT '网关侧填；ai-platform 侧不填',
  `user_agent`    VARCHAR(255) NULL COMMENT '网关侧填；ai-platform 侧不填',
  `detail`        JSON         NULL,
  `created_at`    DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_audit_user_time` (`user_id`, `created_at`),
  KEY `idx_audit_resource` (`resource_type`, `resource_id`),
  KEY `idx_audit_time` (`created_at`)
) ENGINE=InnoDB COMMENT='审计日志（只追加，保留 ≥ 180 天；两服务共享）';

-- 只追加不更新：审计行一旦写入就不允许改（docs/09 §2.8）。
