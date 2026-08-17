#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rxide/diff.py — 编辑应用 + 行级 diff（difflib，纯 stdlib）。"""
import difflib


def apply_edit(file_text: str, edit_text: str, selection: dict | None = None,
               cursor_line: int | None = None) -> tuple[str, int, int]:
    """应用编辑：有选区替换选区行区间，否则在 cursor_line 后插入。

    返回 (新全文, 起始行, 结束行)——行号 1-based。
    """
    lines = (file_text or "").splitlines()
    edit = (edit_text or "").splitlines()
    trail = "\n" if (file_text or "").endswith("\n") else ""
    if selection and selection.get("start") and selection.get("end"):
        s, e = int(selection["start"]), int(selection["end"])
        s = max(1, min(s, len(lines) + 1))
        e = max(s, min(e, len(lines)))
        lines[s - 1:e] = edit
        start, end = s, s + len(edit) - 1
    else:
        cur = max(0, min(int(cursor_line or 0), len(lines)))
        lines[cur:cur] = edit
        start, end = cur + 1, cur + len(edit)
    return "\n".join(lines) + trail, start, end


def line_diff(old_text: str, new_text: str) -> dict:
    """行级 diff（SequenceMatcher）。

    返回 {"added": [新文本新增行号...], "removed": [{"after_line", "content"}],
          "stats": {"add", "del"}, "previews": [{"line", "before", "after"}]}。
    removed.after_line = 删除发生在新文本该行之后（0 = 文件最前）。
    previews.line 恒 ≥1（删首行场景钳位）；每块前后各最多 3 行。
    """
    old = (old_text or "").splitlines()
    new = (new_text or "").splitlines()
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added: list[int] = []
    removed: list[dict] = []
    previews: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        added.extend(range(j1 + 1, j2 + 1))
        for k in range(i1, i2):
            removed.append({"after_line": j1, "content": old[k]})
        line = j1 + 1 if j2 > j1 else j1
        previews.append({"line": max(1, line),  # 删首行时 j1=0 → 钳位到 1
                         "before": old[i1:i2][:3], "after": new[j1:j2][:3]})
    return {"added": added, "removed": removed,
            "stats": {"add": len(added), "del": len(removed)},
            "previews": previews}
