# Jianer_Plugin_Market_Next

SR思锐 **智慧市场** 的插件仓库。Jianer_QQ_bot 的所有插件代码都放在这里，通过 PR 提交，合并后由 GitHub Actions 自动生成索引、打包发布。

这是纯静态方案，没有服务端：索引是仓库里的 [`index.json`](index.json)，插件包挂在 GitHub Release 上，`jianer-cli` 直接拉取。

## 工作原理

```
作者提 PR ──► pr-check.yml 校验 ──► 合并到 main ──► build-market-index.yml
                                                       ├─ 生成 index.json
                                                       ├─ 打包 zip 发 GitHub Release
                                                       └─ 提交 index.json 回仓库
```

1. 插件通过 PR 加入 `plugins/` 目录，每个插件必须带 `market.json` 声明版本号。
2. PR 阶段自动校验：`market.json` 合法性、`__plugin_meta__` 可解析、ID/SemVer 格式、ID 全仓库唯一，并检查是否忘记提升版本号。
3. 合并到 `main` 后，Actions 重新生成 `index.json`，为每个新版本创建 GitHub Release（tag 为 `<pluginId>@<version>`，asset 是 zip），并把索引提交回仓库。
4. `jianer-cli` 拉取索引 → 按 `releaseUrlTemplate` 下载 zip → 校验 `release.sha256` → 解压进 bot 的 `plugins/` 目录。

## 添加插件

### 目录结构

支持两种形态，与 bot 的 `plugins/` 目录一致：

```
plugins/
├── AdvancedQuote/           # 包插件：含 setup.py 的目录
│   ├── setup.py             # 入口，模块级 __plugin_meta__
│   ├── AdvancedQuote.py
│   ├── market.json          # 市场元数据（必须有 version）
│   └── README.md            # 可选，会出现在 plugin show --r
├── LikePlugin.py            # 单文件插件
└── LikePlugin.market.json   # 单文件插件的市场元数据（同级）
```

### market.json

唯一必填字段是 `version`，其余可选：

```json
{
  "version": "1.2.0",
  "id": "jianerbot-plugin-advanced-quote",
  "name": "名人名言",
  "description": "将引用的消息渲染成图片名言",
  "authors": ["someone"],
  "license": "MIT",
  "homepage": "https://github.com/...",
  "tags": ["image", "quote"],
  "pipDependencies": ["pillow>=10.0"],
  "pythonRequires": ">=3.12",
  "coreRequires": ">=0.3,<1.0",
  "deprecated": false
}
```

- `id` 缺省时取 `__plugin_meta__.name`，必须是小写短横线风格。
- `version` 必须是合法 SemVer。**改了插件代码就必须提升版本号**，否则 PR 校验会失败（该版本已发布过）。
- `pipDependencies` 是 PEP 508 格式，安装插件后由 jianer-cli 执行 pip 安装。

### 插件元数据

`__plugin_meta__` 由脚本用 AST 静态解析，**不会 import 插件**，所以 CI 无需安装 bot 依赖：

```python
__plugin_meta__ = PluginMetadata(
    name="jianerbot-plugin-advanced-quote",
    description="Render a quoted message as an image quote.",
    usage="{reminder}名人名言【引用一条消息】",
    requires={"jianerbot-plugin-alconna"},
)
```

注意：`name`/`description`/`usage`/`requires` 的值必须是字面量（字符串/集合），不能是运行时计算的表达式，否则解析不到。

## 索引格式

[`index.json`](index.json) 的完整定义见 [`market-index.schema.json`](market-index.schema.json)。顶层结构：

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-09-26T10:08:08+00:00",
  "repository": {
    "url": "https://github.com/IntelliMarkets/Jianer_Plugin_Market_Next",
    "ref": "<生成索引时的 commit SHA>",
    "releaseUrlTemplate": "https://github.com/IntelliMarkets/Jianer_Plugin_Market_Next/releases/download/{tag}/{asset}"
  },
  "plugins": [
    {
      "id": "jianerbot-plugin-advanced-quote",
      "name": "名人名言",
      "version": "1.2.0",
      "description": "...",
      "usage": "...",
      "type": "package",
      "path": "plugins/AdvancedQuote",
      "entry": "plugins/AdvancedQuote/setup.py",
      "requires": ["jianerbot-plugin-alconna"],
      "release": {
        "tag": "jianerbot-plugin-advanced-quote@1.2.0",
        "asset": "jianerbot-plugin-advanced-quote-1.2.0.zip",
        "size": 12345,
        "sha256": "..."
      }
    }
  ]
}
```

关于**下载量**：索引里没有这个字段。纯静态方案下唯一的计数方式是 GitHub Release asset 的 `download_count`，jianer-cli 通过 `GET /repos/{owner}/{repo}/releases/tags/{tag}` 查询。

## 本地开发

脚本的两种模式：

```bash
# PR 校验模式（只读，不写文件）
python3 scripts/generate_index.py check .

# 生成索引 + 打包新版本 zip 到 dist/
python3 scripts/generate_index.py index .
```

`index` 模式是幂等的：已经发布过的 `id@version` 会复用旧索引里的 size/sha256，不重新打包、不重复发 Release。如果你在本地跑，`dist/` 已被 gitignore 忽略。

如果上次运行创建了 Release 但索引提交失败，脚本会通过 `gh api` 查询已存在的 Release，复用其真实 `size` 和 `digest` —— 不会重新打包（zip 哈希随文件系统时间戳变化，重打包会导致校验失败）。查询需要 `gh` 已登录或设置 `GH_TOKEN`。

## 仓库文件

| 文件 | 作用 |
|---|---|
| `plugins/` | 所有插件代码，通过 PR 添加 |
| `scripts/generate_index.py` | 生成索引、打包、校验 |
| `market-index.schema.json` | index.json 的 JSON Schema |
| `.github/workflows/pr-check.yml` | PR 校验 |
| `.github/workflows/build-market-index.yml` | 合并后发布 + 更新索引 |
| `index.json` | 自动生成的索引，请勿手动编辑 |

## 许可

[MIT](LICENSE)
