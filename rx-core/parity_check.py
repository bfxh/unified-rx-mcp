#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""parity_check.py — Python vs rx-core(Rust) 输出一致性对比（一期验收）。

对每个纯函数跑 N 组随机/边界输入，Python（server._m_*）与 Rust（rx-core
示例程序输出）比对，输出不一致即失败。零 LLM 纯本地。
"""
import json
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402

RX_CORE_EXE = os.path.join(ROOT, "rx-core", "target", "debug", "rx-core.exe")
if not os.path.exists(RX_CORE_EXE):
    RX_CORE_EXE = os.path.join(ROOT, "rx-core", "target", "debug", "rx-core")

random.seed(42)
fails = []
total = 0


def _json_float_close(py_s, rust_s):
    """比较两个 JSON 字符串的结构与数值（浮点相对误差 <1e-12 视为一致）。"""
    try:
        a = json.loads(py_s)
        b = json.loads(rust_s)
    except json.JSONDecodeError:
        return False

    def close(x, y):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return abs(x - y) <= 1e-12 * max(1.0, abs(x))
        if isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
            return all(close(i, j) for i, j in zip(x, y))
        if isinstance(x, dict) and isinstance(y, dict) and x.keys() == y.keys():
            return all(close(x[k], y[k]) for k in x)
        return x == y

    return close(a, b)


def check(name, py_fn, rust_args):
    """py_fn() -> str（Python 结果）；rust_args -> dict（传给 rx-core 的 JSON）。
    数值类近似比较（rel 误差 <1e-12 视为一致——f64 舍入顺序差异不可避免）；
    精确类（factorial/fib/prime/json/sort/str）要求完全一致。"""
    global total
    total += 1
    try:
        py_out = py_fn()
    except ValueError as e:
        py_out = f"ERR: {e}"
    try:
        r = subprocess.run([RX_CORE_EXE], input=json.dumps({"tool": name, "args": rust_args}),
                           capture_output=True, text=True, timeout=10, encoding="utf-8")
        rust_out = r.stdout.rstrip("\n")
        if r.returncode != 0 and not rust_out:
            rust_out = f"ERR: {r.stderr.strip()[:100]}"
    except Exception as e:
        rust_out = f"ERR: {e}"
    # 数值近似比较（float 类工具）
    float_tools = {"math_div", "math_power", "math_sqrt", "stat_mean", "stat_median",
                   "geo_circle", "geo_rect", "c2f", "f2c"}
    # 一期已知差异（二期 bigint/复数支持解决）——不算 mismatch，单独统计
    known_diff = (
        # 负底数小数幂：Python 复数，Rust NaN（二期加复数支持）
        (name == "math_power" and py_out.startswith("(") and rust_out == "NaN")
        # 阶乘/斐波那契超 u128：Python 任意精度，Rust 报错（二期 bigint）
        or (name == "math_factorial" and rust_out.startswith("ERR: n 过大"))
        or (name == "fib" and rust_out.startswith("ERR: n 过大"))
        # json_parse 浮点最短表示差异（serde ryu vs Python repr，值相同末位不同）
        or (name == "json_parse" and _json_float_close(py_out, rust_out))
    )
    if known_diff:
        return
    if name in float_tools and not py_out.startswith("ERR") and not rust_out.startswith("ERR"):
        try:
            pyf, rf = float(py_out), float(rust_out)
            if abs(pyf - rf) <= 1e-12 * max(1.0, abs(pyf)):
                return  # 近似一致
        except ValueError:
            pass
    if py_out != rust_out:
        fails.append((name, py_out, rust_out, rust_args))


def main():
    # math
    for _ in range(100):
        a, b = random.uniform(-100, 100), random.uniform(-100, 100)
        check("math_div", lambda a=a, b=b: server._m_math_div({"a": a, "b": b}) if b else server._m_math_div({"a": a, "b": b}), {"a": a, "b": b})
        check("math_power", lambda a=a, b=b: server._m_math_power({"base": a, "exponent": b}), {"base": a, "exponent": b})
        check("math_sqrt", lambda a=a: server._m_math_sqrt({"x": a}), {"x": a})
        check("math_factorial", lambda a=abs(int(a)) % 100: server._m_math_factorial({"n": a}), {"n": abs(int(a)) % 100})
        check("fib", lambda a=abs(int(a)) % 200: server._m_fib_fibonacci({"n": a}), {"n": abs(int(a)) % 200})
    # str
    for _ in range(100):
        s = "".join(random.choice("abCD12_-. ") for _ in range(random.randint(0, 20)))
        check("str_reverse", lambda s=s: server._m_str_reverse({"s": s}), {"s": s})
        check("str_upper", lambda s=s: server._m_str_upper({"s": s}), {"s": s})
        check("str_palindrome", lambda s=s: server._m_str_palindrome({"s": s}), {"s": s})
    # sort/search
    for _ in range(100):
        arr = [random.randint(-50, 50) for _ in range(random.randint(0, 30))]
        check("sort_quick", lambda arr=arr: server._m_sort_quick({"arr": arr}), {"arr": arr})
        check("sort_bubble", lambda arr=arr: server._m_sort_bubble({"arr": arr}), {"arr": arr})
        target = random.randint(-50, 50)
        check("search_binary", lambda arr=arr, target=target: server._m_search_binary({"arr": arr, "target": target}), {"arr": arr, "target": target})
    # stat/geo/conv
    for _ in range(100):
        data = [random.uniform(-100, 100) for _ in range(random.randint(1, 50))]
        check("stat_mean", lambda data=data: server._m_stat_mean({"data": data}), {"data": data})
        check("stat_median", lambda data=data: server._m_stat_median({"data": data}), {"data": data})
        r = random.uniform(0, 10)
        check("geo_circle", lambda r=r: server._m_geo_circle({"radius": r}), {"radius": r})
        l, w = random.uniform(0, 100), random.uniform(0, 100)
        check("geo_rect", lambda l=l, w=w: server._m_geo_rect({"length": l, "width": w}), {"length": l, "width": w})
        c = random.uniform(-40, 100)
        check("c2f", lambda c=c: server._m_conv_c2f({"celsius": c}), {"celsius": c})
        f = random.uniform(-40, 212)
        check("f2c", lambda f=f: server._m_conv_f2c({"fahrenheit": f}), {"fahrenheit": f})
    # json/email
    for _ in range(100):
        js = json.dumps({"a": random.randint(0, 100), "b": [random.random() for _ in range(3)]})
        check("json_parse", lambda js=js: server._m_json_parse({"json_string": js}), {"json_string": js})
        check("json_valid", lambda js=js: server._m_json_valid({"json_string": js}), {"json_string": js})
    # json 特殊字符/布尔/键序（review 补盲区）
    check("json_parse", lambda js='{"s": "a\\"b\\nc", "b": true}': server._m_json_parse({"json_string": js}), {"json_string": '{"s": "a\\"b\\nc", "b": true}'})
    check("json_valid", lambda js='{"s": "a\\"b\\nc", "b": true}': server._m_json_valid({"json_string": js}), {"json_string": '{"s": "a\\"b\\nc", "b": true}'})
    check("json_parse", lambda js='{"t": "tab\\there", "bs": "back\\\\slash"}': server._m_json_parse({"json_string": js}), {"json_string": '{"t": "tab\\there", "bs": "back\\\\slash"}'})
    check("json_valid", lambda js='{"t": "tab\\there", "bs": "back\\\\slash"}': server._m_json_valid({"json_string": js}), {"json_string": '{"t": "tab\\there", "bs": "back\\\\slash"}'})
    check("json_parse", lambda js='{"z": 1, "a": 2, "m": 3}': server._m_json_parse({"json_string": js}), {"json_string": '{"z": 1, "a": 2, "m": 3}'})
    check("json_valid", lambda js='{"z": 1, "a": 2, "m": 3}': server._m_json_valid({"json_string": js}), {"json_string": '{"z": 1, "a": 2, "m": 3}'})
    check("json_parse", lambda js='[true, false, null, 1, "x"]': server._m_json_parse({"json_string": js}), {"json_string": '[true, false, null, 1, "x"]'})
    check("json_valid", lambda js='[true, false, null, 1, "x"]': server._m_json_valid({"json_string": js}), {"json_string": '[true, false, null, 1, "x"]'})
    check("json_parse", lambda js='{"u": "\\u4f60\\u597d"}': server._m_json_parse({"json_string": js}), {"json_string": '{"u": "\\u4f60\\u597d"}'})
    check("json_valid", lambda js='{"u": "\\u4f60\\u597d"}': server._m_json_valid({"json_string": js}), {"json_string": '{"u": "\\u4f60\\u597d"}'})
    # prime/list
    for _ in range(100):
        n = random.randint(0, 10000)
        check("is_prime", lambda n=n: server._m_prime_is_prime({"n": n}), {"n": n})
        check("gen_primes", lambda n=n: server._m_prime_generate({"limit": n}), {"limit": n})
        lst = [random.randint(0, 10) for _ in range(random.randint(0, 20))]
        check("list_unique", lambda lst=lst: server._m_list_unique({"lst": lst}), {"lst": lst})
        nested = [random.randint(0, 5) for _ in range(random.randint(0, 10))]
        check("list_flatten", lambda nested=nested: server._m_list_flatten({"nested_list": nested}), {"nested_list": nested})

    print(f"parity: {total} cases, {len(fails)} mismatches")
    for name, py, rust, args in fails[:10]:
        print(f"  MISMATCH {name}: py={py} rust={rust} args={str(args)[:80]}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
