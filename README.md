# Overleaf Review Bridge — 只读批注同步与归档 (MVP v0.1)

目标：将 Overleaf **原生批注 + 所选原文 + 上下文 + 文件/行号 + 讨论回复 + 作者 + 时间 + 已解决状态** 保存为结构化数据；后续可在此基础上做 MCP、Skill、自动批注和审稿助手。

> 当前版本**不会修改 Overleaf 项目**，不会写回评论，也不会保存登录 Cookie、密码或密钥。能够测试离线数据解析；实际在线拉取需要用户自己的 Overleaf 项目会话，本开发环境无法代替登录验收。

## 1. 最容易的方式：浏览器一键导出，再归档

1. 安装已有的 MIT 开源扩展 [Overleaf Comments Export (Chrome 商店)](https://chromewebstore.google.com/detail/overleaf-comments-export/nbbappjfcankkjnpbaopjhejgdagaglc)。适用于 Chrome、Edge 等 Chromium 浏览器。
2. 正常登录 Overleaf，进入有批注的项目。使用该扩展导出 **JSON**（包含所选原文及回复）。请勿只下载原生 `.tex` ZIP；它不含评论数据。
3. 解压本工具，用终端进入此目录，执行：

```bash
python3 bridge.py ingest --file ~/Downloads/comments.json --store ./review-store
python3 bridge.py list --store ./review-store
python3 bridge.py export --store ./review-store --format md --out ./comments.md
python3 bridge.py export --store ./review-store --format csv --out ./comments.csv
python3 bridge.py export --store ./review-store --format json --out ./comments-for-ai.json
```

也支持上游提供的 `comments.jsonl`，以及直接传入包含 `comments.json` 的目录。Mac/Windows/Linux 均可运行，只需 Python 3.10+，不必安装本工具依赖。

## 2. 命令行直接拉取在线项目

需要通过 pip 安装已有上游开源解析器（[Mangluu/overleaf-comments-export](https://github.com/Mangluu/overleaf-comments-export)，MIT）。

```bash
python3 -m pip install --upgrade "overleaf-comments-export[all]"
python3 bridge.py doctor

# 可先运行上游 GUI 选择已登录的浏览器，核验认证：
overleaf-comments-export --gui

# 直接拉取所有批注和所选原文、归档变更：
python3 bridge.py pull \
  --url 'https://www.overleaf.com/project/YOUR_PROJECT_ID' \
  --store ./review-store
```

`pull` 调用上游的项目读取实现；底层批注线程和文本定位依赖 Overleaf 的非公开内部接口，有可能随 Overleaf 升级失效。Mac 的 GUI 登录/浏览器会话可按上游 README 操作；**不要把 Cookie 提交到仓库、发送给 AI 或通过命令行参数公开分享**。此工具本身不读 Cookie，仅转交项目 URL 给已安装的上游命令。

## 3. 归档数据结构

```text
review-store/
  <project_id>/
    latest.json                # 最近一次完整原始导出（AI 可直接读取）
    last_import.json           # 上次更新检测结果
    snapshots/
      <content-sha>.json       # 按内容保存快照；无变化不重复生成
    events_<time>_<sha>.json   # 有变化时保存事件与摘要
    upstream-export/           # pull 模式：上游原始导出和 LaTeX 来源（可能包含未公开论文）
```

将 `latest.json` 作为**唯一事实来源**。线程稳定标识为 `thread_id`；`short_id` (C001 等) 只是显示顺序，新增较早批注时会变化。`bridge.py export` 输出简化的扁平表供 AI 和进一步的 Skill 读取。存储应保持私有，不建议直接推送到公开 GitHub。

**锚点质量**定义：

- `matched`: 有选中文本且上游未标为过期
- `stale`: 上游认为原文位置已变化或无法准确匹配，须人工复核
- `point-only`: 原生批注仅附着在光标点而非选中语句，不应猜测原文
- `unlocated`: 有评论线程但没有可定位锚点，仍保留内容

**增量识别：** `new_thread`、`new_message`、`edited_message`、`resolved`、`reopened`、`anchor_changed`、`missing_thread`。后者并不等同于真正删除，可能仅因导出筛选而缺失。

```bash
python3 bridge.py diff --store ./review-store --project YOUR_PROJECT_ID
```

## 4. 离线测试（无需登录）

```bash
python3 -m unittest discover -s tests -v
python3 bridge.py ingest --file examples/example_comments.json --store ./demo-store
python3 bridge.py export --store ./demo-store --format md --out ./demo-comments.md
```

样例数据是**完全虚构的**，测试有标注文字、位置批注、已解决批注、回复和丢失锚点；不包含任何真实账号/论文信息。

## 5. 当前能力与下一阶段

| 能力 | MVP 状态 |
|---|---|
| 在线读取 Overleaf 线程/原文 | 通过上游扩展或 CLI 完成；需用户真实账号验收 |
| 批注内容与原文关联 | 支持，保留上游 anchor 与 stale 状态 |
| 跨文件、多审阅人、回复、已解决 | 支持 |
| JSON/CSV/Markdown | 支持 |
| 本地历史快照、增量事件 | 支持 |
| 断网条件下导入和对比 | 已测试 |
| 自动新增/回复/解决 Overleaf 批注 | **未实现** |
| AI 自动审稿 Skill/MCP | **未实现，后续开发** |

下一阶段建议：1) 用自己的 Overleaf 测试全量评论数量和锚点匹配率；2) 创建 `review_comments` / `get_anchor_context` 两个只读 MCP 工具；3) 开发 **先生成建议、人工确认后再写入 Overleaf** 的自动批注流程，避免误写和误定位。

## 来源与许可

这是独立编写的**归档/增量适配层**，没有复制上游代码，功能依赖其导出 JSON 格式。在线读取组件由 [Mangluu/overleaf-comments-export](https://github.com/Mangluu/overleaf-comments-export) (MIT) 提供；源格式参考其 `schema_version: 1.3`。第三方组件均由用户自行安装并遵守相应许可与 Overleaf 平台规则。本工具自身可按需要整合到私有项目。

### 可选：校验 LaTeX 原文定位

直接 `pull` 时会保存 source 文件，可以进一步验证批注确实附着在该语句而非只凭相似文本猜测：

```bash
python3 bridge.py verify --store ./review-store --project YOUR_PROJECT_ID
```

结果区分 `exact`（指定偏移完全吻合）、`nearby`（相邻文字匹配）、`moved-far`（远处出现同一句，需复核）、`not-found`、`source-missing` 和 `point-only`。如果通过浏览器扩展获取 JSON 且没有保存 `.tex` 文件，可用 `--source /path/to/source/` 明确指定源码目录。