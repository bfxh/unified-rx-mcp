//! taint 子模块（S168 从 taint.rs 拆出；纯搬移，未改语义）。
use super::*;

#[derive(Clone, Debug)]
pub(crate) struct TSrc {
    pub(crate) line: usize,
    pub(crate) kind: String,
    pub(crate) interproc: bool,
    pub(crate) definite: bool, // 入口可达（@tool 入口形参 / 宿主数据源）= 实锤；内部形参流 = clue
    pub(crate) origin: Option<String>, // S128：跨文件链证据（"a.py:12 sys.argv → b.py:read_it.path"）
}

#[derive(Clone, Debug)]
pub(crate) struct Hit {
    pub(crate) var: String,
    pub(crate) line: usize,
    pub(crate) kind: String,
    pub(crate) interproc: bool,
    pub(crate) definite: bool,
    pub(crate) origin: Option<String>, // S128：跨文件链证据（有则 flow=cross）
}

pub(crate) struct Scope {
    pub(crate) name: String,
    pub(crate) indent: usize,
    pub(crate) parent: Option<usize>,
    pub(crate) params: Vec<String>,
    pub(crate) taint: HashMap<String, TSrc>,
    pub(crate) rets: Vec<(usize, usize, usize)>, // (起, 止, 行)
    pub(crate) ret_tainted: bool,
    pub(crate) entry: bool, // @tool 装饰 = MCP 宿主可达入口
}

#[derive(Clone)]
pub(crate) struct ArgSlice {
    pub(crate) start: usize,
    pub(crate) end: usize,
    pub(crate) kw: Option<String>,
}

#[derive(Clone)]
pub(crate) struct CallRec {
    pub(crate) callee: String,       // 点路径如 os.remove；方法形式存属性名
    pub(crate) method: bool,         // true = .attr( 形式（有接收者）
    pub(crate) recv: (usize, usize), // 接收者 token 区间（method 时有效）
    pub(crate) args: Vec<ArgSlice>,
    pub(crate) line: usize,
    pub(crate) scope: usize,
    pub(crate) lhs: Vec<String>, // 语句级赋值目标（x = f(...) 记录 x）
}

pub(crate) struct Analyzer {
    pub(crate) toks: Vec<Tok>,
    pub(crate) scopes: Vec<Scope>,
    pub(crate) calls: Vec<CallRec>,
    pub(crate) file: String,
    pub(crate) naive: bool,
}

