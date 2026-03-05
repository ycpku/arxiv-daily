# arXiv Daily (GitHub Actions)

每天自动抓取 arXiv 新论文，支持：

- 按类别独立配置关键词并筛选
- 每个类别内按关键词相关性排序
- 额外推送你关注作者的最新论文
- 生成 Markdown 到 `reports/`

## 1. 配置筛选条件

编辑 `config.yaml`：

- `category_rules`: 类别规则列表
- `category_rules[].name`: 该类别分组名
- `category_rules[].categories`: 该分组关注的 arXiv 分类（如 `cs.AI`）
- `category_rules[].keywords_any`: 标题/摘要至少命中一个关键词
- `category_rules[].keywords_all`: 标题/摘要必须全部命中（可空）
- `category_rules[].match_mode`: `any` 或 `all`
- `category_rules[].authors_any`: 可选，类别内作者限制
- `category_rules[].max_papers`: 该类别日报最多展示多少篇
- `authors_watchlist.authors`: 关注作者名单
- `authors_watchlist.max_papers_per_author`: 每位作者展示几篇最新论文
- `authors_watchlist.lookback_hours`: 作者新增论文时间窗（默认 24 小时）
- `lookback_hours`: 类别筛选看最近多少小时

## 2. GitHub Actions 自动运行

工作流文件：`.github/workflows/daily-arxiv.yml`

- 定时：每天 `00:15 UTC`（北京时间 `08:15`）
- 支持手动触发：`workflow_dispatch`
- 产物：`reports/YYYY-MM-DD.md`
- 会自动提交到仓库

## 3. 本地手动运行（可选）

```bash
pip install -r requirements.txt
python scripts/generate_digest.py --config config.yaml --output-dir reports
```

## 4. 查看每日结果结构

每天生成一个 Markdown 文件，例如：

- `reports/2026-02-28.md`

文件中会包含两个部分：

- `Category Digest`: 按你定义的类别规则输出，并按相关性排序
- `Followed Authors - Latest`: 每位关注作者的最新论文
