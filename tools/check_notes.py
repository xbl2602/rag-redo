"""check_notes.py — 笔记命名规范只读扫描。

移植自 obsidian-rag/tools/check_notes.py（设计：docs/specs/
2026-08-14-note-naming-convention-design.md），规则与豁免集逐条保持一致；
三个集成点适配 rag-redo 架构：库注册表走 official-library-manager 插件、
文件枚举走 resolve_included_files（同一份排除/勾选门禁）、frontmatter
解析走 core/text_cleaning.extract_frontmatter。

用法（需在仓库根目录、RAG_REDO_DATA_ROOT 指向数据目录时运行）：
  python tools/check_notes.py                 # 扫描全部注册库
  python tools/check_notes.py <库id>           # 只扫某个库
  python tools/check_notes.py <库id> --json    # 输出机器可读 JSON

行为：
  - 只读，绝不修改任何文件。
  - 复用库配置的排除名单与勾选门禁（与索引同一份文件枚举）。
  - 硬规则 R1-R4 报不合规；软规则（H1 与文件名不一致、缺 frontmatter title）单独分组。
  - 豁免集：结构文件（MOC-* / LOG / 目录 / Home / AGENTS）、templates 目录、
    代码/数值命名（_ + 大写缩写/数字特征）、纯日期命名、00-Inbox 未处理区。
  - 报告含每条的 created/updated、建议新名（从文件内已有中文上下文提取）、入链清单。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.text_cleaning import extract_frontmatter  # noqa: E402

_TEXT_EXTS = {".md", ".txt", ".markdown"}

CJK = re.compile(r"[\u4e00-\u9fff]")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)
PURE_NUM_HEADING_RE = re.compile(r"^\d+(\.\d+)*$")
WIKILINK_RE = re.compile(r"!?\[\[([^\]]*)\]\]")
BAD_NAME_RE = re.compile(r"[_:]| {2,}")
CODE_NAME_RE = re.compile(r"^[A-Za-z]?\d+[._-][A-Za-z0-9_.-]|^.*?[A-Z]{2,}.*?_\d|^[A-Z0-9]+_[A-Z0-9_]+$|^(?=[^一-龥]*$)(?=.*\d)(?=.*_)[A-Za-z0-9_]+$")
DATE_NAME_RE = re.compile(r"^\d{4}[-_.]\d{2}[-_.]\d{2}")
HIGH_LINK_THRESHOLD = 3


def split_target(target):
    """wikilink 目标 → 文件名（去 |别名、#锚点、folder/ 路径、[] 数组）。"""
    t = target.strip()
    if not t or t.startswith("#"):
        return ""
    t = t.split("|")[0].strip()
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner or inner.startswith("#"):
            return ""
        t = inner.split("|")[0].strip()
        if t.startswith("#"):
            return ""
    t = t.split("#", 1)[0].strip()
    return t.rsplit("/", 1)[-1].strip() if t else ""


def is_exempt(name, rel):
    """豁免集：命中即不报。

    覆盖（2026-08-14 增强）：
    - 结构文件：AGENTS/LOG/目录/Home/README 及其变体、MOC-*、_ 前缀工作流
    - 目录型豁免：templates、00-Inbox、90-Archive（归档区命名规范支持加前缀变体）、
      TODO、任务节点、Clippings（剪藏）
    - 代码/数值命名、纯日期命名、graph-ignore-*（图谱忽略文件）、session/会话/临时
    """
    stem = Path(name).stem
    low = name.lower()
    if low in ("log.md", "目录.md", "home.md", "agents.md", "readme.md"):
        return True
    if low.startswith("_"):
        return True  # Obsidian 隐藏/工作流文件惯例（如 _workflow-*.md）
    if low.startswith("moc-"):
        return True
    if re.match(r"^(log|agents|readme)-.+", low) and "90-Archive" in rel:
        return True  # 归档区的加前缀结构文件变体（归档区 AGENTS.md 规范支持）
    if DATE_NAME_RE.match(stem):
        return True
    if CODE_NAME_RE.match(stem):
        return True
    if low.startswith("graph-ignore-"):
        return True
    parts = Path(rel).parts
    if any(p in ("templates", "TODO", "任务节点", "Clippings") for p in parts):
        return True
    if parts and parts[0] in ("00-Inbox", "90-Archive"):
        return True
    return False


def violations_for(file_name, body, front, meta):
    """对单个文件跑硬/软规则，返回 (硬违规列表, 软建议列表)。"""
    stem = Path(file_name).stem
    hard = []
    soft = []

    if not CJK.search(stem):
        hard.append({
            "rule": "R1",
            "msg": "文件名无中文主题词",
            "suggest": suggest_name(stem, front, body),
        })
    if BAD_NAME_RE.search(stem):
        hard.append({
            "rule": "R2",
            "msg": "文件名含 _ / : 或连续空格（应为自然短语）",
            "suggest": None,
        })

    headings = [m.group(2).strip() for m in HEADING_RE.finditer(body)]
    if not headings:
        hard.append({
            "rule": "R3",
            "msg": "正文无任何标题（整篇成块，无标题链锚点）",
            "suggest": None,
        })
    pure = [h for h in headings if PURE_NUM_HEADING_RE.match(h)]
    if pure:
        hard.append({
            "rule": "R4",
            "msg": f"存在纯编号标题 {len(pure)} 处，如「{pure[0]}」（应带主题上下文）",
            "suggest": None,
        })

    if headings:
        h1 = headings[0]
        h1_clean = re.sub(r"[\s\-_]+", "", h1).lower()
        stem_clean = re.sub(r"[\s\-_]+", "", stem).lower()
        if h1_clean and stem_clean and h1_clean != stem_clean and not CJK.search(h1_clean) == (not CJK.search(stem_clean)):
            soft.append({
                "rule": "H1",
                "msg": f"H1「{h1}」与文件名「{stem}」不一致",
                "suggest": None,
            })
    if not front.get("title"):
        soft.append({
            "rule": "FM",
            "msg": "frontmatter 无 title（短文件整篇成块时用它做标题）",
            "suggest": None,
        })

    meta_ctx = f"created {meta.get('created', '?')}, updated {meta.get('updated', '?')}"
    return hard, soft, meta_ctx


def _shorten(cand):
    """把候选压缩成文件名级短语：断句截断到 ~20 字，去首尾噪声。"""
    cand = re.split(r"[—–；;，,。：:]", cand)[0]
    cand = re.sub(r"^[【\s]*", "", cand)
    cand = re.sub(r"[\s]+", " ", cand).strip()
    if len(cand) > 24:
        cand = cand[:24].rstrip()
    return cand.strip() or None


def suggest_name(stem, front, body):
    """建议新名：从文件内已有中文上下文提取（脚本无 LLM，不做翻译）。"""
    cands = []
    for key in ("title", "tags", "aliases", "summary"):
        v = (front.get(key) or "").strip()
        for part in v.split(","):
            part = part.strip()
            if CJK.search(part):
                cands.append(part)
    if not cands:
        for h in HEADING_RE.finditer(body):
            if CJK.search(h.group(2)):
                cands.append(h.group(2).strip())
                break
    if not cands:
        return None
    head = _shorten(cands[0])
    if not head:
        return None
    if stem.lower() not in head.lower():
        return f"{head} {stem}".strip()
    return head.strip()


def build_backlinks(vault_root, files):
    """建全库 wikilink 反向索引：目标文件名（无扩展名）→ [来源相对路径...]。

    扫描整个 vault 的全部 md（含被索引排除的 目录.md/LOG.md/MOC 等）——
    改名会影响所有引用者，不只影响可索引文件。files 为当前库的可索引文件集
    （相对路径 → 绝对路径），用于排除"来源 == 目标文件自身"的判断。
    """
    back: dict[str, list[str]] = {}
    for p in Path(vault_root).rglob("*.md"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in WIKILINK_RE.finditer(text):
            tgt = split_target(m.group(1))
            if tgt:
                back.setdefault(tgt, []).append(rel)
    return back


def scan_library(lib_mgr, library_id):
    """扫描一个库，返回报告行。rag-redo 适配版：文件枚举走
    resolve_included_files（与索引同一份排除/勾选门禁）。"""
    cfg = lib_mgr.store.get(library_id)
    vault = Path(cfg.root_path)
    files = {
        path: vault / path
        for path, included, _reason in lib_mgr.resolve_included_files(library_id)
        if included and path.lower().endswith(".md")
    }
    back = build_backlinks(vault, files)

    hard_rows, soft_rows, mapping = [], [], []
    binary_rels = {
        rel for rel, p in files.items() if p.suffix.lower() not in _TEXT_EXTS
    }
    if binary_rels:
        print(f"[check_notes] 库 {cfg.name} 有 {len(binary_rels)} 个非 md 文件，"
              f"不参与命名规范分析", file=sys.stderr)
    for rel in sorted(files):
        if is_exempt(Path(rel).name, rel):
            continue
        p = files[rel]
        if rel in binary_rels:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        front, body = extract_frontmatter(text)
        meta = front
        hard, soft, meta_ctx = violations_for(Path(rel).name, body, front, meta)
        inlinks = sorted({s for s in back.get(Path(rel).stem, []) if s != rel})
        risk = "⚠️ 高引用" if len(inlinks) >= HIGH_LINK_THRESHOLD else ""
        for h in hard:
            hard_rows.append(f"[{h['rule']}] {rel}  ({meta_ctx}){risk}\n"
                             f"       {h['msg']}\n"
                             f"       {fmt_suggest(h['suggest'], inlinks)}")
        for s in soft:
            soft_rows.append(f"[{s['rule']}] {rel}  ({meta_ctx})\n       {s['msg']}")
        if hard:
            for h in hard:
                mapping.append({
                    "旧名": Path(rel).name,
                    "建议新名": h["suggest"] or "",
                    "入链数": len(inlinks),
                    "风险": risk,
                    "相对路径": rel,
                })
    return cfg.name, hard_rows, soft_rows, mapping


def fmt_suggest(suggest, inlinks):
    if suggest:
        line = f"建议新名: {suggest}"
    else:
        line = "建议新名: （需人工给出中文主题词）"
    if inlinks:
        links = ", ".join(inlinks[:5]) + ("…" if len(inlinks) > 5 else "")
        line += f"\n       入链: {len(inlinks)} 处 ({links})"
    else:
        line += "\n       入链: 0 处（改名无连锁影响）"
    return line


def main():
    ap = argparse.ArgumentParser(description="笔记命名规范只读扫描")
    ap.add_argument("library", nargs="?", default="",
                    help="库名（默认 Obsidian Vault 主库）")
    ap.add_argument("--all", action="store_true",
                    help="扫描全部注册库（默认只扫主库；agents/skills/test 非笔记库需显式指定）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    # rag-redo 适配：注册表走 official-library-manager 插件，启动方式同
    # core/cli.py 的业务命令（扫描并启用官方插件集）
    import argparse as _argparse

    from core.cli import _boot_pipeline  # noqa: PLW0127 - 复用同一启动器

    boot_args = _argparse.Namespace(
        plugins_dir=Path("plugins"),
        state_file=Path("data/plugins_state.json"),
        data_root=Path(__import__("os").environ.get("RAG_REDO_DATA_ROOT", "data")),
    )
    runtime, pipeline = _boot_pipeline(boot_args)
    lib_mgr = pipeline._singleton("library_manager")
    registry_ids = [c.library_id for c in lib_mgr.store.list_libraries()]
    if args.library:
        if args.library not in registry_ids:
            print(f"库不存在：{args.library}。可用：{', '.join(registry_ids)}", file=sys.stderr)
            runtime.close()
            sys.exit(1)
        entries = [args.library]
    elif args.all:
        entries = registry_ids
    else:
        # 默认只扫第一个库（旧项目默认"主库"；rag-redo 无主库概念，取注册顺序第一个）
        entries = registry_ids[:1]
        if not entries:
            print("没有已注册的库。先用 `python -m core.cli libraries add <路径>` 注册。", file=sys.stderr)
            runtime.close()
            sys.exit(1)

    all_data = []
    try:
        for library_id in entries:
            name, hard, soft, mapping = scan_library(lib_mgr, library_id)
            all_data.append({"library": name, "hard": hard, "soft": soft, "mapping": mapping})
    finally:
        runtime.close()

    if args.json:
        print(json.dumps(all_data, ensure_ascii=False, indent=2))
        return

    n_hard = sum(len(d["hard"]) for d in all_data)
    n_soft = sum(len(d["soft"]) for d in all_data)
    n_map = sum(len(d["mapping"]) for d in all_data)
    print(f"== 笔记命名规范扫描（只读，共 {len(all_data)} 库） ==")
    print(f"硬规则不合规 {n_hard} 项，软建议 {n_soft} 项，改名映射 {n_map} 条\n")
    for d in all_data:
        if d["hard"]:
            print(f"=== 不合规清单 — {d['library']} ===")
            print("\n".join(d["hard"]))
            print()
        if d["soft"]:
            print(f"=== 软规则建议 — {d['library']} ===")
            print("\n".join(d["soft"]))
            print()
    for d in all_data:
        if d["mapping"]:
            print(f"=== 改名映射表（需人工确认）— {d['library']} ===")
            print("旧名 | 建议新名 | 入链数 | 风险 | 相对路径")
            for m in d["mapping"]:
                print(f"{m['旧名']} | {m['建议新名'] or '（待定）'} | {m['入链数']} | {m['风险'] or '-'} | {m['相对路径']}")
            print()
    print("== 完成：脚本只读，未修改任何文件。改名需人工确认后手动执行。 ==")


if __name__ == "__main__":
    main()
