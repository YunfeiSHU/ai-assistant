-- =============================================================================
-- ai-platform · 建表脚本自检（对应 docs/09-§7 的 AC-DATA-01）
--
-- 执行：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source verify_schema.sql"
--
-- 设计：**全程不产生任何错误**，每步用 `ROW_COUNT()` 输出一个应当等于期望值的
-- 数字。用 `INSERT IGNORE` 把唯一冲突降级为 warning，是为了让「期望的冲突」
-- 与「真正的失败」不同形 —— 若脚本任何一步报 ERROR，就说明真的有问题。
--
-- 期望输出（依次是 1 / 0 / 1 / 1 / 0 / 1，再加两个共享表断言）：
--   first_insert_must_be_1            = 1
--   second_insert_must_be_0           = 0
--   after_soft_delete_must_be_1       = 1
--   doc_first_must_be_1               = 1
--   doc_duplicate_must_be_0           = 0
--   doc_after_soft_delete_must_be_1   = 1
--   shared_extra_columns_must_be_4    = 4   共享表的 method/request_hash/ip/user_agent
--   uk_idem_columns_must_be_4         = 4   uk_idem 已含 method
--
-- 脚本幂等：开头与结尾都清理探针数据（user_id = 'u_ddl_probe'）。
-- =============================================================================

SET NAMES utf8mb4;
USE `ai_platform`;

DELETE FROM `document` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `knowledge_base` WHERE `user_id` = 'u_ddl_probe';

-- 1) 第一条：应插入成功
INSERT IGNORE INTO `knowledge_base`
  (`id`, `user_id`, `name`, `description`, `chunk_size`, `chunk_overlap`,
   `embedding_dim`, `retrieval_top_k`, `rerank_top_n`, `score_threshold`, `status`,
   `document_count`, `chunk_count`, `created_at`, `updated_at`)
VALUES
  ('kb_ddl_probe_1', 'u_ddl_probe', '同名库', '', 512, 64,
   1024, 20, 5, 0, 'active',
   0, 0, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `first_insert_must_be_1`;

-- 2) 同名 + deleted_at IS NULL → 撞 uk_kb_user_name，IGNORE 掉记 0 行
INSERT IGNORE INTO `knowledge_base`
  (`id`, `user_id`, `name`, `description`, `chunk_size`, `chunk_overlap`,
   `embedding_dim`, `retrieval_top_k`, `rerank_top_n`, `score_threshold`, `status`,
   `document_count`, `chunk_count`, `created_at`, `updated_at`)
VALUES
  ('kb_ddl_probe_2', 'u_ddl_probe', '同名库', '', 512, 64,
   1024, 20, 5, 0, 'active',
   0, 0, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `second_insert_must_be_0`;

-- 3) 软删后 deleted_at 非 NULL → 不参与唯一性 → 同名可以重建
UPDATE `knowledge_base` SET `deleted_at` = UTC_TIMESTAMP(3) WHERE `id` = 'kb_ddl_probe_1';
INSERT IGNORE INTO `knowledge_base`
  (`id`, `user_id`, `name`, `description`, `chunk_size`, `chunk_overlap`,
   `embedding_dim`, `retrieval_top_k`, `rerank_top_n`, `score_threshold`, `status`,
   `document_count`, `chunk_count`, `created_at`, `updated_at`)
VALUES
  ('kb_ddl_probe_3', 'u_ddl_probe', '同名库', '', 512, 64,
   1024, 20, 5, 0, 'active',
   0, 0, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `after_soft_delete_must_be_1`;

-- 4) document 去重：未删时同一文件只能一份；软删后允许重新上传
INSERT IGNORE INTO `document`
  (`id`, `kb_id`, `user_id`, `doc_name`, `file_ext`, `mime_type`, `size_bytes`,
   `object_key`, `content_sha256`, `status`, `chunk_size`, `chunk_overlap`,
   `created_at`, `updated_at`)
VALUES
  ('doc_ddl_probe_1', 'kb_ddl_probe', 'u_ddl_probe', '手册.pdf', '.pdf',
   'application/pdf', 1024, 'u_ddl_probe/kb_ddl_probe/doc_ddl_probe_1/手册.pdf',
   REPEAT('a', 64), 'INDEXED', 512, 64, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `doc_first_must_be_1`;

INSERT IGNORE INTO `document`
  (`id`, `kb_id`, `user_id`, `doc_name`, `file_ext`, `mime_type`, `size_bytes`,
   `object_key`, `content_sha256`, `status`, `chunk_size`, `chunk_overlap`,
   `created_at`, `updated_at`)
VALUES
  ('doc_ddl_probe_2', 'kb_ddl_probe', 'u_ddl_probe', '手册副本.pdf', '.pdf',
   'application/pdf', 1024, 'u_ddl_probe/kb_ddl_probe/doc_ddl_probe_2/手册副本.pdf',
   REPEAT('a', 64), 'INDEXED', 512, 64, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `doc_duplicate_must_be_0`;

UPDATE `document` SET `deleted_at` = UTC_TIMESTAMP(3) WHERE `id` = 'doc_ddl_probe_1';
INSERT IGNORE INTO `document`
  (`id`, `kb_id`, `user_id`, `doc_name`, `file_ext`, `mime_type`, `size_bytes`,
   `object_key`, `content_sha256`, `status`, `chunk_size`, `chunk_overlap`,
   `created_at`, `updated_at`)
VALUES
  ('doc_ddl_probe_3', 'kb_ddl_probe', 'u_ddl_probe', '手册重传.pdf', '.pdf',
   'application/pdf', 1024, 'u_ddl_probe/kb_ddl_probe/doc_ddl_probe_3/手册重传.pdf',
   REPEAT('a', 64), 'INDEXED', 512, 64, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3));
SELECT ROW_COUNT() AS `doc_after_soft_delete_must_be_1`;

-- 5) 每个 uk_* 唯一索引都应在位（期望 6 行）
--    注意只有 6 张表带业务唯一键：conversation_summary / user_settings / audit_log
--    只靠主键，不应该报「缺索引」；网关独占表的唯一键在 go-services 的自检脚本里查
SELECT `TABLE_NAME`, `INDEX_NAME`,
       GROUP_CONCAT(`COLUMN_NAME` ORDER BY `SEQ_IN_INDEX`) AS `columns`
  FROM `information_schema`.`STATISTICS`
 WHERE `TABLE_SCHEMA` = 'ai_platform' AND `INDEX_NAME` LIKE 'uk\_%'
 GROUP BY `TABLE_NAME`, `INDEX_NAME`
 ORDER BY `TABLE_NAME`;

-- 6) 共享表（与网关共用）应当带上网关需要的 4 列（期望 4）
--    `idempotency_record` / `audit_log` 是同库同名的单张表，网关的列已并到本脚本里
SELECT COUNT(*) AS `shared_extra_columns_must_be_4`
  FROM `information_schema`.`COLUMNS`
 WHERE `TABLE_SCHEMA` = 'ai_platform'
   AND ((`TABLE_NAME` = 'idempotency_record' AND `COLUMN_NAME` IN ('method', 'request_hash'))
     OR (`TABLE_NAME` = 'audit_log'         AND `COLUMN_NAME` IN ('ip', 'user_agent')));

-- 7) uk_idem 应当是 4 列（user_id, method, path, idem_key）；仍为 3 列说明
--    旧库还没跑过 002_align_shared_tables.sql（期望 4）
SELECT COUNT(*) AS `uk_idem_columns_must_be_4`
  FROM `information_schema`.`STATISTICS`
 WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'idempotency_record'
   AND `INDEX_NAME` = 'uk_idem';

-- 8) 清理
DELETE FROM `document` WHERE `user_id` = 'u_ddl_probe';
DELETE FROM `knowledge_base` WHERE `user_id` = 'u_ddl_probe';
SELECT ROW_COUNT() AS `kb_probe_rows_cleaned_must_be_2`;
