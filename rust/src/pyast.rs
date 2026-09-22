//! pyast —— 手写 Python 迷你解析器（S83，bug_scan 原生化的地基）。
//!
//! 为什么手写：红线禁止第三方 crate（没有 rustpython/syn 可用），而 bug_scan 的
//! 未定义变量检测需要 Load/Store 上下文判定 + ast.walk 的 BFS 遍历序——正则做不到。
//!
//! 保真目标：与 CPython `ast` 的**观察等价**（不追求完整文法）：
//! - 每个节点 children 严格按 ASDL 字段声明序排列（= ast.iter_child_nodes 顺序，
//!   ast.walk 的 BFS 事件序由此决定，同文件同行 tie 的次序依赖它）
//! - Name.ctx 三值 Load/Store/Del；赋值目标只在**最外层**节点标 Store，
//!   Tuple/List/Starred 元素递归标 Store，Subscript/Attribute 内部的 Name 保持 Load
//!   （`a[i] = x` 的 a、i 都是 Load——与 ast 一致）
//! - 节点行号 = 首 token 行；f-string 的 {} 区域递归解析为 FormattedValue 子树
//! - 已知怪癖刻意保留（与 tools/scan.py::_scan_python 对齐）：
//!   Lambda 只收 args+kwonlyargs（vararg/kwarg 不算定义）→ `lambda *a: a` 报
//!   未定义变量 'a'；ClassDef 的 bases Names 无条件入 defined、keywords 不收
//! - fail-soft：match 语句按宽松的 token 扫描解析（捕获名标 Store）；罕见文法
//!   （如 3.12 的 f-string 嵌套同类引号）宁可漏事件也不 panic
//!
//! 已知偏离（语料与真实仓库不触发，见 S83 对照实验）：
//! - 未终止字符串的 SyntaxError msg 不带 "(detected at line N)" 后缀
//! - f"{a:{w}}{b}" 的 spec 子表达式事件序与 ast 相比整体提前（同为 tie 内次序）
//! - 文件头 BOM 不报错（Python 报 invalid non-printable character）

/// Name/Tuple/List/Starred/Attribute/Subscript 的访问上下文。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ctx {
    Load,
    Store,
    Del,
}

/// Constant 节点的值载荷（S84：ast_scan 的 secret_literal 要字符串值、
/// shell_like_call 的 callee 要 ast.dump 形态的数值文本）。
#[derive(Clone, Debug, PartialEq)]
pub enum CVal {
    NoneC,
    Bool(bool),
    /// 数字的源码原文（含 0x/下划线）；dump 时再定 int/float 形态
    Num(String),
    Str(String),
    Bytes(Vec<u8>),
    EllipsisC,
}

/// 迷你 AST 节点。name/name2/ctx/aux/names 按节点类型取用：
/// - `name`：Name.id、FunctionDef/ClassDef.name、arg.arg、alias.name、
///   Attribute.attr、ExceptHandler.name（except-as）、ImportFrom.module
/// - `name2`：alias.asname
/// - `aux`：ClassDef = bases 个数（children 前 aux 个是 bases）；ExceptHandler = 有无 type
/// - `names`：Global/Nonlocal 的名字表
#[derive(Clone, Debug)]
pub struct PyNode {
    pub kind: &'static str,
    pub line: usize,
    /// CPython col_offset 口径的列号（0 基、按字符计）。S84 起只对 ast_scan 用到的
    /// 节点赋值：Call（后缀链首 token，含前置括号）与字符串 Constant（含前缀）。
    pub col: usize,
    pub name: String,
    pub name2: String,
    pub ctx: Ctx,
    pub aux: usize,
    pub names: Vec<String>,
    /// S135：装饰器个数（FunctionDef/AsyncFunctionDef/ClassDef 专用；其余恒 0）。
    /// 装饰器表达式挂在 children 尾部，本计数是从尾部数回去的唯一边界依据。
    pub deco: usize,
    pub cval: CVal,
    pub children: Vec<PyNode>,
}

impl PyNode {
    fn new(kind: &'static str, line: usize) -> PyNode {
        PyNode {
            kind,
            line,
            col: 0,
            name: String::new(),
            name2: String::new(),
            ctx: Ctx::Load,
            aux: 0,
            names: Vec::new(),
            deco: 0,
            cval: CVal::NoneC,
            children: Vec::new(),
        }
    }

    fn with_name(kind: &'static str, line: usize, name: String) -> PyNode {
        let mut n = PyNode::new(kind, line);
        n.name = name;
        n
    }
}

/// 解析错误：等价 SyntaxError 的 (lineno, msg)——msg 逐字对齐 3.14 实测（S83 对照实验）。
#[derive(Clone, Debug)]
pub struct PyErr {
    pub line: usize,
    pub msg: String,
}

// ---------- 词法 ----------

#[derive(Clone, Debug)]
enum Tok {
    Name(String),
    Kw(&'static str),
    /// 数字源码原文（含 0x/0o/0b/下划线/小数/指数），值形态由 dump 层再定
    Num(String),
    /// 解码后的字符串字面量值（S84：raw 不走转义；bytes 单独收集）
    Str(StrLit),
    /// f-string：内插区域表（区域源码 + 区域首行）
    FStr(Vec<FRegion>),
    Op(String),
    Newline,
    Indent,
    Dedent,
    End,
}

/// 字符串字面量的解码结果：str 用 s，bytes 用 b（互斥；隐式拼接在此之上折叠）。
#[derive(Clone, Debug)]
pub struct StrLit {
    pub s: String,
    pub b: Vec<u8>,
    pub is_bytes: bool,
}

#[derive(Clone, Debug)]
struct FRegion {
    src: String,
    line: usize,
    /// '{' 的 0 基列号：区域内第 1 行的 col 映射基准（CPython 3.12+ 位置保真）
    col: usize,
}

struct TokOut {
    kind: Tok,
    line: usize,
    /// token 起始的 0 基字符列（S84：Call/str-Constant 的 col_offset 用）
    col: usize,
}

const KEYWORDS: &[&str] = &[
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
    "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
    "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
    "try", "while", "with", "yield",
];
// match/case/type 是软关键字：词法一律出 Name，语句级按上下文试探（失败回退表达式）

const PREFIXES: &[&str] = &["r", "u", "b", "f", "br", "rb", "fr", "rf"];
const OPS3: &[&str] = &["**=", "//=", ">>=", "<<=", "..."];
const OPS2: &[&str] = &[
    "**", "//", "<<", ">>", "<=", ">=", "==", "!=", "->", ":=", "+=", "-=", "*=", "/=", "%=",
    "&=", "|=", "^=", "@=",
];


// ── S167：按域拆出两个子模块（mod 根留在本文件；子模块用 use super::*; 取父作用域）
mod lex;
mod parse;
mod parse_pattern;   // S167：从 parse.rs 按域拆出的三段（match 模式 / 简单语句 / 表达式优先级）
mod parse_simple;
mod parse_expr;
use self::parse::Parser;
use self::lex::tokenize;

/// 解析整个模块（bug_scan 入口）。
pub fn parse_module(src: &str) -> Result<PyNode, PyErr> {
    let toks = tokenize(src)?;
    let mut p = Parser { t: toks, i: 0 };
    p.run_module()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 收集一行源码里 f-string 的全部区域 (line, src)，按扁平化顺序。
    fn fstr_regions(src: &str) -> Vec<(usize, String)> {
        let toks = tokenize(src).expect("tokenize");
        let mut out = Vec::new();
        for t in &toks {
            if let Tok::FStr(rs) = &t.kind {
                for r in rs {
                    out.push((r.line, r.src.clone()));
                }
            }
        }
        out
    }

    #[test]
    fn fstring_conversion_and_debug_eq_are_cut_from_expr() {
        // !r 等转换符与调试 '=' 必须真截断（区域是源切片，跳过字节无效）
        let rs = fstr_regions("s = f\"{name!r} is {len(name):>{width}}\"\n");
        assert_eq!(
            rs,
            vec![
                (1, "name".to_string()),
                (1, "len(name)".to_string()),
                (1, "width".to_string()),
            ]
        );
        let dbg = fstr_regions("s = f\"{name=}\"\n");
        assert_eq!(dbg, vec![(1, "name".to_string())]);
    }

    #[test]
    fn fstring_bracket_colon_is_not_spec() {
        // 切片/下标/lambda 的 ':' 在括号内（sq>0）不得触发 format-spec 臂
        parse_module("raise ValueError(f\"bad: {lines[-1][:200]}\")\n")
            .expect("slice colon inside f-string");
        parse_module("g = f\"{(lambda x: x)(1)}\"\n").expect("lambda colon inside f-string");
        // 顶层冒号仍是 spec 起点
        let rs = fstr_regions("s = f\"{val:>8}\"\n");
        assert_eq!(rs, vec![(1, "val".to_string())]);
    }

    #[test]
    fn match_soft_keyword_dispatches_and_falls_back() {
        parse_module(concat!(
            "def h(cmd, v):\n",
            "    match cmd:\n",
            "        case [1, 2, rest]:\n",
            "            return rest\n",
            "        case {\"k\": vv}:\n",
            "            return vv\n",
            "        case Point(x=px):\n",
            "            return px\n",
            "        case _:\n",
            "            return v\n",
            "    match = 1\n",
            "    return match\n",
        ))
        .expect("match 语句与软关键字回退（match 作标识符）共存");
    }

    #[test]
    fn del_starred_is_syntax_error_like_cpython() {
        match parse_module("del *a\n") {
            Ok(_) => panic!("del *a 应为语法错误（CPython: cannot delete starred）"),
            Err(e) => assert_eq!(e.line, 1),
        }
    }

    /// S84：col_offset 口径（探针实测 CPython）——Call 列 = 链首 token（含前置
    /// 括号）；字符串 Constant 列含前缀；括号对原子透明。
    #[test]
    fn cols_match_cpython_probes() {
        let got = |src: &str, kind: &str| -> usize {
            let tree = parse_module(src).expect("parse");
            let mut out = Vec::new();
            fn go(n: &PyNode, kind: &str, out: &mut Vec<usize>) {
                if n.kind == kind {
                    out.push(n.col);
                }
                for c in &n.children {
                    go(c, kind, out);
                }
            }
            go(&tree, kind, &mut out);
            *out.last().expect("node found")
        };
        // (a+b)(x)：Call 列在 '('，内层 BinOp 不参与
        assert_eq!(got("y = (a+b)(x)\n", "Call"), 4);
        // 嵌套调用两层 Call 同列（都挂在链首 f）
        assert_eq!(got("y = f(a)(b)\n", "Call"), 4);
        // 下标链：Call 列在链首 d
        assert_eq!(got("d['k'].system(x)\n", "Call"), 0);
        // 括号对字符串透明：列落在引号上
        assert_eq!(got("x = (\"ab\")\n", "Constant"), 5);
        // 前缀计入列：rb 的 r（0 基第 4 列）
        assert_eq!(got("x = rb'ab'\n", "Constant"), 4);
    }

    /// S84：字符串值解码——转义还原、raw 保形、bytes 收集、隐式拼接折叠。
    #[test]
    fn string_values_decode_like_python() {
        let val = |src: &str| -> CVal {
            let tree = parse_module(src).expect("parse");
            let mut out = Vec::new();
            fn go(n: &PyNode, out: &mut Vec<CVal>) {
                if n.kind == "Constant" {
                    out.push(n.cval.clone());
                }
                for c in &n.children {
                    go(c, out);
                }
            }
            go(&tree, &mut out);
            out.pop().expect("constant")
        };
        assert_eq!(val("x = 'a\\n\\t\\x41\\101\\\\'\n"), CVal::Str("a\n\tAA\\".into()));
        assert_eq!(val("x = r'a\\n'\n"), CVal::Str("a\\n".into()));
        assert_eq!(val("x = b'ab'\n"), CVal::Bytes(vec![b'a', b'b']));
        // 未知转义保形
        assert_eq!(val("x = '\\q'\n"), CVal::Str("\\q".into()));
        // 隐式拼接
        assert_eq!(val("x = 'ab' 'cd'\n"), CVal::Str("abcd".into()));
        // bytes 与 str 混排：CPython 同款报错
        assert!(parse_module("x = b'a' 'b'\n").is_err());
        // 数字原文入 cval
        assert_eq!(val("x = 0x1F\n"), CVal::Num("0x1F".into()));
    }

    /// S84：算子名入节点——dump_expr 依赖 BinOp.name / Compare.names / BoolOp.name。
    #[test]
    fn operator_names_recorded() {
        let tree = parse_module("r = a // 2 if x < 1 and not y else None\n").expect("parse");
        let mut bins = Vec::new();
        let mut cmps = Vec::new();
        let mut bools = Vec::new();
        fn go(n: &PyNode, b: &mut Vec<String>, c: &mut Vec<Vec<String>>, d: &mut Vec<String>) {
            match n.kind {
                "BinOp" => b.push(n.name.clone()),
                "Compare" => c.push(n.names.clone()),
                "BoolOp" => d.push(n.name.clone()),
                _ => {}
            }
            for ch in &n.children {
                go(ch, b, c, d);
            }
        }
        go(&tree, &mut bins, &mut cmps, &mut bools);
        assert_eq!(bins, vec!["FloorDiv"]);
        assert_eq!(cmps, vec![vec!["Lt"]]);
        assert_eq!(bools, vec!["And"]);
    }

    #[test]
    fn multiline_bracket_expressions_keep_line_flow() {
        parse_module("x = ([1] +\n     [2])\nprint(x)\n").expect("括号续行");
        parse_module("def f():\n    d = {\n        \"a\": 1}\n    print(d)\n").expect("字典续行");
        parse_module(
            "def m(pairs, dd):\n    for i, (tag, src) in enumerate([(\"prog\", pairs)] +\n\
             ([(\"data\", dd)] if dd else [])):\n        res = run_one(tag, src)\n    print(1)\n",
        )
        .expect("for 目标元组解包 + 续行");
    }
}
