-- =============================================================================
-- ai_platform · 共享表列对齐（从「分库」迁到「同库」时跑一次）
--
-- 背景：`idempotency_record` / `audit_log` 原本两侧各有一份定义（列不同）。
--       改成同一个库之后同库同名只能留一张表，于是 001 脚本把这两张表升级为
--       **并集**定义（把网关要的 method / request_hash / ip / user_agent 也放进去）。
--
-- 为什么需要本脚本：`001_init_schema.sql` 只写 `CREATE ... IF NOT EXISTS`，
--   对**已经存在**的表是空操作，不会补列。所以在跑过旧版 001 的库上，
--   这两张表仍然缺 4 列，须由本脚本补齐。
--
-- 幂等：每步先查 `information_schema`，缺什么补什么；已对齐则跳过（`DO 0`）。
--       可重复执行，**不含任何 DROP TABLE / DELETE**。
--
-- 执行：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source 002_align_shared_tables.sql"
-- =============================================================================

SET NAMES utf8mb4;
USE `ai_platform`;

-- ---------------------------------------------------------------------------
-- 1) idempotency_record：补 method / request_hash，并把 uk_idem 扩成 4 列
--    （顺序不能反：先有列才能进唯一键）
-- ---------------------------------------------------------------------------

-- 1.1 method
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'idempotency_record'
      AND `COLUMN_NAME` = 'method') = 0,
  'ALTER TABLE `idempotency_record` ADD COLUMN `method` VARCHAR(8) NOT NULL DEFAULT '''' COMMENT ''GET|POST|...；ai-platform 侧不填'' AFTER `user_id`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- 1.2 request_hash
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'idempotency_record'
      AND `COLUMN_NAME` = 'request_hash') = 0,
  'ALTER TABLE `idempotency_record` ADD COLUMN `request_hash` CHAR(64) NOT NULL DEFAULT '''' COMMENT ''sha256(body)，防「同键换请求体」；ai-platform 侧不填'' AFTER `idem_key`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- 1.3 uk_idem：(user_id, path, idem_key) → (user_id, method, path, idem_key)
--     判据是「当前索引有 3 列」而不是「有没有 uk_idem」，这样重复跑第二次不会误改
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`STATISTICS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'idempotency_record'
      AND `INDEX_NAME` = 'uk_idem') = 3,
  'ALTER TABLE `idempotency_record` DROP INDEX `uk_idem`, ADD UNIQUE KEY `uk_idem` (`user_id`, `method`, `path`, `idem_key`)',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------------------
-- 2) audit_log：补 ip / user_agent
-- ---------------------------------------------------------------------------

-- 2.1 ip
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'audit_log'
      AND `COLUMN_NAME` = 'ip') = 0,
  'ALTER TABLE `audit_log` ADD COLUMN `ip` VARCHAR(45) NULL COMMENT ''网关侧填；ai-platform 侧不填'' AFTER `resource_id`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- 2.2 user_agent
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'audit_log'
      AND `COLUMN_NAME` = 'user_agent') = 0,
  'ALTER TABLE `audit_log` ADD COLUMN `user_agent` VARCHAR(255) NULL COMMENT ''网关侧填；ai-platform 侧不填'' AFTER `ip`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------------------
-- 3) 核对：两张表应与 001 脚本的并集定义一致（期望各 4 行）
-- ---------------------------------------------------------------------------
SELECT `TABLE_NAME`, `COLUMN_NAME`, `COLUMN_TYPE`, `IS_NULLABLE`
  FROM `information_schema`.`COLUMNS`
 WHERE `TABLE_SCHEMA` = 'ai_platform'
   AND ((`TABLE_NAME` = 'idempotency_record' AND `COLUMN_NAME` IN ('method', 'request_hash'))
     OR (`TABLE_NAME` = 'audit_log'         AND `COLUMN_NAME` IN ('ip', 'user_agent')))
 ORDER BY `TABLE_NAME`, `ORDINAL_POSITION`;
