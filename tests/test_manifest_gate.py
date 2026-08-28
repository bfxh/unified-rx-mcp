# -*- coding: utf-8 -*-
"""S38 门禁：MCP 对外工具必须有 manifest 完整声明 + 域级 skill 文档覆盖。

用户要求："MCP 对外也是要求有 skill 才行"、"每一个格式每一个语言都要配置 skill"。
本门禁让"新工具没文档/新域没 skill"在 CI 直接红。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)

import registry  # noqa: E402
import tools  # noqa: E402,F401

SKILLS_DIR = os.path.join(ROOT, "skills")


def test_every_tool_has_complete_manifest():
    for name, entry in registry._TOOLS.items():
        assert entry["description"] and len(entry["description"]) >= 10, \
            f"{name}: description 缺失或过短"
        schema = entry["schema"]
        assert schema.get("type") == "object", f"{name}: schema 必须是 object"
        assert isinstance(schema.get("properties"), dict), f"{name}: 缺 properties"
        assert entry["group"], f"{name}: 缺 group"
        # required 里的字段必须都在 properties 里声明（防门禁绕过）
        for req in schema.get("required") or []:
            assert req in schema["properties"], f"{name}: required[{req}] 未声明"


def test_every_tool_schema_required_fields_typed():
    for name, entry in registry._TOOLS.items():
        props = entry["schema"].get("properties") or {}
        for k, spec in props.items():
            assert isinstance(spec, dict) and spec.get("type"), \
                f"{name}.{k}: property 缺 type"


def test_every_domain_has_skill_doc():
    """MCP 对外 = 必须有 skill 文档：skills/<域>.md 必须存在且非空。"""
    missing = []
    for group, names in registry.groups().items():
        doc = os.path.join(SKILLS_DIR, f"{group}.md")
        if not (os.path.isfile(doc) and os.path.getsize(doc) > 50):
            missing.append(f"{group} ({len(names)} 工具) -> skills/{group}.md")
    assert not missing, "缺域级 skill 文档: " + "; ".join(missing)


def test_language_skills_exist():
    """每一个语言都要有 skill：ide 域声明支持的语言必须有对应指南。"""
    src = open(os.path.join(ROOT, "tools", "ide.py"), encoding="utf-8").read()
    declared = [lang for lang in ("python", "rust", "java", "go")
                if f'"{lang}"' in src or f"'{lang}'" in src]
    for lang in ("python", "rust", "java", "go", "c", "cpp"):
        doc = os.path.join(SKILLS_DIR, "lang", f"{lang}.md")
        assert os.path.isfile(doc) and os.path.getsize(doc) > 100, \
            f"缺语言 skill: skills/lang/{lang}.md"
