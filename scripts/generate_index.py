#!/usr/bin/env python3
"""智慧市场索引与发布工具。

用法:
    generate_index.py index   [repo_root]  # 生成/更新 index.json，打包新版本 zip 到 dist/
    generate_index.py check   [repo_root]  # PR 校验模式：只检查，不写文件

仓库约定（插件通过 PR 加入 plugins/）:
- 包插件:   plugins/Xxx/        入口 setup.py，必须带 market.json（含 version）
- 单文件:   plugins/Xxx.py      必须带同级 Xxx.market.json（含 version）

__plugin_meta__ 的 name/description/usage/requires 通过 AST 静态解析，
无需导入插件、无需安装 bot 依赖。

index 模式输出:
- index.json                          插件索引（含 release.tag/asset/sha256）
- dist/<id>-<version>.zip             仅为"索引里尚不存在该 id@version"的插件打包
- dist/releases.json                  需要新建的 Release 列表，供 workflow 的 gh 步骤消费
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
EXCLUDE_NAMES = {"__pycache__", ".git"}
EXCLUDE_SUFFIXES = {".pyc"}

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)
    print(f"::error::{msg}")


def parse_plugin_meta(entry: Path) -> dict:
    """从入口文件静态提取 __plugin_meta__ = PluginMetadata(...) 的关键字参数。"""
    meta: dict = {}
    try:
        tree = ast.parse(entry.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        err(f"{entry}: 语法错误，无法解析: {exc}")
        return meta
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__plugin_meta__" for t in node.targets):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        for kw in call.keywords:
            try:
                value = ast.literal_eval(kw.value)
            except ValueError:
                continue
            if kw.arg == "requires":
                value = sorted(value)
            meta[kw.arg] = value
        return meta
    err(f"{entry}: 缺少 __plugin_meta__ = PluginMetadata(...) 声明")
    return meta


def collect_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files = []
    for p in sorted(target.rglob("*")):
        if p.is_dir() or any(part in EXCLUDE_NAMES for part in p.parts):
            continue
        if p.suffix in EXCLUDE_SUFFIXES or p.name == "market.json":
            continue
        files.append(p)
    return files


def git_last_commit_iso(repo_root: Path, path: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%cI", "--", str(path.relative_to(repo_root))],
            cwd=repo_root, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return out or None
    except (subprocess.CalledProcessError, OSError):
        return None


def build_plugin(repo_root: Path, target: Path) -> dict | None:
    if target.is_dir():
        entry = target / "setup.py"
        if not entry.exists():
            err(f"{target}: 包插件缺少 setup.py")
            return None
        plugin_type = "package"
        market_path = target / "market.json"
        readme = target / "README.md"
    else:
        entry = target
        plugin_type = "single-file"
        market_path = target.with_name(target.stem + ".market.json")
        readme = None

    if not market_path.exists():
        err(f"{target}: 缺少 {market_path.name}（必须声明 version）")
        return None
    try:
        market = json.loads(market_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err(f"{market_path}: JSON 非法: {exc}")
        return None

    meta = parse_plugin_meta(entry)
    plugin_id = market.get("id") or meta.get("name", "")
    version = str(market.get("version", ""))

    if not ID_RE.match(plugin_id):
        err(f"{target}: 插件 ID '{plugin_id}' 不合法（须为全小写短横线风格，来自 market.json 的 id 或 __plugin_meta__.name）")
        return None
    if not SEMVER_RE.match(version):
        err(f"{market_path}: version '{version}' 不是合法 SemVer")
        return None

    record = {
        "id": plugin_id,
        "name": market.get("name") or target.stem,
        "version": version,
        "description": market.get("description") or meta.get("description", ""),
        "usage": meta.get("usage", ""),
        "type": plugin_type,
        "path": target.relative_to(repo_root).as_posix(),
        "entry": entry.relative_to(repo_root).as_posix(),
        "requires": meta.get("requires", []),
        "pipDependencies": market.get("pipDependencies", []),
        "release": {
            "tag": f"{plugin_id}@{version}",
            "asset": f"{plugin_id}-{version}.zip",
        },
        "_target": target,  # 内部使用，输出前删除
    }
    for key in ("authors", "license", "homepage", "tags", "pythonRequires", "coreRequires", "deprecated"):
        if key in market:
            record[key] = market[key]
    if readme is not None and readme.exists():
        record["readme"] = readme.relative_to(repo_root).as_posix()
    updated = git_last_commit_iso(repo_root, target)
    if updated:
        record["updatedAt"] = updated
    return record


def make_zip(repo_root: Path, target: Path, out_zip: Path) -> None:
    """打包插件。zip 根为插件目录名（或单文件名），解压到 plugins/ 即安装。"""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        base = target.parent
        for f in collect_files(target):
            zf.write(f, f.relative_to(base).as_posix())


def scan(repo_root: Path) -> list[dict]:
    plugins_dir = repo_root / "plugins"
    if not plugins_dir.is_dir():
        # 插件通过 PR 加入；仓库初始化时可能还没有 plugins/。
        # 空仓库不算错误，索引就是空列表。
        print(f"::notice::{plugins_dir} 不存在，视为空市场")
        return []
    records = []
    for child in sorted(plugins_dir.iterdir()):
        if child.name in EXCLUDE_NAMES or child.name.startswith("."):
            continue
        if child.is_file() and child.suffix != ".py":
            if not child.name.endswith(".market.json"):
                err(f"{child}: plugins/ 下出现无法识别的文件")
            continue
        record = build_plugin(repo_root, child)
        if record:
            records.append(record)

    ids = [r["id"] for r in records]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        err(f"插件 ID 重复: {dup}")

    known = set(ids)
    for r in records:
        for dep in r["requires"]:
            if dep not in known:
                print(f"::warning::{r['id']} 依赖 '{dep}' 不在本仓库（假定为 Bot 内置插件）")
    return records


def cmd_check(repo_root: Path) -> int:
    records = scan(repo_root)
    if errors:
        print(f"校验失败：{len(errors)} 个问题")
        return 1
    print(f"校验通过，共 {len(records)} 个插件")
    return 0


def gh_release_asset(repo: str, tag: str, asset: str) -> dict | None:
    """查询已存在的 Release asset，返回其真实 size/digest。

    用于重试：若上次运行创建了 Release 但索引提交失败，本次不能重新打包
    （zip 元数据随文件系统时间戳变化，哈希会不同），必须复用已发布 asset
    的实际大小与摘要。
    """
    try:
        out = subprocess.check_output(
            ["gh", "api", f"repos/{repo}/releases/tags/{tag}"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        return None
    try:
        for a in json.loads(out).get("assets", []):
            if a.get("name") != asset:
                continue
            digest = (a.get("digest") or "").removeprefix("sha256:")
            if len(digest) != 64:
                # 没有 digest 时无法安全复用，交由调用方重新打包
                return None
            return {"tag": tag, "asset": asset, "size": a["size"], "sha256": digest}
    except (json.JSONDecodeError, KeyError):
        return None
    return None


def cmd_index(repo_root: Path) -> int:
    records = scan(repo_root)
    if errors:
        return 1

    old_index_path = repo_root / "index.json"
    old_releases: dict[str, dict] = {}
    if old_index_path.exists():
        try:
            for p in json.loads(old_index_path.read_text(encoding="utf-8")).get("plugins", []):
                old_releases[p["release"]["tag"]] = p["release"]
        except (json.JSONDecodeError, KeyError):
            pass

    repo = os.environ.get("GITHUB_REPOSITORY", "IntelliMarkets/Jianer_Plugin_Market_Next")
    dist = repo_root / "dist"
    new_releases = []
    for r in records:
        tag = r["release"]["tag"]
        if tag in old_releases:
            # 该版本已发布过，复用旧索引里的 size/sha256，不重新打包
            r["release"] = old_releases[tag]
            r.pop("_target", None)
            continue
        # 索引里没有该 tag，但它可能已作为 Release 存在（上次索引提交失败）。
        published = gh_release_asset(repo, tag, r["release"]["asset"])
        if published is not None:
            r["release"] = published
            r.pop("_target", None)
            continue
        out_zip = dist / r["release"]["asset"]
        make_zip(repo_root, r.pop("_target"), out_zip)
        data = out_zip.read_bytes()
        r["release"]["size"] = len(data)
        r["release"]["sha256"] = hashlib.sha256(data).hexdigest()
        new_releases.append({
            "tag": tag,
            "asset": str(out_zip.relative_to(repo_root)),
            "title": f"{r['name']} {r['version']}",
            "notes": r["description"],
        })
        r.pop("_target", None)

    index = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository": {
            "url": f"https://github.com/{repo}",
            "ref": os.environ.get("GITHUB_SHA", "HEAD"),
            "releaseUrlTemplate": f"https://github.com/{repo}/releases/download/{{tag}}/{{asset}}",
        },
        "plugins": records,
    }
    old_index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dist.mkdir(exist_ok=True)
    (dist / "releases.json").write_text(
        json.dumps(new_releases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成 index.json（{len(records)} 个插件），待发布新版本 {len(new_releases)} 个")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "index"
    repo_root = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(".").resolve()
    if mode == "check":
        return cmd_check(repo_root)
    if mode == "index":
        return cmd_index(repo_root)
    print(f"未知模式: {mode}（可用: index / check）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
