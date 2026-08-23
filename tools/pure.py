# -*- coding: utf-8 -*-
"""tools/pure.py —— 纯函数域（2 工具）：pure_funcs / pure_batch

收敛自旧版 52 ciopt_* + 33 math/str/json/sort/prime/stat/geo + 4 stats = ~89 个
→ 合并为 2 个组合工具（action 分发 + 批量执行器）。

动作清单（action 名）：
- 数学: add sub mul div power sqrt abs factorial mod floor_div
- 文本: upper lower reverse palindrome
- JSON: dict_to_json json_to_dict is_valid_json
- 校验: is_email_valid is_phone_valid is_strong_password
- 列表: unique flatten
- 排序查找: quick_sort bubble_sort binary_search
- 素数: is_prime generate_primes
- 统计: mean median
- 几何: circle_area rect_perimeter
- 温度: c2f f2c
"""
import json
import math
import statistics

from registry import tool


def _fact(n):
    if n < 0:
        raise ValueError("n 必须 ≥0")
    return math.factorial(n)


def _is_prime(n):
    if n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0 or n % 3 == 0:
        return False
    i = 5
    while i * i <= n:
        if n % i == 0 or n % (i + 2) == 0:
            return False
        i += 6
    return True


def _quick_sort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    mid = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return _quick_sort(left) + mid + _quick_sort(right)


def _bin_search(arr, target):
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def _unique(lst):
    seen = []
    for x in lst:
        if x not in seen:
            seen.append(x)
    return seen


def _flatten(nested):
    out = []
    for x in nested:
        if isinstance(x, list):
            out.extend(_flatten(x))
        else:
            out.append(x)
    return out


def _dispatch(action, **kw):
    """纯函数分发。返回 (value, unit_desc)。"""
    if action == "add":
        return kw["a"] + kw["b"], ""
    if action == "sub":
        return kw["a"] - kw["b"], ""
    if action == "mul":
        return kw["a"] * kw["b"], ""
    if action == "div":
        if kw["b"] == 0:
            raise ValueError("除零")
        return kw["a"] / kw["b"], ""
    if action == "power":
        return kw["base"] ** kw["exponent"], ""
    if action == "sqrt":
        if kw["x"] < 0:
            raise ValueError("负数不能开方")
        return math.sqrt(kw["x"]), ""
    if action == "abs":
        return abs(kw["x"]), ""
    if action == "factorial":
        return _fact(kw["n"]), ""
    if action == "mod":
        return kw["a"] % kw["b"], ""
    if action == "floor_div":
        return kw["a"] // kw["b"], ""
    if action == "upper":
        return kw["s"].upper(), ""
    if action == "lower":
        return kw["s"].lower(), ""
    if action == "reverse":
        return kw["s"][::-1], ""
    if action == "palindrome":
        s = kw["s"]
        return s == s[::-1], ""
    if action == "dict_to_json":
        return json.dumps(kw["dictionary"], ensure_ascii=False), ""
    if action == "json_to_dict":
        return json.loads(kw["json_string"]), ""
    if action == "is_valid_json":
        try:
            json.loads(kw["json_string"])
            return True, ""
        except Exception:
            return False, ""
    if action == "is_email_valid":
        s = kw["email"]
        return ("@" in s and "." in s.split("@")[-1] and len(s) > 5), ""
    if action == "is_phone_valid":
        s = str(kw["phone"])
        return len(s) >= 7 and s.isdigit(), ""
    if action == "is_strong_password":
        s = kw["password"]
        return (len(s) >= 8 and any(c.isupper() for c in s)
                and any(c.islower() for c in s) and any(c.isdigit() for c in s)), ""
    if action == "unique":
        return _unique(kw["lst"]), ""
    if action == "flatten":
        return _flatten(kw["nested_list"]), ""
    if action == "quick_sort":
        return _quick_sort(kw["arr"]), ""
    if action == "bubble_sort":
        arr = list(kw["arr"])
        for i in range(len(arr)):
            for j in range(len(arr) - 1 - i):
                if arr[j] > arr[j + 1]:
                    arr[j], arr[j + 1] = arr[j + 1], arr[j]
        return arr, ""
    if action == "binary_search":
        return _bin_search(kw["arr"], kw["target"]), ""
    if action == "is_prime":
        return _is_prime(kw["n"]), ""
    if action == "generate_primes":
        limit = kw["limit"]
        return [x for x in range(2, limit + 1) if _is_prime(x)], ""
    if action == "mean":
        return statistics.mean(kw["data"]), ""
    if action == "median":
        return statistics.median(kw["data"]), ""
    if action == "circle_area":
        return math.pi * kw["radius"] ** 2, ""
    if action == "rect_perimeter":
        return 2 * (kw["length"] + kw["width"]), ""
    if action == "c2f":
        return kw["celsius"] * 9 / 5 + 32, "°F"
    if action == "f2c":
        return (kw["fahrenheit"] - 32) * 5 / 9, "°C"
    raise ValueError(f"未知纯函数动作: {action}")


@tool("pure_funcs", "纯函数组合（action 分发，~40 动作）", "pure",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "description": "动作名（add/sub/mul/div/power/sqrt/abs/factorial/upper/lower/reverse/palindrome/unique/flatten/quick_sort/bubble_sort/binary_search/is_prime/generate_primes/mean/median/circle_area/rect_perimeter/c2f/f2c/dict_to_json/json_to_dict/is_valid_json/is_email_valid/is_phone_valid/is_strong_password 等）"},
           "a": {"type": "number"}, "b": {"type": "number"},
           "base": {"type": "number"}, "exponent": {"type": "number"},
           "x": {"type": "number"}, "n": {"type": "integer"},
           "s": {"type": "string"},
           "arr": {"type": "array"}, "lst": {"type": "array"},
           "nested_list": {"type": "array"}, "data": {"type": "array"},
           "target": {"type": "number"}, "limit": {"type": "integer"},
           "radius": {"type": "number"}, "length": {"type": "number"}, "width": {"type": "number"},
           "celsius": {"type": "number"}, "fahrenheit": {"type": "number"},
           "dictionary": {"type": "object"}, "json_string": {"type": "string"},
           "email": {"type": "string"}, "phone": {"type": "string"},
           "password": {"type": "string"},
       },
       "required": ["action"]})
def pure_funcs(action, **kw):
    value, unit = _dispatch(action, **kw)
    return {"action": action, "value": value, "unit": unit}


@tool("pure_batch", "纯函数批量执行器（数组输入批量跑同一动作）", "pure",
      {"type": "object",
       "properties": {
           "action": {"type": "string"},
           "inputs": {"type": "array", "description": "批量输入数组（每元素为 kwargs dict 或标量）"},
       },
       "required": ["action", "inputs"]})
def pure_batch(action, inputs):
    results = []
    for item in inputs:
        try:
            if isinstance(item, dict):
                v, u = _dispatch(action, **item)
            else:
                v, u = _dispatch(action, s=item)
            results.append({"ok": True, "value": v})
        except Exception as e:
            results.append({"ok": False, "error": str(e)})
    return {"action": action, "count": len(results), "results": results}
