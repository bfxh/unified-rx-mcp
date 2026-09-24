"""tools/filewalk.py —— 代码文件遍历统一实现（S127，CONSOLIDATION C1 除重）。

此前三份模块级文件遍历器（`scan._iter_files` / `ide_common._iter_files` /
`ide_deadcode._walk_py`）各写一遍 os.walk + skip + 计数逻辑；本模块收敛为
**唯一实现**，各域用参数化 profile（match 判定 + skip 集合 + 单文件支持）
保持既有语义逐字不变：

- 扫描域（scan / code_review）：`LANG_BY_EXT` 25 扩展名 + `SCAN_WALK_SKIP`；
- ide 域（ide_common._iter_files）：沿用其 `_lang_of` 表（含 gd/cs/dart）+ 自身 skip；
- ide_deadcode._walk_py：.py 单扩展 + 自身 skip（venv 等）。

如实边界：
- cache.py / filescan.py / neardupes.py 的**内联** os.walk 与各自 `_SKIP_DIRS`
  副本不在本轮（语义需逐一对齐，列 CONSOLIDATION C1b 候选）；
- `fs_list.walk` / `lsp.walk` 是树递归闭包（目录树/JSON 树），非同族不并。
"""
import os

# 扫描域代码文件定义（原 scan._LANG_BY_EXT，逐字保留——扩展这张表等于
# 改变扫描域 max_files 的计额口径，需连带测试同步）
LANG_BY_EXT = {
    ".py": "python", ".rs": "rust", ".go": "go", ".ts": "typescript",
    ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".gd": "gdscript", ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".cs": "csharp", ".dart": "dart", ".lua": "lua", ".sh": "bash",
    ".java": "java", ".kt": "kotlin", ".php": "php", ".rb": "ruby",
    ".swift": "swift",
}

# 扫描域【遍历】跳过集（原 scan._iter_files 函数内元组，逐字保留）
SCAN_WALK_SKIP = ('.git', 'node_modules', 'target', '__pycache__', 'dist', 'build',
                  '.codegraph', 'backups', 'assets', 'screenshots', 'images', 'fonts')

# 扫描域【模块级】跳过集（原 scan._SKIP_DIRS，_untested_findings 用，逐字保留）
SCAN_SKIP_DIRS = ('.git', 'node_modules', 'target', '__pycache__', 'dist', 'build',
                  '.unified-rx-index', 'backups')

# 工具链遍历跳过集（S129/C1b：filescan 与 neardupes 曾各持一份同款集合，此处唯一）
TOOLCHAIN_SKIP_DIRS = ('.git', 'node_modules', 'target', '__pycache__', 'dist',
                       'build', '.venv', 'venv', '.pytest_cache')


def is_code_file(fp):
    """有语言映射的代码文件（原 scan._iter_files 判定）。"""
    return bool(LANG_BY_EXT.get(os.path.splitext(fp)[1].lower(), ""))


def iter_files(path, max_files, match, skip_dirs, file_ok=False, sort_files=False):
    """统一遍历（全仓唯一 os.walk 文件遍历实现）。

    语义与各旧实现逐字等价：
    - `file_ok=True` 且路径是文件 → **无条件产出**（不改判——原 scan._iter_files
      单文件行为如此，保持兼容）；目录不存在 → 空；
    - 目录按 os.walk 序（不排序）产出 `match(fp)` 为真的文件；
      `sort_files=True` 时每层文件名排序（filescan/neardupes 原语义）；
    - `count` 只计**产出项**，达 max_files 即停；非匹配文件不占额。
    """
    if file_ok and os.path.isfile(path):
        yield path
        return
    if not os.path.isdir(path):
        return
    skip = set(skip_dirs)
    count = 0
    for r, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip]
        if sort_files:
            files = sorted(files)
        for fn in files:
            fp = os.path.join(r, fn)
            if not match(fp):
                continue
            if count >= max_files:
                return
            yield fp
            count += 1


def iter_code_files(path, max_files, skip_dirs=SCAN_WALK_SKIP, file_ok=False):
    """扫描域惯用 profile（原 scan._iter_files 的公开行为）。"""
    return iter_files(path, max_files, is_code_file, skip_dirs, file_ok)
