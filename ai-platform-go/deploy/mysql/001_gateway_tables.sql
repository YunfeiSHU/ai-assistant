-- =============================================================================
-- go-services（网关）· MySQL 建表脚本
--
-- 权威定义：docs/05-数据模型.md §2
--
-- ⚠️ **与 ai-platform 共用同一个库 `ai_platform`**，本脚本只建网关**独占**的 6 张表。
--    共享的表（`idempotency_record` / `audit_log`）**不在这里重复定义** ——
--    同库同名只能有一张表，它的权威定义在
--    `ai-platform/deploy/mysql/001_init_schema.sql`（并集版：同时含网关要的
--    `method` / `request_hash` / `ip` / `user_agent`）。
--    重复定义会产生「改一处漏一处」的风险，所以这里只引用不复制。
--
-- 执行顺序：
--   1. ai-platform/deploy/mysql/001_init_schema.sql   （建库 + 9 张表，含共享表）
--   2. 本文件                                        （建网关独占的 6 张表）
--   3. go-services/deploy/mysql/verify_schema.sql    （自检）
--
-- 执行方式（推荐，避免 PowerShell 管道的编码问题）：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source 001_gateway_tables.sql"
--
-- 幂等：全部 CREATE ... IF NOT EXISTS，可重复执行；**不含任何 DROP**。
--
-- 全局约定（与 ai-platform/docs/09 §2 preamble 逐条对齐）
--   * 字符集 utf8mb4 / 排序规则 utf8mb4_0900_ai_ci（MySQL 8.0）
--   * 时间列一律 DATETIME(3)，存 UTC
--   * 主键为带前缀 ULID（VARCHAR(32)）：u_ / rt_ / cv_ / msg_
--   * user_id 一律 VARCHAR(64)：它是 JWT 的 sub，跨服务传递，两侧宽度必须一致
--   * 不建物理外键（docs/05 §4）：message 高频插入，外键会在 conversation 上加
--     共享锁导致写入串行化；跨表一致性由应用层事务 + 每日孤儿检查任务保证
--   * 全部 SQL 走参数化（GORM / database/sql 占位符）
-- =============================================================================

SET NAMES utf8mb4;

-- 库本身由 ai-platform 的 001 建立；这里再写一遍 IF NOT EXISTS 是为了让本脚本
-- 在「先跑它」的情况下也不会因「库不存在」而报错（已存在时是无副作用的空操作）。
CREATE DATABASE IF NOT EXISTS `ai_platform`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `ai_platform`;


-- -----------------------------------------------------------------------------
-- 表归属总览（同一库 `ai_platform`，15 张表）
-- -----------------------------------------------------------------------------
-- 本脚本（网关独占，6 张）：       user / refresh_token / conversation /
--                                  message / quota_usage / usage_record
-- ai-platform 的 001 脚本（AI 独占，7 张）：knowledge_base / document /
--                                  document_chunk / task / user_memory /
--                                  conversation_summary / user_settings
-- 共享（2 张，只在 ai-platform 的 001 里定义）：idempotency_record / audit_log
--
-- 为什么共享表不在这里再写一遍：同一个库、同一个名字只能有一张表。两份定义
-- 一定会逐渐不一致（改一处漏一处），而「两份 DDL 都执行过」看起来又是成功的。
--
-- 为什么 AI 的 7 张表也不在这里出现：网关 MUST NOT 持有 AI 的元数据
-- （docs/05 §1.2），建表权限同样不该在网关侧。


-- -----------------------------------------------------------------------------
-- 2.1 user
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `user` (
  `id`            VARCHAR(32)  NOT NULL COMMENT 'u_*',
  `email`         VARCHAR(254) NOT NULL COMMENT '统一小写存储',
  `nickname`      VARCHAR(64)  NOT NULL DEFAULT '',
  `password_hash` VARCHAR(255) NOT NULL COMMENT 'argon2id 编码串',
  `plan`          VARCHAR(32)  NOT NULL DEFAULT 'free',
  `status`        VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT 'active|disabled|deleted',
  `token_version` INT          NOT NULL DEFAULT 1 COMMENT '改密码/踢下线时 +1，使旧 access 失效',
  `last_login_at` DATETIME(3)  NULL,
  `created_at`    DATETIME(3)  NOT NULL,
  `updated_at`    DATETIME(3)  NOT NULL,
  `deleted_at`    DATETIME(3)  NULL,
  PRIMARY KEY (`id`),
  -- 唯一键**不含** deleted_at，这是刻意的：邮箱是账号身份锚点，若允许注销后立即复用，
  -- 攻击者可用旧邮箱重新注册来摸到历史数据引用 → 已软删用户的邮箱永久占用。
  --
  -- 注意不要照抄 ai-platform knowledge_base 的写法：那边用生成列 deleted_key 让
  -- 「同名可重建」。「资源名可复用」与「身份标识不可复用」是两件事，写法相反是有意的。
  -- 另：本表没有 user_id 列（id 就是用户 id），引用它的列用 VARCHAR(64)。
  UNIQUE KEY `uk_user_email` (`email`),
  KEY `idx_user_status_created` (`status`, `created_at`)
) ENGINE=InnoDB COMMENT='用户';


-- -----------------------------------------------------------------------------
-- 2.2 refresh_token
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `refresh_token` (
  `id`          VARCHAR(32)  NOT NULL COMMENT 'rt_*',
  `user_id`     VARCHAR(64)  NOT NULL COMMENT 'JWT sub；宽度与 ai-platform 对齐',
  `token_hash`  CHAR(64)     NOT NULL COMMENT 'sha256(明文)，明文只在响应中出现一次',
  `device_name` VARCHAR(64)  NOT NULL DEFAULT '',
  `user_agent`  VARCHAR(255) NOT NULL DEFAULT '',
  `ip`          VARCHAR(45)  NOT NULL DEFAULT '',
  `expires_at`  DATETIME(3)  NOT NULL,
  `revoked_at`  DATETIME(3)  NULL,
  `created_at`  DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_rt_hash` (`token_hash`),
  KEY `idx_rt_user` (`user_id`, `revoked_at`, `expires_at`),
  KEY `idx_rt_expire` (`expires_at`)
) ENGINE=InnoDB COMMENT='刷新令牌（只存哈希）';

-- 轮换语义（REQ-AUTH-003）：同一事务内 UPDATE 旧（revoked_at）→ 检查 RowsAffected
-- → 为 0 即失效令牌复用，返回 401 INVALID_REFRESH_TOKEN；否则 INSERT 新行。


-- -----------------------------------------------------------------------------
-- 2.3 conversation
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `conversation` (
  `id`              VARCHAR(32)  NOT NULL COMMENT 'cv_*，网关生成；ai-platform 以 VARCHAR(64) 引用',
  `user_id`         VARCHAR(64)  NOT NULL COMMENT 'JWT sub；宽度与 ai-platform 对齐',
  `title`           VARCHAR(100) NOT NULL DEFAULT '',
  `title_source`    VARCHAR(16)  NOT NULL DEFAULT 'auto' COMMENT 'auto|manual',
  `status`          VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT 'active|archived',
  `model`           VARCHAR(64)  NULL,
  `kb_ids`          JSON         NULL COMMENT 'string[]，会话默认检索范围（同库引用 knowledge_base）',
  `message_count`   INT          NOT NULL DEFAULT 0 COMMENT '同时作为 seq 分配器',
  `last_message_at` DATETIME(3)  NULL,
  `pinned`          TINYINT(1)   NOT NULL DEFAULT 0,
  `metadata`        JSON         NULL,
  `created_at`      DATETIME(3)  NOT NULL,
  `updated_at`      DATETIME(3)  NOT NULL,
  `deleted_at`      DATETIME(3)  NULL,
  PRIMARY KEY (`id`),
  KEY `idx_conv_user_list` (`user_id`, `deleted_at`, `status`, `pinned` DESC, `last_message_at` DESC),
  KEY `idx_conv_cleanup` (`deleted_at`)
) ENGINE=InnoDB COMMENT='会话';

-- seq 分配 MUST 在**同一连接**上完成（LAST_INSERT_ID(expr) 是连接级的）：
--   UPDATE conversation
--      SET message_count = LAST_INSERT_ID(message_count + 1), last_message_at = ?
--    WHERE id = ? AND user_id = ? AND deleted_at IS NULL AND status = 'active';
--   SELECT LAST_INSERT_ID();
-- 换到另一条连接上执行第二条语句会读到别的会话的值 —— 这是本设计最容易踩的坑，
-- MUST 有并发测试（AC-DATA-02）。
--
-- kb_ids 与 knowledge_base 现在在**同一个库**里，但仍是弱引用：知识库是软删的，
-- 所以使用 MUST 按 `deleted_at IS NULL` 过滤，不能假设「能取到就是有效」。


-- -----------------------------------------------------------------------------
-- 2.4 message
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `message` (
  `id`               VARCHAR(32)   NOT NULL COMMENT 'msg_*（网关侧 id；AI 侧另有自生成 id，见 docs/05-§2.0）',
  `conversation_id`  VARCHAR(32)   NOT NULL,
  `user_id`          VARCHAR(64)   NOT NULL COMMENT '冗余，用于隔离校验与批量清理',
  `seq`              INT           NOT NULL COMMENT '会话内自增，排序唯一依据',
  `role`             VARCHAR(16)   NOT NULL COMMENT 'user|assistant',
  `content`          MEDIUMTEXT    NOT NULL,
  `status`           VARCHAR(16)   NOT NULL DEFAULT 'completed' COMMENT 'completed|partial|failed',
  `finish_reason`    VARCHAR(32)   NULL COMMENT 'stop|length|max_steps|canceled',
  `refs`             JSON          NULL COMMENT 'Reference[]，结构见 ai-platform/docs/06-§6',
  `tool_calls`       JSON          NULL COMMENT 'ToolCallTrace[]，结构见 ai-platform/docs/03-§3.1',
  `usage`            JSON          NULL COMMENT '{prompt_tokens,completion_tokens,total_tokens}',
  `model`            VARCHAR(64)   NULL,
  `degraded`         TINYINT(1)    NOT NULL DEFAULT 0,
  `degraded_reasons` JSON          NULL COMMENT 'string[]',
  `elapsed_ms`       INT           NULL,
  `trace_id`         VARCHAR(64)   NULL COMMENT '本轮次 trace，排障用（接缝 J3）',
  `created_at`       DATETIME(3)   NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_msg_conv_seq` (`conversation_id`, `seq`),
  KEY `idx_msg_conv_list` (`conversation_id`, `seq`),
  KEY `idx_msg_user_created` (`user_id`, `created_at`),
  KEY `idx_msg_conv_status` (`conversation_id`, `status`)
) ENGINE=InnoDB COMMENT='消息台账';

-- 列名用 refs 而非 references：后者是 MySQL 8.0 的保留字，避开能省掉一类低级错误。
-- content 用 MEDIUMTEXT 留足余量；不建 FULLTEXT（正文检索应由 AI 侧向量化承担，
-- 而不是在网关加全文索引）。
-- trace_id 是跨服务排障的**唯一可靠关联键** —— message_id 目前两侧各自生成（docs/05-§2.0）。


-- -----------------------------------------------------------------------------
-- 2.5 quota_usage
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `quota_usage` (
  `user_id`     VARCHAR(64)  NOT NULL COMMENT 'JWT sub；宽度与 ai-platform 对齐',
  `metric`      VARCHAR(32)  NOT NULL COMMENT 'chat_requests|llm_tokens|kb_count|documents_count|storage_bytes',
  `period`      VARCHAR(16)  NOT NULL COMMENT '日指标用 YYYY-MM-DD，存量指标固定 current',
  `used`        BIGINT       NOT NULL DEFAULT 0,
  `limit_value` BIGINT       NULL COMMENT '签发周期内的限额快照',
  `created_at`  DATETIME(3)  NOT NULL,
  `updated_at`  DATETIME(3)  NOT NULL,
  -- 业务键即主键（对齐 ai-platform 的 user_settings / conversation_summary）：
  -- 它本来就有一条业务唯一键，再加一个自增 id 就是多余的列与多余的索引。
  -- 计数器更新的 WHERE 条件正好是这三列，直接走聚簇索引。
  PRIMARY KEY (`user_id`, `metric`, `period`),
  -- PK 的前缀是 user_id，按 period 批量对账仍需单独的索引
  KEY `idx_quota_period` (`period`)
) ENGINE=InnoDB COMMENT='配额用量聚合';

-- 本表是配额的**权威**（Redis 只是加速器）：先写 Redis（快路径），每 5 分钟对账写本表，
-- 以 MySQL 周期重建 Redis（docs/05 §4.2）。跨存储，故不在同一事务内。


-- -----------------------------------------------------------------------------
-- 2.6 usage_record
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `usage_record` (
  `id`              BIGINT       NOT NULL AUTO_INCREMENT,
  `user_id`         VARCHAR(64)  NOT NULL COMMENT 'JWT sub；宽度与 ai-platform 对齐',
  `metric`          VARCHAR(32)  NOT NULL,
  `amount`          BIGINT       NOT NULL,
  `conversation_id` VARCHAR(32)  NULL,
  `message_id`      VARCHAR(32)  NULL COMMENT '**网关侧**的 msg_*（不是 AI 返回的那个，见 docs/05-§2.0）',
  `trace_id`        VARCHAR(64)  NULL,
  `created_at`      DATETIME(3)  NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_usage_user_time` (`user_id`, `created_at`),
  KEY `idx_usage_time` (`created_at`)
) ENGINE=InnoDB COMMENT='用量明细';

-- 写入量最大的表（每对话 2 条）。MUST 只保留 USAGE_RETENTION_DAYS（默认 90 天），
-- 由每日任务分批清理；聚合值已在 quota_usage 中，删除明细不丢总账。


-- -----------------------------------------------------------------------------
-- 共享表（不在本脚本定义，只在此声明依赖）
-- -----------------------------------------------------------------------------
-- `idempotency_record` / `audit_log` 由 ai-platform/deploy/mysql/001_init_schema.sql
-- 以**并集**形式建立（含网关侧的 method / request_hash / ip / user_agent）。
-- 网关写这两张表时用的列：
--   idempotency_record：(user_id, method, path, idem_key, request_hash,
--                        status_code, response_body, created_at, expires_at)
--   audit_log         ：(user_id, action, resource_type, resource_id, ip,
--                        user_agent, detail, created_at)
