-- =============================================================================
-- ai_platform · document_chunk 补 metadata 列（在已建库上跑一次）
--
-- 背景：切片实体（app/infrastructure/storage/base.py 的 Chunk）带 metadata 字段，入库时由
--       app/rag/service.py::_build_chunks **固定写入**切分参数：
--         {"doc_name": ..., "chunk_size": ..., "chunk_overlap": ..., "merged": ...}
--       用途是「KB 之后改了 chunk_size，老切片仍能解释自己」。
--
--       但 001 脚本最初的 document_chunk 定义漏了这一列（docs/09 §2.3 也没写）。
--       列不存在时写入会报 MySQL 1054，被 app/infrastructure/mysql/db.py 分类成
--       503 DEPENDENCY_UNAVAILABLE（"请先执行建表脚本"）——面向用户像是
--       「服务挂了」，实际是**表结构落后于代码**。所以必须补列。
--
--   同时已更新 001_init_schema.sql：全新环境跑 001 就带上该列，无需本脚本。
--
-- 幂等：先查 information_schema，缺才补；已存在则跳过（`DO 0`）。
--       可重复执行，**不含任何 DROP TABLE / DELETE / 数据改写**。
--
-- 执行：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source 003_add_chunk_metadata.sql"
-- =============================================================================

SET NAMES utf8mb4;
USE `ai_platform`;

-- ---------------------------------------------------------------------------
-- document_chunk.metadata
--   放在 token_count 之后、created_at 之前：与 Chunk 实体的字段顺序一致，
--   这样 `SELECT *` 出来的人眼对齐顺序与代码里读的顺序一样。
-- ---------------------------------------------------------------------------
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'document_chunk'
      AND `COLUMN_NAME` = 'metadata') = 0,
  'ALTER TABLE `document_chunk` ADD COLUMN `metadata` JSON NULL COMMENT ''切分参数固化：doc_name/chunk_size/chunk_overlap/merged'' AFTER `token_count`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------------------
-- 核对：期望 1 行
-- ---------------------------------------------------------------------------
SELECT `TABLE_NAME`, `COLUMN_NAME`, `COLUMN_TYPE`, `IS_NULLABLE`
  FROM `information_schema`.`COLUMNS`
 WHERE `TABLE_SCHEMA` = 'ai_platform'
   AND `TABLE_NAME` = 'document_chunk'
   AND `COLUMN_NAME` = 'metadata';
