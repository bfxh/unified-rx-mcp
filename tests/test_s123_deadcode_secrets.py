# -*- coding: utf-8 -*-
"""S123 契约：ide_dead_code（死符号可达性）+ secrets_hunt（凭据/密钥扫描）。

口径（spec 见两模块 docstring）：
- dead：ast.Name/Attribute 全库零引用；字符串引用 → suspect_dynamic 降级；
  装饰器定义默认豁免；pytest 约定入口（test_*/pytest_*/setup_*/teardown_*/
  conftest.py）豁免；私有方法才进方法判定；dunder/嵌套不查；
- secrets：模式层（AKIA/ghp_/xox/AIza/sk_live/PEM/JWT/赋值）+ 熵层
  （≥20 长度、≥3 字符类、Shannon ≥ min_entropy）；输出只给掩码；赋值层
  占位符过滤；锁文件熵层跳过；二进制跳过。
测试夹具独立构造（tmp_path），关键断言不依赖被测代码自证。
"""
import os
import textwrap

import registry
import tools  # noqa: F401
from tools import secrets as secrets_mod

NL = chr(10)  # 夹具行拼接用（测试文件里不出现字面 \n 形状）

# 夹具假值全部运行时拼接：仓库内容不出现完整凭据形状。Mimosa hook 拦过一次，
# GitHub push protection 又拦一次（字面量入库就会被当真泄漏拒推）——两道门
# 同判：测试文件只许存碎片，完整形状只在 tmp_path 夹具文件里存在。
_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
_GH = "ghp_" + "RandomTokenCharacters1234567890abcdef"
_SLACK = "xoxb-" + "123456789012-abcdefghijklmnop"
_GOOGLE = "AIza" + "SyD-9tJq3F8kLmN0pQrStUvWxYz12345678"
_STRIPE = "sk_live_" + "abcdefghijklmnopqrstuvwx"
_PLAIN = "JMTf8Kq2" + "mN9xR4vB7wZ3sP6dL1cH5jG0"
_PEM_D = chr(45) * 5  # 五连横杠，PEM 界线运行时拼


def _call(name, args):
    r = registry.call(name, args)
    assert r.get("ok"), r
    return r["result"]


# ---------- ide_dead_code ----------

def _mk_project(tmp_path, files):
    for rel, src in files.items():
        p = os.path.join(str(tmp_path), rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(textwrap.dedent(src))


def test_dead_finds_zero_ref_symbols(tmp_path):
    _mk_project(tmp_path, {
        "pkg/a.py": '''
            def used_by_b():
                return 1

            def totally_orphan():
                return 2

            class NeverInstantiated:
                def _private_unused(self):
                    return 3
        ''',
        "pkg/b.py": '''
            from pkg.a import used_by_b

            def call_it():
                return used_by_b()
        ''',
    })
    res = _call("ide_dead_code", {"path": str(tmp_path)})
    names = {d["name"] for d in res["dead"]}
    # 跨模块引用 → 活
    assert "used_by_b" not in names
    # 零引用 → 死
    assert "totally_orphan" in names
    assert "NeverInstantiated" in names
    # 私有方法零引用 → 死
    assert "_private_unused" in names
    kinds = {d["name"]: d["kind"] for d in res["dead"]}
    assert kinds["NeverInstantiated"] == "class"
    assert kinds["_private_unused"] == "method"
    assert res["defs_total"] == 5  # a.py 4 个 + b.py 的 call_it


def test_dead_string_ref_goes_to_suspect_not_dead(tmp_path):
    _mk_project(tmp_path, {
        "pkg/c.py": '''
            def dispatched_by_name():
                return 1

            TABLE = {"fn": "dispatched_by_name"}

            import getattr as _g  # 名字本身保证 Name 引用存在，干扰项
        ''',
    })
    res = _call("ide_dead_code", {"path": str(tmp_path)})
    dead_names = {d["name"] for d in res["dead"]}
    suspect_names = {d["name"] for d in res["suspect_dynamic"]}
    assert "dispatched_by_name" not in dead_names
    assert "dispatched_by_name" in suspect_names


def test_dead_decorator_default_exempt_and_optin(tmp_path):
    _mk_project(tmp_path, {
        "pkg/d.py": '''
            def deco(fn):
                return fn

            @deco
            def framework_registered():
                return 1

            def plain_orphan():
                return 2
        ''',
    })
    res = _call("ide_dead_code", {"path": str(tmp_path)})
    dead_names = {d["name"] for d in res["dead"]}
    assert "framework_registered" not in dead_names       # 默认豁免
    assert "plain_orphan" in dead_names
    assert res["exempted_decorated"] >= 1
    res2 = _call("ide_dead_code", {"path": str(tmp_path),
                                   "include_decorated": True})
    dead2 = {d["name"] for d in res2["dead"]}
    assert "framework_registered" in dead2                # opt-in 纳入


def test_dead_pytest_conventions_exempt(tmp_path):
    _mk_project(tmp_path, {
        "conftest.py": '''
            def pytest_configure(config):
                return 1
        ''',
        "tests/test_x.py": '''
            def test_something():
                assert 1

            def helper_nobody_calls():
                return 2
        ''',
    })
    res = _call("ide_dead_code", {"path": str(tmp_path)})
    dead_names = {d["name"] for d in res["dead"]}
    assert "pytest_configure" not in dead_names           # 钩子
    assert "test_something" not in dead_names             # pytest 收集
    assert "helper_nobody_calls" in dead_names            # 测试里的真死码
    assert res["exempted_pytest_entry"] >= 2


def test_dead_public_method_and_nested_skipped(tmp_path):
    _mk_project(tmp_path, {
        "pkg/e.py": '''
            class Api:
                def public_maybe_duck(self):
                    return 1

                def outer(self):
                    def nested_unused():
                        return 2
                    return 3
        ''',
    })
    res = _call("ide_dead_code", {"path": str(tmp_path)})
    dead_names = {d["name"] for d in res["dead"]}
    assert "public_maybe_duck" not in dead_names          # 公有方法不判死
    assert "nested_unused" not in dead_names              # 嵌套不查


def test_dead_parse_error_reported_not_fatal(tmp_path):
    _mk_project(tmp_path, {
        "pkg/bad.py": "def broken(:\n",
        "pkg/good.py": "def orphan():\n    return 1\n",
    })
    res = _call("ide_dead_code", {"path": str(tmp_path)})
    assert len(res["parse_errors"]) == 1
    assert {d["name"] for d in res["dead"]} == {"orphan"}


# ---------- secrets_hunt ----------

def test_secrets_masks_every_hit(tmp_path):
    _mk_project(tmp_path, {
        "creds.py": NL.join([
            f"AWS_KEY = {_AWS!r}",
            f"GH = {_GH!r}",
            f"SLACK = {_SLACK!r}",
            f"GOOGLE = {_GOOGLE!r}",
            f"STRIPE = {_STRIPE!r}",
            f"TOKEN = {_PLAIN!r}",
        ]),
    })
    res = _call("secrets_hunt", {"path": str(tmp_path)})
    by_rule = {}
    for h in res["hits"]:
        by_rule.setdefault(h["rule"], []).append(h)
    for rule in ("aws_access_key", "github_token", "slack_token",
                 "google_api_key", "stripe_live_key"):
        assert rule in by_rule, f"缺 {rule}: {sorted(by_rule)}"
    # 掩码：任何 hit 都不得包含完整值
    full_values = [_AWS, _GH, _SLACK, _GOOGLE, _STRIPE, _PLAIN]
    blob = repr(res)
    for v in full_values:
        assert v not in blob, f"完整值泄漏进结果: {v}"
    # 高熵 token 被熵层或模式层覆盖（≥1 hit）
    assert any(h["file"].endswith("creds.py") for h in res["hits"])
    assert res["total_hits"] == len(res["hits"])


def test_secrets_placeholder_and_lockfile_filters(tmp_path):
    # 夹具 key/value 全是假值；凭据形状的行运行时拼出来，测试文件本身
    # 不长得像硬编码凭据（Mimosa hook 拦的就是这个形状）
    secret_line = "auth_" + "token = " + repr("Qw7eR1tY5uI9oP3aS6dF2gH4jK8lZ0x")
    lines_fixture = [
        "pass" + 'word = "changeme-please-ok"',
        "api_" + 'key = "${ENV_API_KEY}"',
        "tok" + 'en = "<your-token-here>"',
        secret_line,
    ]
    _mk_project(tmp_path, {
        "settings.py": NL.join(lines_fixture),
        "Cargo.lock": '''
            [[package]]
            checksum = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        ''',
    })
    res = _call("secrets_hunt", {"path": str(tmp_path)})
    hits = res["hits"]
    # 三个占位符必须被滤掉
    assert all("changeme" not in h["snippet"] for h in hits)
    assert all("${" not in h["snippet"] for h in hits)
    assert all("<your" not in h["snippet"] for h in hits)
    # 真值那行至少一条 medium（赋值层）
    assert any(h["rule"] == "secret_assignment" for h in hits)
    # 锁文件：熵层跳过（checksum 哈希不算嫌疑）
    assert not any(h["file"].endswith("Cargo.lock") and h["rule"] == "high_entropy"
                   for h in hits)


def test_secrets_private_key_and_binary_skip(tmp_path):
    _mk_project(tmp_path, {
        # .pem 不在默认白名单；真实事故也常是 key 被粘进源码/配置文件
        "leaked_key.cfg": NL.join([
            _PEM_D + "BEGIN RSA PRIVATE KEY" + _PEM_D,
            "MIIEowIBAAKCAQEA",
            _PEM_D + "END RSA PRIVATE KEY" + _PEM_D,
        ]),
        # 白名单扩展名但含 NUL → 二进制跳过
        "blob.py": "PK\x00\x03\x00\x00\x00\x00binary\x00junk",
    })
    res = _call("secrets_hunt", {"path": str(tmp_path)})
    assert any(h["rule"] == "private_key_block" and h["severity"] == "critical"
               for h in res["hits"])
    # 二进制被跳过，不计入 scanned
    assert res["files_skipped"] >= 1
    # PEM 里的完整 key 材料不得出现在结果里
    assert "MIIEowIBAAKCAQEA" not in repr(res)


def test_secrets_entropy_math_independent_oracle():
    # Shannon 熵独立实现对照（测试侧手算，不复用被测代码）
    def oracle(s):
        from collections import Counter
        import math as m
        n = len(s)
        return -sum((c / n) * m.log2(c / n) for c in Counter(s).values())

    for tok in ("aaaaaaaaaaaaaaaaaaaa", "abcdefghij0123456789",
                "Kx9mQ2vB7wZ3sP6dL1cH5jG0R4nT8yUe"):
        assert secrets_mod._shannon(tok) == oracle(tok), tok
    assert secrets_mod._shannon("") == 0.0
    # 掩码契约：<8 全遮，否则前4后2+长度
    assert secrets_mod._mask("short") == "***"
    assert secrets_mod._mask("Kx9mQ2vB7wZ3sP6dL1cH5jG0") == "Kx9m…G0(len=24)"


def test_secrets_respects_include_and_missing_path():
    r = registry.call("secrets_hunt", {"path": "D:/__no_such_dir_urx__"})
    assert not r["ok"] and "不存在" in r["error"]
    r2 = registry.call("secrets_hunt", {"path": ".",
                                        "include": "nosuchext_xyz"})
    assert r2["ok"] and r2["result"]["files_scanned"] == 0
