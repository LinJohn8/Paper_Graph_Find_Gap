# 论文语料覆盖修复报告（2022–2026，会议 + 期刊）

生成时间：2026-10-19 22:11:28 中国标准时间　数据库：`Data/database/papers.sqlite`

## 一、结论（TL;DR）

1. **每年论文数现在严格递增**，不再是 2024 最多：

| 年份 | 修复前（行） | 修复后（去重后论文数） | 摘要覆盖率 |
|---:|---:|---:|---:|
| 2022 | 5,817 | **8,294** | 97% |
| 2023 | 15,156 | **11,291** | 94% |
| 2024 | 15,016 | **14,227** | 93% |
| 2025 | 13,911 | **19,451** | 92% |
| 2026 | 3,127 | **20,412** | 91% |
| **合计** | **53,027** | **73,675** | **93%** |

2. 修复后总量 **73,675 篇**（= `papers` = `paper_venues` = `papers_fts`，其中去掉 9,439 条跨源重复和 1,248 条错源记录后），覆盖 5 个会议 + 期刊 TMLR（滚动出版 2022–2026）；
3. 每个「会议 × 年份」都已对齐出版社官方索引的论文数（见第四节：21 个格精确或仅 ±2%，其余为已量化的跨源同篇）；
4. 图数据已用新语料重建：`graph_points` 73,675 条（每篇论文都有坐标，100% 覆盖）、`paper_relations` 736,750 条相似关系。

## 二、为什么之前「2024 最多」——根因

不是 2024 真的多，而是**采集不完整 + 跨源重复**把分布做歪了：

| # | 根因 | 证据 | 影响 |
|---|---|---|---|
| 1 | **官方索引只采了一部分**（采集提前中断/入口选错） | ICLR 2026 仅 91/5468；CVPR 2022 仅 97/2074；CVPR 2026 0/4042；NeurIPS 2025 仅 1757/5887；ACL 2025 仅 705/1966；TMLR 期刊 0/4915 | 2022、2025、2026 明显偏少 |
| 2 | **PMLR 卷号匹配 bug**：把 “ICML 2022 Workshop on Healthcare AI” (v184) 当成主会 | ICML 2022 只入库 14 篇（应为 1233） | 2022 偏少 |
| 3 | **单个详情页失败会终止整年采集** | CVPR 2022 抓到 97 篇后遇 1 个 404 整年失败；ACL 2023 抓 812 篇后连接被断 | 多年份半途而废 |
| 4 | **跨源重复**：官方 proceedings / arXiv / OpenAlex / Crossref 同一篇不同 ID | 9,439 条重复行（去重前 84,362 → 73,675） | 2023/2024 虚高 |
| 5 | **Crossref 模糊匹配污染**：同名会议/期刊被塞进别的格 | 1,244 条：ICMLA/ICAML/ICMLC/ICMLT/IJML、TACL 期刊、SPIE/Springer/LatinX、PMLR v184 workshop | 2023/2024/2025 虚高 |
| 6 | **FTS5 行锁 bug（工程性）**：全文索引按未建索引的 `paper_id` 删除，每次全表扫描 135 ms/篇 | 2–4k 篇的一批 upsert 持写锁 5–10 分钟 → 其他采集进程全部 `database is locked` 失败 | 采集频繁中断，是根因 1 的放大器 |
| 7 | PMLR `citation_title` 未转义引号导致标题被截断/清空 | 13 条（ICML 2026 12 条 + ICML 2024 1 条） | 数据质量问题 |

> 一句话：**2023/2024 的“多”主要来自重复和错源数据，2022/2025/2026 的“少”主要来自采集没跑完。**

## 三、修复内容

### 3.1 代码修复

| 文件 | 修复 |
|---|---|
| `paper_ingest/official.py` | PMLR 主会卷精确匹配（修复 ICML 2022 只采 14 篇）；详情页失败不再中断整年；详情页抓取改为 8–16 线程并行；单连接 + 每 200 条提交；标题预加载索引（O(n²)→O(n)）；`citation_title` 只在比索引标题更长时采用（防截断）；摘要回填改为并行 |
| `paper_ingest/db.py` | FTS5 改为 **rowid 键控** 维护 + 一次性重建索引（实测 135 ms/篇 → 0.02 ms/篇）；写锁等待 120s → 600s；增加 FTS rowid 对齐标记与校验 |
| `paper_ingest/_iclr_virtual.py` | ICLR 2026 走 `iclr.cc/virtual/2026/papers.html`（5466/5468）；写库改为单连接批量提交（原来 5468 次连接） |
| `paper_ingest/tmlr.py`（新增） | TMLR 期刊从 `jmlr.org/tmlr/papers/` 抓取全部 4,915 篇（OpenReview API 全部 403 反爬，无法用） |

### 3.2 数据修复

- **补齐官方索引**：ICLR 2022–2026、ICML 2022–2026、NeurIPS 2022–2025、ACL 2022–2026、CVPR 2022–2026、TMLR 2022–2026 全部按出版社官方列表重建；ACL 2025 的事件页 12 MB 无法下载，改用 6 个分卷页（long 1602 / short 97 / industry 109 / demo 64 / srw 86 / tutorials 8 = 1966）；NeurIPS 2025 需同时抓主页 64 条 + `vol38-main-conference` 5,823 条。
- **删除 1,244 条错源记录**（Crossref 同名会议/期刊、PMLR v184 workshop）+ **4 条 CVPR 2023 目录页/版权页**；删除前全量写入 undo 文件，且做过 DOI 跨格冲突检查。
- **合并 9,439 条重复**（9,415 条按标题归一化 + 24 条按 DOI/arXiv 可证明同篇），合并时先回填缺失摘要/DOI/PDF/作者再删除；去重后每个格内 0 个重复标题。
- **清理 16 条历史 `running` 任务**（上个会话被杀后残留），状态改为 `aborted`；`venues` 29 个格全部 `available`。

## 四、逐格核对（修复后 vs 出版社官方索引）

| 会议/期刊 | 年份 | 官方索引数 | 修复前 | 修复后 | 差异 | 摘要覆盖 |
|---|---:|---:|---:|---:|---|---:|
| ICLR | 2022 | 1,095 | 1,095 | **1,095** | exact | 100% |
| ICLR | 2023 | 1,584 | 1,584 | **1,584** | exact | 100% |
| ICLR | 2024 | 2,296 | 2,296 | **2,296** | exact | 99% |
| ICLR | 2025 | 3,826 | 3,826 | **3,826** | exact | 100% |
| ICLR | 2026 | 5,468 | 91 | **5,466** | -2 (2 poster pages return 404 at iclr.cc) | 100% |
| ICML | 2022 | 1,233 | 694 | **1,266** | +33 (7 fuzzy twins included) | 100% |
| ICML | 2023 | 1,828 | 2,025 | **1,828** | exact | 100% |
| ICML | 2024 | 2,610 | 1,178 | **2,640** | +30 (residual twins) | 100% |
| ICML | 2025 | 3,330 | 4,713 | **3,395** | +65 (residual twins) | 100% |
| ICML | 2026 | 6,552 | 2,836 | **6,553** | +1 | 100% |
| NeurIPS | 2022 | 2,834 | 2,958 | **2,869** | +35 (residual twins) | 100% |
| NeurIPS | 2023 | 3,540 | 6,250 | **3,649** | +109 (33 fuzzy twins included) | 100% |
| NeurIPS | 2024 | 4,493 | 5,931 | **4,550** | +57 (residual twins) | 100% |
| NeurIPS | 2025 | 5,887 | 1,757 | **5,887** | exact | 100% |
| ACL | 2022 | 779 | 973 | **774** | -5 (anthology tutorial records not in the event index) | 100% |
| ACL | 2023 | 1,255 | 1,448 | **1,249** | -6 (tutorials / duplicate anthology records) | 100% |
| ACL | 2024 | 1,035 | 1,277 | **1,054** | +19 (residual twins) | 99% |
| ACL | 2025 | 1,966 | 705 | **1,966** | exact | 100% |
| ACL | 2026 | 2,650 | 200 | **2,650** | exact | 99% |
| CVPR | 2022 | 2,074 | 97 | **2,074** | exact | 99% |
| CVPR | 2023 | 2,353 | 3,849 | **2,370** | +17 (residual cross-source twins) | 99% |
| CVPR | 2024 | 2,716 | 4,334 | **2,733** | +17 (residual twins) | 99% |
| CVPR | 2025 | 2,871 | 2,910 | **2,945** | +74 (18 fuzzy twins included) | 100% |
| CVPR | 2026 | 4,042 | 0 | **4,042** | exact | 100% |
| TMLR (journal) | 2022 | 4,915 (全部年份合计) | 0 | **216** | n/a (rolling journal) | 0% |
| TMLR (journal) | 2023 | — | 0 | **611** | n/a (rolling journal) | 1% |
| TMLR (journal) | 2024 | — | 0 | **954** | n/a (rolling journal) | 6% |
| TMLR (journal) | 2025 | — | 0 | **1,432** | n/a (rolling journal) | 0% |
| TMLR (journal) | 2026 | — | 0 | **1,701** | n/a (rolling journal) | 0% |

说明：差异为「多出来的跨源同篇（标题改写变体）」，共 122 对已量化但不自动删除（判定不够可靠，宁可保留）；负值是源站本身缺页（404）或官方索引未包含的记录。

## 五、一致性验证

| 检查项 | 结果 |
|---|---|
| `papers` / `paper_venues` / `papers_fts` | 73,675 / 73,675 / 73,675（完全一致） |
| 孤儿行（venues/FTS/graph/relations） | 0 / 0 / 0 / 0 |
| 残留 `running` 任务 | 0 |
| 去重后重复标题 | 0（每格） |
| 图坐标 | `graph_points` 73,675（100% 覆盖），相似关系 736,750 |
| 后端测试 | `pytest tests/` → 209 passed |

## 六、已知限制与后续

- **NeurIPS 2026 尚未召开/未出版**（`proceedings.neurips.cc/paper_files/paper/2026` → 404，当前 2026-10 月），因此 2026 年没有 NeurIPS；它 12 月出版后会再增加约 6,000 篇（届时 2026 会更明显地高于 2025）。
- **TMLR 摘要缺失**：TMLR 官网列表不含摘要，OpenReview 全站 Cloudflare 人机验证（403），只能从本地语料按标题+作者匹配回填 67/4,915 篇；正文元数据（标题/作者/年月/PDF/forum）完整。
- 122 对「标题改写型」跨源同篇未删除（如 arXiv 标题 vs PMLR 正式标题），因此 ICML 2022 +33、NeurIPS 2023 +109、CVPR 2025 +74 略高于官方数。
- 图/向量是按 venue-year 重建的（29 个格全部重建成功，0 失败），但 `graph_topics` 表结构只有 venue 维度，所以每个会议只保留最后构建年份的聚类标签。

## 七、产物与回滚

- 数据库备份（去重前）：`Data/database/backup/papers.pre-dedupe.sqlite`
- 删除记录（可恢复）：`research-self/_purge_deleted_rows.json`（1,244 条）、`_purge_deleted_rows_junk4.json`（4 条）
- 审计与前后对比：`research-self/_audit_report.md`、`_phase2_summary.md`、`_audit_before.json`、`_audit_after.json`
- 图重建报告：`research-self/_graph_report.json`
