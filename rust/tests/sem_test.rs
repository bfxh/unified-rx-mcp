//! code_semantic Rust 原生实现的契约测试（S81）。
//! 直接打 rxrs::sem::code_semantic：四种语言的定义匹配器（含 impl `for` 回溯、
//! go 接收者、.ts 非 js 怪癖）、注释折叠、名称 trigram、body 40 行采样帽、
//! related 锚点（精确 + 模糊）、阈值、空语料无 mode 键、walk 先文件后目录判别。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::json::Value;
use rxrs::sem;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-sem-test-{}-{}", tag, n));
        fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn write_rel(root: &Path, rel: &str, content: &str) {
    let p = root.join(rel);
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(&p, content).unwrap();
}

fn get_str<'a>(v: &'a Value, k: &str) -> &'a str {
    match v.get(k) {
        Some(Value::Str(s)) => s,
        other => panic!("{} 应为字符串，实得 {:?}", k, other),
    }
}

fn get_i128(v: &Value, k: &str) -> i128 {
    match v.get(k) {
        Some(Value::Int(i)) => *i,
        other => panic!("{} 应为整数，实得 {:?}", k, other),
    }
}

fn get_f64(v: &Value, k: &str) -> f64 {
    match v.get(k) {
        Some(Value::Float(f)) => *f,
        other => panic!("{} 应为浮点，实得 {:?}", k, other),
    }
}

fn get_arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    match v.get(k) {
        Some(Value::Arr(a)) => a,
        other => panic!("{} 应为数组，实得 {:?}", k, other),
    }
}

fn symbols(res: &Value) -> Vec<&str> {
    get_arr(res, "hits").iter().map(|h| get_str(h, "symbol")).collect()
}

/// S31 回归基准树（与 tests/test_semantic.py 的 _make_tree 同款）。
fn write_bench_tree(root: &Path) {
    write_rel(root, "clock.rs", "pub struct Clock {\n    pub elapsed: f32,  // 经过的时间\n}\n\nimpl Clock {\n    // 时钟累加\n    pub fn tick(&mut self, dt: f32) {\n        self.elapsed += dt;\n    }\n}\n\npub fn reset_clock(c: &mut Clock) {\n    c.elapsed = 0.0;\n}\n");
    write_rel(root, "drive.rs", "// 旋转载具角度\npub fn rotate_vehicle_y(angle: f32) {\n    let rad = angle.to_radians();\n    let _ = rad;\n}\n\npub fn drive_forward(speed: f32) {\n    let _ = speed;\n}\n");
    write_rel(root, "ui.py", "def render_panel(stats):\n    \"\"\"画载具面板。\"\"\"\n    return stats\n\n\nclass PanelCache:\n    def __init__(self):\n        self.items = []\n");
}

#[test]
fn search_cjk_comment_bridge_and_kinds() {
    let td = TempDir::new("cjk");
    write_bench_tree(td.path());
    // 注释折叠：tick 上方紧邻注释进 body → 注释词命中函数
    let res = sem::code_semantic(td.path(), "时钟累加", "search", 5);
    assert!(res.get("error").is_none(), "不应报错");
    assert_eq!(get_str(&res, "mode"), "search");
    assert!(symbols(&res).iter().any(|s| *s == "tick"), "实得 {:?}", symbols(&res));
    // 旧 S31 回归查询
    let res = sem::code_semantic(td.path(), "时钟经过的时间累加", "search", 5);
    assert!(symbols(&res).iter().any(|s| *s == "tick" || *s == "Clock"));
    // kind 区分：struct→type
    let res = sem::code_semantic(td.path(), "经过的时间", "search", 5);
    assert!(get_arr(&res, "hits").iter().any(|h| get_str(h, "symbol") == "Clock"
        && get_str(h, "kind") == "type"));
    // search 命中带 snippet（重读定义行）
    let hit = &get_arr(&res, "hits")[0];
    assert!(!get_str(hit, "snippet").is_empty(), "search 命中应带 snippet");
}

#[test]
fn py_def_class_and_name_trigram() {
    let td = TempDir::new("pydefs");
    write_rel(td.path(), "ui.py", "def render_panel(stats):\n    return stats\n\n\nclass PanelCache:\n    def __init__(self):\n        self.items = []\n");
    // 名称 trigram：rotat… 前缀查询命中 rotate_vehicle_y 同理走名称拆词+trigram
    let res = sem::code_semantic(td.path(), "render panel", "search", 5);
    assert!(symbols(&res).iter().any(|s| *s == "render_panel"));
    let res = sem::code_semantic(td.path(), "panel cache", "search", 5);
    assert!(symbols(&res).iter().any(|s| *s == "PanelCache"));
    // def 与 class 同 kind="def"（Python 侧不细分）
    let hit = &get_arr(&res, "hits").iter().find(|h| get_str(h, "symbol") == "PanelCache").unwrap();
    assert_eq!(get_str(hit, "kind"), "def");
}

#[test]
fn rs_impl_for_backtracks_to_type_name() {
    // 旧正则 impl(?:<...>)?\s+(?:\w+\s+for\s+)?(\w+) 捕获 for 右侧的类型名；
    // 若 Rust 侧错捕左侧 trait，related 精确锚点就找不到 FooWidget
    let td = TempDir::new("implfor");
    write_rel(td.path(), "foo.rs", "impl Display for FooWidget {}\n");
    let res = sem::code_semantic(td.path(), "FooWidget", "related", 5);
    assert_eq!(get_str(&res, "anchor"), "FooWidget");
    // 泛型 + for 同理
    let td = TempDir::new("implgen");
    write_rel(td.path(), "bar.rs", "impl<T> Repo for BarStore<T> {}\n");
    let res = sem::code_semantic(td.path(), "BarStore", "related", 5);
    assert_eq!(get_str(&res, "anchor"), "BarStore");
    // 无 for 的普通 impl 取首个 ident；pub async fn 照常收
    let td = TempDir::new("implplain");
    write_rel(td.path(), "c.rs", "impl Cache {\n    pub async fn serve_forever() {}\n}\n");
    let res = sem::code_semantic(td.path(), "Cache", "related", 5);
    assert_eq!(get_str(&res, "anchor"), "Cache");
    let res = sem::code_semantic(td.path(), "serve_forever", "related", 5);
    assert_eq!(get_str(&res, "anchor"), "serve_forever");
}

#[test]
fn go_receiver_and_bare_func() {
    // 两个 func 分文件放：定义体向後采样 40 行会吞进相邻定义行，
    // 同文件会让 FetchItem 的 body 含 NewStore 全套 token，判别失效。
    let td = TempDir::new("go");
    write_rel(td.path(), "repo.go", "package store\n\nfunc (s *Repo) FetchItem(k string) string { return k }\n");
    write_rel(td.path(), "main.go", "package store\n\nfunc NewStore() *Store { return nil }\n");
    let res = sem::code_semantic(td.path(), "FetchItem", "related", 5);
    assert_eq!(get_str(&res, "anchor"), "FetchItem", "接收者形式 func (s *Repo) FetchItem 的名字应取 FetchItem");
    let res = sem::code_semantic(td.path(), "NewStore", "search", 5);
    assert_eq!(get_i128(&res, "total"), 1, "裸 func 也须收定义");
    assert_eq!(get_str(&get_arr(&res, "hits")[0], "kind"), "fn");
}

#[test]
fn js_fn_class_and_ts_not_js_quirk() {
    let td = TempDir::new("js");
    write_rel(td.path(), "app.js", "export async function loadData() {}\n\nexport class Widget {}\n\nclass Plain {}\n");
    write_rel(td.path(), "mod.ts", "function hiddenTsFn() {}\n");
    let res = sem::code_semantic(td.path(), "loadData", "search", 5);
    assert!(symbols(&res).iter().any(|s| *s == "loadData"));
    // related 命中不带 snippet（契约形状差异）
    let res = sem::code_semantic(td.path(), "Widget", "related", 5);
    assert_eq!(get_str(&res, "mode"), "related");
    assert_eq!(get_str(&res, "anchor"), "Widget");
    assert!(get_arr(&res, "hits").iter().all(|h| h.get("snippet").is_none()));
    let res = sem::code_semantic(td.path(), "Plain", "related", 5);
    assert_eq!(get_str(&res, "anchor"), "Plain", "无 export 前缀的 class 也收");
    // .ts/.tsx/.jsx 不算 js（S31 起的怪癖原样保留）：隐藏函数不产生定义
    let res = sem::code_semantic(td.path(), "hiddenTsFn", "search", 5);
    assert_eq!(get_i128(&res, "total"), 0, ".ts 不是 js，不得提取定义");
}

#[test]
fn related_fuzzy_anchor_when_name_absent() {
    let td = TempDir::new("fuzzy");
    write_rel(td.path(), "a.py", "def truth_exists():\n    pass\n");
    // 名字绝不存在的查询 → 余弦全 0 → 首个定义当模糊锚点
    let res = sem::code_semantic(td.path(), "这个符号绝不存在 zz", "related", 5);
    assert_eq!(get_str(&res, "anchor"), "truth_exists");
    assert_eq!(get_i128(&res, "total"), 0, "全 0 分低于 0.05 阈值，related 无邻居");
}

#[test]
fn search_threshold_no_match_is_zero() {
    let td = TempDir::new("nomatch");
    write_bench_tree(td.path());
    let res = sem::code_semantic(td.path(), "zzz qq wwww", "search", 5);
    assert_eq!(get_str(&res, "mode"), "search");
    assert_eq!(get_i128(&res, "total"), 0, "低于 0.02 阈值应逐个断开");
}

#[test]
fn empty_corpus_has_no_mode_key() {
    let td = TempDir::new("emptycorpus");
    let res = sem::code_semantic(td.path(), "任何查询", "search", 5);
    assert!(res.get("mode").is_none(), "空语料返回不含 mode 键（S31 契约）");
    assert_eq!(get_i128(&res, "total"), 0);
    assert_eq!(get_str(&res, "query"), "任何查询");
}

#[test]
fn walk_files_before_dirs_under_cap() {
    // 201 文件判别法（与 search_test 同款）：sub/ 烧名额时 z.py 不得落榜
    let td = TempDir::new("walkorder");
    write_rel(td.path(), "a.py", "def alphamarker_base(): pass\n");
    write_rel(td.path(), "z.py", "def zeta_zfind_fn():\n    pass\n");
    for i in 0..199 {
        write_rel(td.path(), &format!("sub/f{:03}.py", i), "def filler(): pass\n");
    }
    let res = sem::code_semantic(td.path(), "zeta_zfind_fn", "search", 10);
    assert_eq!(get_i128(&res, "total"), 1, "z.py 必须入选语料");
    assert!(symbols(&res).contains(&"zeta_zfind_fn"));
}

#[test]
fn k_caps_search_results() {
    let td = TempDir::new("kcap");
    for i in 0..5 {
        write_rel(td.path(), &format!("f{}.py", i), "def widget_part(): pass\n");
    }
    let res = sem::code_semantic(td.path(), "widget part", "search", 2);
    assert_eq!(get_i128(&res, "total"), 2);
}

#[test]
fn body_cap_40_lines_excludes_far_markers() {
    // 定义体只采样 40 行：第 45 行的标记不得把 alpha 提上榜
    //（标记选 qzkvx_marker99 并让 alpha 名字用 distance——与查询无 trigram 重叠，
    //  否则名称 trigram 本身就会把 alpha 拉上榜，判别失效）
    let td = TempDir::new("bodycap");
    let mut big = String::from("def alpha_distance_fn():\n");
    for i in 1..44 {
        big.push_str(&format!("x{} = {}\n", i, i));
    }
    big.push_str("qzkvx_marker99 = 1\n");
    write_rel(td.path(), "big.py", &big);
    write_rel(td.path(), "small.py", "def beta_qzkvx_fn():\n    qzkvx_marker99 = 1\n");
    let res = sem::code_semantic(td.path(), "qzkvx_marker99", "search", 10);
    assert_eq!(get_i128(&res, "total"), 1, "body 40 行帽应把 big.py 的远标记排除");
    assert_eq!(get_str(&get_arr(&res, "hits")[0], "symbol"), "beta_qzkvx_fn");
}

#[test]
fn not_a_dir_is_error_object() {
    let td = TempDir::new("notdir");
    let res = sem::code_semantic(&td.path().join("nope"), "q", "search", 5);
    match res.get("error") {
        Some(Value::Str(e)) => assert!(e.contains("不是目录"), "实得 {}", e),
        other => panic!("应为 error 对象，实得 {:?}", other),
    }
}

#[test]
fn scores_rounded_and_positive() {
    let td = TempDir::new("round");
    write_bench_tree(td.path());
    let res = sem::code_semantic(td.path(), "rotat vehicle", "search", 5);
    assert!(symbols(&res).iter().any(|s| *s == "rotate_vehicle_y"), "trigram 应命中部分名");
    for h in get_arr(&res, "hits") {
        let s = get_f64(h, "score");
        assert!(s > 0.02, "低于阈值不应上榜：{}", s);
        assert_eq!(s, (s * 1000.0).round() / 1000.0, "score 应为三位小数圆整：{}", s);
    }
}
