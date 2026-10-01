-- =============================================================================
-- go-services · 建表脚本自检（对应 docs/05-§6 的 AC-DATA-07 ~ AC-DATA-10）
--
-- 执行：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source verify_schema.sql"
--
-- 前置：已跑过 ai-platform/deploy/mysql/001_init_schema.sql 与
--       go-services/deploy/mysql/001_gateway_tables.sql（同一个库 `ai_platform`）。
--
-- 设计：**全程不产生任何错误**，每步用 `ROW_COUNT()` 输出一个应当等于期望值的数字。
-- 用 `INSERT IGNORE` 把唯一冲突降级为 warning，是为了让「期望的冲突」与「真正的失败」
-- 不同形 —— 若脚本任何一步报 ERROR，就说明真的有问题。
--
-- 期望输出（依次）：
--   gateway_tables_total_must_be_6        = 6     网关独占表数量
--   total_tables_must_be_15               = 15    单库合计
--   user_first_insert_must_be_1           = 1
--   user_dup_email_must_be_0              = 0
--   user_email_after_delete_must_be_0     = 0     ← 邮箱**不可复用**（刻意，与 AI 侧相反）
--   conv_seq_first_must_be_1              = 1     LAST_INSERT_ID 分配 seq
--   conv_seq_second_must_be_2             = 2
--   msg_dup_seq_must_be_0                 = 0
--   quota_first_insert_must_be_1          = 1
--   quota_dup_metric_must_be_0            = 0
--   rt_dup_hash_must_be_0                 = 0
--   idem_dup_key_must_be_0                = 0     ← 用**共享表**的 4 列唯一键
--   shared_extra_columns_must_be_4        = 4     method/request_hash/ip/user_agent
--   quota_has_no_surrogate_id_must_be_0   = 0     ← 业务键即主键（对齐 AI 侧风格）
--   user_probe_rows_cleaned_must_be_1     = 1
--
-- 脚本幂等：开头与结尾都清理探针数据（user_id = 'u_ddl_probe'）。
-- =============================================================================

SET NAMES utf8mb4;
USE `ai_platform`;

DELETE FROM `message` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `usage_record` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `quota_usage` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `refresh_token` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `conversation` WHERE `id` = 'cv_ddl_probe';
DELETE FROM `idempotency_record` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `audit_log` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `user` WHERE `id` = 'u_ddl_probe';

-- 1) 表集合：网关独占 6 张 + 单库合计 15 张
--    （15 = 网关 6 + AI 独占 7 + 共享 2；共享表只有一份定义，不会重复计数）
SELECT COUNT(*) AS `gateway_tables_total_must_be_6`
  FROM `information_schema`.`TABLES`
 WHERE `TABLE_SCHEMA` = 'ai_platform'
   AND `TABLE_NAME` IN ('user', 'refresh_token', 'conversation', 'message',
                        'quota_usage', 'usage_record');

SELECT COUNT(*) AS `total_tables_must_be_15`
  FROM `information_schema`.`TABLES`
 WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_TYPE` = 'BASE TABLE';

-- 2) 用户名唯一键：第一条成功
INSERT IGNORE INTO `user`
  (`id`, `email`, `nickname`, `password_hash`, `plan`, `status`, `token_version`,
   `created_at`, `updated_at`)
VALUES
  ('u_ddl_probe', 'probe@example.com', 'probe', 'argon2id$probe', 'free', 'active', 1,
   UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `user_first_insert_must_be_1`;

-- 3) 同邮箱再插 → 撞 uk_user_email，IGNORE 掉记 0 行
INSERT IGNORE INTO `user`
  (`id`, `email`, `nickname`, `password_hash`, `plan`, `status`, `token_version`,
   `created_at`, `updated_at`)
VALUES
  ('u_ddl_probe_dup', 'probe@example.com', 'probe2', 'argon2id$probe', 'free', 'active', 1,
   UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `user_dup_email_must_be_0`;

-- 4) 软删后同邮箱**仍然**插不进去 —— 这是刻意语义（邮箱永久占用），
--    与 ai-platform 的 knowledge_base（软删后可重建同名）恰好相反
UPDATE `user` SET `deleted_at` = UTC_TIMESTAMP(3) WHERE `id` = 'u_ddl_probe';
INSERT IGNORE INTO `user`
  (`id`, `email`, `nickname`, `password_hash`, `plan`, `status`, `token_version`,
   `created_at`, `updated_at`)
VALUES
  ('u_ddl_probe_after', 'probe@example.com', 'probe3', 'argon2id$probe', 'free', 'active', 1,
   UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `user_email_after_delete_must_be_0`;

-- 5) seq 分配：LAST_INSERT_ID(expr) 是**连接级**的，mysql 的 source 全程同一连接，
--    所以这里能正确演示「连续两次分配得到 1 与 2」
INSERT IGNORE INTO `conversation`
  (`id`, `user_id`, `title`, `title_source`, `status`, `message_count`,
   `pinned`, `created_at`, `updated_at`)
VALUES ('cv_ddl_probe', 'u_ddl_probe', '自检会话', 'auto', 'active', 0,
        0, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));

UPDATE `conversation` SET `message_count` = LAST_INSERT_ID(`message_count` + 1)
 WHERE `id` = 'cv_ddl_probe';
SELECT LAST_INSERT_ID() AS `conv_seq_first_must_be_1`;

UPDATE `conversation` SET `message_count` = LAST_INSERT_ID(`message_count` + 1)
 WHERE `id` = 'cv_ddl_probe';
SELECT LAST_INSERT_ID() AS `conv_seq_second_must_be_2`;

-- 6) message 的 (conversation_id, seq) 唯一键：seq 不可重
INSERT IGNORE INTO `message`
  (`id`, `conversation_id`, `user_id`, `seq`, `role`, `content`, `status`, `created_at`)
VALUES ('msg_ddl_probe_1', 'cv_ddl_probe', 'u_ddl_probe', 1, 'user', '你好', 'completed',
        UTC_TIMESTAMP(3));
INSERT IGNORE INTO `message`
  (`id`, `conversation_id`, `user_id`, `seq`, `role`, `content`, `status`, `created_at`)
VALUES ('msg_ddl_probe_2', 'cv_ddl_probe', 'u_ddl_probe', 1, 'user', '你好', 'completed',
        UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `msg_dup_seq_must_be_0`;

-- 7) quota_usage：业务键 (user_id, metric, period) 已是主键，重复插被拦住
INSERT IGNORE INTO `quota_usage`
  (`user_id`, `metric`, `period`, `used`, `created_at`, `updated_at`)
VALUES ('u_ddl_probe', 'chat_requests', '2026-09-28', 1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `quota_first_insert_must_be_1`;

INSERT IGNORE INTO `quota_usage`
  (`user_id`, `metric`, `period`, `used`, `created_at`, `updated_at`)
VALUES ('u_ddl_probe', 'chat_requests', '2026-09-28', 2, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `quota_dup_metric_must_be_0`;

-- 8) refresh_token 只存哈希，且哈希唯一
INSERT IGNORE INTO `refresh_token`
  (`id`, `user_id`, `token_hash`, `device_name`, `user_agent`, `ip`,
   `expires_at`, `created_at`)
VALUES ('rt_ddl_probe_1', 'u_ddl_probe', REPEAT('a', 64), '', '', '',
        UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
INSERT IGNORE INTO `refresh_token`
  (`id`, `user_id`, `token_hash`, `device_name`, `user_agent`, `ip`,
   `expires_at`, `created_at`)
VALUES ('rt_ddl_probe_2', 'u_ddl_probe', REPEAT('a', 64), '', '', '',
        UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `rt_dup_hash_must_be_0`;

-- 9) 共享表 idempotency_record：用**网关侧那 4 列**唯一键插入，
--    能拦住重复就说明共享表确实带上了 method / request_hash（同库单一权威的证明）
INSERT IGNORE INTO `idempotency_record`
  (`user_id`, `method`, `path`, `idem_key`, `request_hash`,
   `status_code`, `response_body`, `created_at`, `expires_at`)
VALUES ('u_ddl_probe', 'POST', '/api/v1/conversations', 'probe-key', REPEAT('b', 64),
        201, JSON_OBJECT('ok', TRUE), UTC_TIMESTAMP(3), UTC_TIMESTAMP(3) + INTERVAL 1 DAY);
SELECT ROW_COUNT() AS `idem_first_insert_must_be_1`;

INSERT IGNORE INTO `idempotency_record`
  (`user_id`, `method`, `path`, `idem_key`, `request_hash`,
   `status_code`, `response_body`, `created_at`, `expires_at`)
VALUES ('u_ddl_probe', 'POST', '/api/v1/conversations', 'probe-key', REPEAT('b', 64),
        201, JSON_OBJECT('ok', TRUE), UTC_TIMESTAMP(3), UTC_TIMESTAMP(3) + INTERVAL 1 DAY);
SELECT ROW_COUNT() AS `idem_dup_key_must_be_0`;

-- 10) 共享表上「网关才要的 4 列」应当在位（期望 4）
SELECT COUNT(*) AS `shared_extra_columns_must_be_4`
  FROM `information_schema`.`COLUMNS`
 WHERE `TABLE_SCHEMA` = 'ai_platform'
   AND ((`TABLE_NAME` = 'idempotency_record' AND `COLUMN_NAME` IN ('method', 'request_hash'))
     OR (`TABLE_NAME` = 'audit_log'         AND `COLUMN_NAME` IN ('ip', 'user_agent')));

-- 11) quota_usage 不应有代理主键 `id`（期望 0）：业务键即主键，对齐 AI 侧风格
SELECT COUNT(*) AS `quota_has_no_surrogate_id_must_be_0`
  FROM `information_schema`.`COLUMNS`
 WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'quota_usage'
   AND `COLUMN_NAME` = 'id';

-- 12) 全库唯一索引清单（期望 9 行：AI 侧 6 + 网关 3；uk_idem 是共享表共同的一条）
SELECT `TABLE_NAME`, `INDEX_NAME`,
       GROUP_CONCAT(`COLUMN_NAME` ORDER BY `SEQ_IN_INDEX`) AS `columns`
  FROM `information_schema`.`STATISTICS`
 WHERE `TABLE_SCHEMA` = 'ai_platform' AND `INDEX_NAME` LIKE 'uk\_%'
 GROUP BY `TABLE_NAME`, `INDEX_NAME`
 ORDER BY `TABLE_NAME`;

-- 13) 单库下所有 `user_id` 列都应是 varchar(64)（期望 14 行，无 varchar(32)）
--     网关 7 列（user 表自身没有 user_id）+ AI 7 列 = 14
SELECT `TABLE_NAME`, `COLUMN_NAME`, `COLUMN_TYPE`
  FROM `information_schema`.`COLUMNS`
 WHERE `TABLE_SCHEMA` = 'ai_platform' AND `COLUMN_NAME` = 'user_id'
 ORDER BY `TABLE_NAME`;

-- 14) 清理
DELETE FROM `message` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `quota_usage` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `refresh_token` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `conversation` WHERE `id` = 'cv_ddl_probe';
DELETE FROM `idempotency_record` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `user` WHERE `id` = 'u_ddl_probe';
SELECT ROW_COUNT() AS `user_probe_rows_cleaned_must_be_1`;
