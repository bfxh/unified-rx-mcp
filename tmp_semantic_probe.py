# 测试文件：code_context / change_impact / lsp_query 接线验证用
def helper_add(a, b):
    """加法辅助函数。"""
    return a + b


def helper_mul(a, b):
    """乘法辅助函数。"""
    return a * b


def main():
    x = helper_add(1, 2)
    y = helper_mul(x, 3)
    return y


if __name__ == "__main__":
    print(main())
