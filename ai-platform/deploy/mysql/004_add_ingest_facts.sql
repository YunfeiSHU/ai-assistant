-- =============================================================================
-- ai_platform · 入库事实（截断可见 + 切片进度）补列（在已建库上跑一次）
--
-- 背景（docs/10-大文件上传与索引优化清单.md 的 UP-01 / UP-02）：
--
--   UP-01 · 截断可见
--     8MB 测试正文实测切出 **16,969** 片，被 MAX_DOC_CHUNKS=10000 截断成
--     **10,000** 片 —— 丢 41% 正文，而代码只打了一条 `ingest.chunks_truncated`
--     warning、接口照旧回 202。调用方因此拿不到「这个文档只入了一半」这个事实，
--     「入库成功」是假的。
--     新增 `document.chunks_total`（切分产出数）与 `document.truncated`，
--     与既有的 `chunk_count`（实际入库数）并存，三者的关系自证：
--       chunks_total > chunk_count  ⇒  truncated = 1
--
--   UP-02 · 切片进度
--     任务的 `progress` 只能画百分比；`embedding` 阶段会饱和在 95，
--     之后只剩「还在跑」。新增 `task.chunks_total` / `task.chunks_done`
--     让客户端能算 ETA，也能分辨「在算」与「卡住」。
--     口径：`chunks_total` 是**截断后**待向量化的切片数（= 进度条分母），
--     不是 `document.chunks_total`（截断前产出数）—— 两个量不同，故意不混用。
--
-- 幂等：先查 information_schema，缺才补；已存在则跳过（`DO 0`）。
--       可重复执行，**不含任何 DROP TABLE / DELETE / 数据改写**。
--
-- 执行：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source 004_add_ingest_facts.sql"
--
-- 注：全新环境跑 001_init_schema.sql 就已带上这些列，无需本脚本。
-- =============================================================================

SET NAMES utf8mb4;
USE `ai_platform`;

-- ---------------------------------------------------------------------------
-- document.chunks_total / document.truncated
--   放在 char_count 之后：与 Document 实体（app/infrastructure/storage/base.py）
--   里这两个字段的位置一致，`SELECT *` 的人眼顺序与代码阅读顺序一样。
-- ---------------------------------------------------------------------------
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'document'
      AND `COLUMN_NAME` = 'chunks_total') = 0,
  'ALTER TABLE `document` ADD COLUMN `chunks_total` INT NULL COMMENT ''切分产出切片数（截断前）；NULL=还没走到切分'' AFTER `char_count`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'document'
      AND `COLUMN_NAME` = 'truncated') = 0,
  'ALTER TABLE `document` ADD COLUMN `truncated` TINYINT(1) NOT NULL DEFAULT 0 COMMENT ''是否因 MAX_DOC_CHUNKS 丢弃尾部切片'' AFTER `chunks_total`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------------------
-- task.chunks_total / task.chunks_done
--   当前任务仓储实现在 Redis（app/tasks/redis_store.py），这两列是为
--   「换回 MySQL 权威存储」预留。加在这里是为了避免「代码有字段、DDL 没列」
--   的漂移 —— 历史上已因同一原因补过一次 document_chunk.metadata（见 003）。
-- ---------------------------------------------------------------------------
SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'task'
      AND `COLUMN_NAME` = 'chunks_total') = 0,
  'ALTER TABLE `task` ADD COLUMN `chunks_total` INT NOT NULL DEFAULT 0 COMMENT ''本次待向量化切片数（截断后）'' AFTER `stage`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @ddl := IF(
  (SELECT COUNT(*) FROM `information_schema`.`COLUMNS`
    WHERE `TABLE_SCHEMA` = 'ai_platform' AND `TABLE_NAME` = 'task'
      AND `COLUMN_NAME` = 'chunks_done') = 0,
  'ALTER TABLE `task` ADD COLUMN `chunks_done` INT NOT NULL DEFAULT 0 COMMENT ''已完成向量化的切片数'' AFTER `chunks_total`',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ---------------------------------------------------------------------------
-- 核对：期望 4 行
-- ---------------------------------------------------------------------------
SELECT `TABLE_NAME`, `COLUMN_NAME`, `COLUMN_TYPE`, `IS_NULLABLE`
  FROM `information_schema`.`COLUMNS`
 WHERE `TABLE_SCHEMA` = 'ai_platform'
   AND ((`TABLE_NAME` = 'document' AND `COLUMN_NAME` IN ('chunks_total', 'truncated'))
     OR (`TABLE_NAME` = 'task' AND `COLUMN_NAME` IN ('chunks_total', 'chunks_done')))
 ORDER BY `TABLE_NAME`, `ORDINAL_POSITION`;
