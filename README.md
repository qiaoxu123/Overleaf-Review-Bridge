# Overleaf Review Bridge — 只读批注同步与归档 (v0.2)

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

将 `latest.json` 作为**原生导出数据的事实来源**；它只包含上游实际导出的字段，不保证上游能获取完整修改建议。通知索引单独保存，不能用于推断原生数据是否完整。线程稳定标识为 `thread_id`；`short_id` (C001 等) 只是显示顺序，新增较早批注时会变化。`bridge.py export` 输出简化的扁平表供 AI 和进一步的 Skill 读取。存储应保持私有，不建议直接推送到公开 GitHub。

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

## 6. 通知邮件解析与增量索引（v0.2）

邮件是发现修改的线索，**不是完整原生批注或修改记录**。本版本离线导入已保存的通知，不连接 Gmail、不要求邮箱 OAuth，也不自动打开修改链接。

支持 Gmail API `format=full` JSON（含 base64url MIME body）、Gmail 连接器的 `structuredContent` JSON、RFC 822 `.eml`，以及以下规范化 JSON。批量输入可以是 JSON 数组或 `{"messages": [...]} `；每项必须包含完整正文，搜索结果的 snippet 不可替代正文。

```json
{"message_id":"<unique-id@overleaf.com>", "subject":"New activity in ...",
 "date":"Sat, 17 Oct 2026 10:55:22 +0000", "html":"<html>...</html>", "text":"..."}
```

```bash
python3 bridge.py ingest-mail --file ~/Downloads/notification.eml --store ./review-store
python3 bridge.py ingest-mail --file ~/Downloads/gmail-full.json --store ./review-store
# 直接读取累积索引（通知专用，不读取 latest.json）
python3 -m json.tool review-store/notifications/index.json
```

解析字段包括项目 ID、doc ID、文件路径、章节、作者、邮件来源、修改链接、change ID，以及 HTML 中的行号和插入/删除片段。HTML 优先，因为真实通知的纯文本版本可能没有行号；纯文本无法定位时行号留空。片段中的上下文、插入和删除仅是通知展示内容，不自动声称为完整原文或最终版本。未支持的模板会保留计数不匹配警告；无项目链接或多个项目混杂时拒绝导入。链接只做解析，不访问。

真实格式核对：2026-10-17 18:55 北京时间的邢亚欣论文通知，报告 **12 条新增、3 条展开、9 条省略**，另有一条没有 change ID 的拒绝活动。解析后有 3 个可索引建议；9 条保存为 `unexpanded_new=9`，不生成虚构 ID。没有 ID 的拒绝活动进入 `unlinked_activities`，不通过相似文本猜测其所属建议。公开测试样例保留该邮件的 HTML 结构、行号和 diff 标签，替换项目/文档/change ID、作者、章节和论文片段，去除地址及追踪图像。

```text
review-store/
  <project_id>/latest.json              # 原生 comments / threads / tracked_changes
  <project_id>/snapshots/...            # 原生导出的历史快照
  notifications/index.json              # 通知累积索引
  notifications/batches/<sha256>.json   # 不可变批次证据及增量事件
```

去重与边界：

- 建议身份为 `(project_id, doc_id, change_id)`；同一建议出现在多封邮件中，只新增来源观察。
- 邮件优先使用 RFC Message-ID，其次 Gmail ID；无 ID 时按解析内容散列。相同邮件/内容重复导入不产生事件；同一邮件正文得到补充时保留新证据版本。
- 增量事件为 `notification_observed` 和 `suggestion_discovered`。乱序导入只累积证据，不覆盖原生状态；邮件中的拒绝/接受活动保留为观察，`native_status` 仍为 unknown。
- `reported_new`、`visible_new`、`explicitly_omitted` 和 `unexpanded_new` 是每封通知的计数，**不能将不同邮件的省略数相加作为项目待处理总数**。没有明确计数时用 null；即便全部摘要展开，`native_data_complete=false`。
- 缺席不等于删除、接受或解决。通知不会触发 `missing_thread` 或 `missing_tracked_change`，不会更新原生 `latest.json`。
- 当前为单进程写入，索引通过临时文件原子替换；不要并行向同一个 store 导入。邮件及本地索引可能含未公开论文和链接，应保存在私有目录。

### 原生评论与修改建议分别导出

旧版本已在快照中保留 `tracked_changes`，但只比较/导出评论。v0.2 增加原生建议数量、独立导出与 `new_tracked_change` / `updated_tracked_change` / `missing_tracked_change` 事件。缺席事件只表示本次导出未出现，不代表已接受或删除。无稳定 ID 的建议以内容散列标识，内容修改表现为新项和缺席项；不会猜测关联。

```bash
# 默认仍导出原生评论与回复
python3 bridge.py export --store ./review-store --project PROJECT_ID --kind comments --format json --out ./comments.json
# 单独导出上游实际提供的原生修改建议；保留未知字段
python3 bridge.py export --store ./review-store --project PROJECT_ID --kind suggestions --format json --out ./native-suggestions.json
```

`--kind suggestions` 同样支持 CSV / Markdown。JSON 保留完整原始 `tracked_changes`；CSV 的 `raw_change_json` 保留单条原始数据。评论导出保持兼容。若上游导出没有 `tracked_changes`，仅能说明当前数据源未提供，仍需要登录 Overleaf 或可提供完整修改记录的上游导出；通知不能补足省略内容。

### 回归验证

```bash
python3 -m unittest discover -s tests -v
```

覆盖真实 HTML 结构的 12/3/9 计数、无 ID 拒绝活动、diff 片段、行号、跨邮件去重、重复导入、跨项目/文档 ID 隔离、乱序导入、EML、两类 Gmail MIME 格式、纯文本、截断正文，以及通知不更改原生归档和原生建议单独导出。
