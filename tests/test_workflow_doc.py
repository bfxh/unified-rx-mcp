# -*- coding: utf-8 -*-
"""S50 workflow 文档存在性门禁。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
wf = os.path.join(ROOT, "skills", "workflow.md")


def test_workflow_doc_exists():
    assert os.path.isfile(wf) and os.path.getsize(wf) > 200


def test_workflow_doc_keywords():
    src = open(wf, encoding="utf-8").read()
    for kw in ("D:\\rj\\MCP", "UNIFIED_RX_SANDBOX", "pytest", "main"):
        assert kw in src, f"workflow.md 缺关键词: {kw}"
