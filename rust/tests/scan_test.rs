//! scan 域三工具 Rust 原生实现的契约测试（S82）。
//! 直接打 rxrs::scan::{std_check, ui_check, bug_locate}：占位/魔法数字全边界
//! 语义（unicode \b、注释行魔法数、语言门）、遍历名额只计代码文件、godot `$`
//! 跨行、unity 无边界 new、bevy 死按钮全分支与救回、bug_locate traceback/
//! 文件名 tsx 捕获怪癖/符号跨行捕获/cap10、空 needle 补 how 怪癖。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::json::Value;
use rxrs::scan;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-scan-test-{}-{}", tag, n));
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

fn get_arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    match v.get(k) {
        Some(Value::Arr(a)) => a,
        other => panic!("{} 应为数组，实得 {:?}", k, other),
    }
}

fn err_of(v: &Value) -> String {
    match v.get("error") {
        Some(Value::Str(s)) => s.clone(),
        other => panic!("应为 error 对象，实得 {:?}", other),
    }
}

#[test]
fn std_placeholder_and_magic_boundaries() {
    let td = TempDir::new("std-basics");
    write_rel(td.path(), "a.py", "# TODO 注释行\nx = 123\ny = -456\nz = 999abc\nw = 12\nv = 007\nu = 1234abc\nt = 123中\ns = \"foo bar\"\nbar = 456\n# set bar = 123\nc = 100\n");
    let out = scan::std_check(td.path().to_str().unwrap(), 100);
    assert!(out.get("error").is_none());
    let findings = get_arr(&out, "findings");
    // "行|rule|msg" 便于整表对比
    let got: Vec<String> = findings
        .iter()
        .map(|f| format!("{}|{}|{}", get_i128(f, "line"), get_str(f, "rule"), get_str(f, "msg")))
        .collect();
    assert_eq!(got, vec![
        "2|magic_number|魔法数字: 123",      // 3 位
        "3|magic_number|魔法数字: -456",     // 负号进捕获组
        "6|magic_number|魔法数字: 007",      // 前导零也算 \d{3,}
        "9|placeholder|占位/假数据文字: foo", // 字符串里的词也命中
        "10|placeholder|占位/假数据文字: bar", // 变量名命中 + 魔数同行
        "10|magic_number|魔法数字: 456",
        "11|magic_number|魔法数字: 123",     // 注释行魔法数照报（无注释豁免）
        "12|magic_number|魔法数字: 100",
    ]);
    // z=999abc / w=12 / u=1234abc / t=123中 都因 \b 落空；注释行 TODO 被豁免
    assert_eq!(get_i128(&out, "files"), 1);
    assert_eq!(get_i128(&out, "total"), got.len() as i128);
}

#[test]
fn std_magic_lang_gate_and_comment_skip() {
    let td = TempDir::new("std-gate");
    // .c 不在魔法数语言门内，但占位词照查
    write_rel(td.path(), "x.c", "int q = 500;\n/* TODO */\nchar* foo_ptr;\n");
    let out = scan::std_check(td.path().to_str().unwrap(), 100);
    let findings = get_arr(&out, "findings");
    assert_eq!(findings.len(), 1, "c 文件只报占位不报魔数: {:?}", findings);
    assert_eq!(get_str(&findings[0], "rule"), "placeholder");
    assert_eq!(get_str(&findings[0], "msg"), "占位/假数据文字: foo");
}

#[test]
fn std_walk_order_and_quota_counts_code_only() {
    let td = TempDir::new("std-walk");
    write_rel(td.path(), "A1.py", "a = 111\n");
    write_rel(td.path(), "a2.py", "b = 222\n");
    write_rel(td.path(), "notes.md", "TODO\n");
    write_rel(td.path(), "sub/s0.py", "c = 333\n");
    write_rel(td.path(), "sub/s1.py", "d = 444\n");
    // 名额 2：根目录两个代码文件烧光，md 不占名额，sub 不再进
    let out = scan::std_check(td.path().to_str().unwrap(), 2);
    assert_eq!(get_i128(&out, "files"), 2);
    let findings = get_arr(&out, "findings");
    assert_eq!(findings.len(), 2);
    assert!(get_str(&findings[0], "file").ends_with("A1.py"));
    assert_eq!(get_str(&findings[0], "msg"), "魔法数字: 111");
    assert!(get_str(&findings[1], "file").ends_with("a2.py"));
    assert_eq!(get_str(&findings[1], "msg"), "魔法数字: 222");
    // 名额 0：一个都不扫
    let out0 = scan::std_check(td.path().to_str().unwrap(), 0);
    assert_eq!(get_i128(&out0, "files"), 0);
    assert_eq!(get_i128(&out0, "total"), 0);
    // 单文件路径：不占名额逻辑，直接收录
    let single = scan::std_check(td.path().join("A1.py").to_str().unwrap(), 100);
    assert_eq!(get_i128(&single, "files"), 1);
    assert_eq!(get_i128(&single, "total"), 1);
}

#[test]
fn std_missing_path_is_error_object() {
    let out = scan::std_check("Z:/rx-scan-test-不存在-8271", 100);
    assert!(err_of(&out).contains("路径不存在"));
}

#[test]
fn ui_godot_button_signal_multiline_dollar() {
    let td = TempDir::new("ui-godot");
    write_rel(td.path(), "ui.gd",
              "extends Button\n\nButton:\nButton: x\nvar q = Button :\nMyButton y:\nButton\n  :\n# Button:\n");
    let out = scan::ui_check(td.path().to_str().unwrap(), 100);
    let issues = get_arr(&out, "issues");
    let lines: Vec<i128> = issues.iter().map(|i| get_i128(i, "line")).collect();
    // 1: extends Button 的 [^:]* 跨行吃到第 3 行冒号（$ 在其后换行前）
    // 4: "Button: x" 的空白串后是 x → 不命中；5/6（无左边界）/7-8（跨行冒号）/9 注释照报
    assert_eq!(lines, vec![1, 5, 6, 7, 9], "godot $ 语义: {:?}", issues);
    for i in issues {
        assert_eq!(get_str(i, "engine"), "godot");
        assert_eq!(get_str(i, "rule"), "ui_pattern");
    }
}

#[test]
fn ui_unity_new_button_no_left_boundary() {
    let td = TempDir::new("ui-unity");
    write_rel(td.path(), "u.cs",
              "Button b = new Button();\nvar x = renew Button( 1 );\nvar y = new\n    Button( 5 );\nvar z = new Button(;\nnew Button(\"a)b\");\n");
    let out = scan::ui_check(td.path().to_str().unwrap(), 100);
    let issues = get_arr(&out, "issues");
    let lines: Vec<i128> = issues.iter().map(|i| get_i128(i, "line")).collect();
    // 2: renew 的 new 也命中（无左边界）；3-4: \s+ 跨行；
    // 5: [^)]* 可跨行——';' 之后跨行吃到 6 号行串内首个 ')'，整段成一次匹配，
    //    6 号行的 new 被吞进同一匹配不再单报
    assert_eq!(lines, vec![1, 2, 3, 5], "unity 语义: {:?}", issues);
    assert_eq!(get_str(&issues[0], "engine"), "unity");
}

#[test]
fn ui_bevy_patterns_and_empty_with_children_only() {
    let td = TempDir::new("ui-bevy-pat");
    write_rel(td.path(), "g.rs",
              "fn ui(mut c: Commands) {\n    c.spawn(NBundle::default()).with_children();\n    c.spawn(NBundle::default()).with_children(|p| {\n        p.spawn(NBundle::default());\n    });\n    c.spawn(TextBundle { ..default() });\n    c.spawn(TextStyle { font: h });\n}\n");
    let out = scan::ui_check(td.path().to_str().unwrap(), 100);
    let issues = get_arr(&out, "issues");
    let msgs: Vec<&str> = issues.iter().map(|i| get_str(i, "msg")).collect();
    assert_eq!(msgs, vec![
        "空 with_children()——无子节点（UI 无效）",
        "旧式 TextBundle——Bevy 0.15+ 推荐 Text::new（API 迁移）",
        "TextStyle 手动构建——Bevy 0.15+ 推荐 TextFont/TextColor 组件",
    ]);
    // 非 .rs/.gd/.cs 不进 ui_check
    let out2 = scan::ui_check(td.path().to_str().unwrap(), 100);
    assert_eq!(get_i128(&out2, "files"), 1);
}

#[test]
fn ui_bevy_dead_button_and_rescues() {
    let td = TempDir::new("ui-dead");
    write_rel(td.path(), "f.rs",
              "fn build(mut c: Commands) {\n    c.spawn((Button, OrphanBtn, N { ..d() }));\n}\nfn gs(mut c: Commands) {\n    c.spawn((Button, GoodBtn, N { ..d() }));\n}\nfn gc(mut q: Query<(&GoodBtn, &Interaction), Changed<Interaction>>) {}\nfn wr(mut c: Commands) {\n    c.spawn((Button, Mark2, N::default()));\n}\nfn wu(q: Query<Entity, With<Mark2>>) {}\nfn lone(mut c: Commands) {\n    c.spawn((\n        Button,\n        LoneMarker,\n        N::default(),\n    ));\n}\nfn paren(mut c: Commands) {\n    c.spawn((\n        Button,\n    ));\n}\nfn cmt(mut c: Commands) {\n    c.spawn((\n        Button,\n        // Marker 行被注释\n        N::default(),\n    ));\n}\n");
    let out = scan::ui_check(td.path().to_str().unwrap(), 100);
    let issues = get_arr(&out, "issues");
    let dead: Vec<&str> = issues
        .iter()
        .filter(|i| get_str(i, "msg").starts_with("死按钮"))
        .map(|i| get_str(i, "msg"))
        .collect();
    // OrphanBtn（同行）与 LoneMarker（独行）死；GoodBtn 被跨 system Query 救回、
    // Mark2 被 With<> 救回、右括号与注释行断扫的不报
    assert_eq!(dead.len(), 2, "死按钮判定: {:?}", dead);
    assert!(dead[0].contains("OrphanBtn"));
    assert!(dead[1].contains("LoneMarker"));
    assert_eq!(get_str(&issues[0], "engine"), "bevy");
}

#[test]
fn loc_traceback_and_context_window() {
    let td = TempDir::new("loc-tb");
    let src = "l1\nl2\nl3\nl4\nbroken_line\nl6\nl7\nl8\n";
    write_rel(td.path(), "src/app.py", src);
    let text = "Traceback (most recent call last):\n  File \"src\\app.py\", line 5, in <module>\n    broken()\nNameError: name 'broken' is not defined";
    let out = scan::bug_locate(td.path().to_str().unwrap(), text);
    let hits = get_arr(&out, "hits");
    assert_eq!(hits.len(), 1);
    let h = &hits[0];
    assert!(get_str(h, "file").ends_with("src\\app.py"));
    assert_eq!(get_i128(h, "line"), 5);
    assert_eq!(get_str(h, "how"), "traceback 精确");
    // _line_ctx(radius=2)：2 前行 + 命中行 + 2 后行
    assert_eq!(get_str(h, "snippet"), "l3\nl4\nbroken_line\nl6\nl7");
}

#[test]
fn loc_filename_fallback_tsx_quirk() {
    let td = TempDir::new("loc-fn");
    write_rel(td.path(), "bar.py", "alpha\nbeta marker\ngamma\ndelta\n");
    // foo.tsx 捕获成 foo.ts（备选 ts 先于 tsx）→ endswith("foo.ts") 落空；
    // bar.py 正常命中，_find_in_file 空 needle 前 3 行、how 只补最后一条
    let out = scan::bug_locate(td.path().to_str().unwrap(), "check foo.tsx and bar.py now");
    let hits = get_arr(&out, "hits");
    assert_eq!(hits.len(), 3);
    assert!(get_str(&hits[0], "file").ends_with("bar.py"));
    assert!(hits[0].get("how").is_none(), "首两条无 how: {:?}", hits[0]);
    assert_eq!(get_str(&hits[2], "how"), "文件名");
    assert_eq!(get_i128(&hits[0], "line"), 1);
    // 空 needle 上下文窗 [idx-2, idx+3)：4 行小文件里 1/2 号行的窗都是全文件
    assert_eq!(get_str(&hits[0], "snippet"), "alpha\nbeta marker\ngamma\ndelta");
    assert_eq!(get_str(&hits[2], "snippet"), "beta marker\ngamma\ndelta");
}

#[test]
fn loc_symbols_cross_line_and_cap() {
    let td = TempDir::new("loc-sym");
    let mut syms = String::new();
    for i in 1..=12 {
        syms.push_str(&format!("use_sym{} = 'sym{}'\n", i, i));
    }
    write_rel(td.path(), "syms.py", &syms);
    // cap10：12 个符号各命中 1 行 → 截到 10
    let text: String = (1..=12).map(|i| format!("Error 'sym{}' ", i)).collect();
    let out = scan::bug_locate(td.path().to_str().unwrap(), &text);
    let hits = get_arr(&out, "hits");
    assert_eq!(hits.len(), 10);
    assert_eq!(get_i128(&out, "candidates"), 10);
    for h in hits {
        assert!(get_str(h, "how").starts_with("符号 'sym"));
    }
    // 跨行闭引号：捕获含 \n 的符号在逐行匹配里落空 → 0 命中
    let out2 = scan::bug_locate(td.path().to_str().unwrap(), "KeyError: 'multi\nline_key'");
    assert_eq!(get_arr(&out2, "hits").len(), 0);
    // 无引号无关键词 → 空
    let out3 = scan::bug_locate(td.path().to_str().unwrap(), "something went wrong");
    assert_eq!(get_i128(&out3, "candidates"), 0);
}

#[test]
fn loc_root_not_dir_is_error() {
    let out = scan::bug_locate("Z:/rx-scan-test-不存在-8271", "File \"a.py\", line 1");
    assert!(err_of(&out).contains("root 不是目录"));
}

#[test]
fn loc_dedupe_same_frame() {
    let td = TempDir::new("loc-dup");
    write_rel(td.path(), "app.py", "a\nb\nc\n");
    let text = "  File \"app.py\", line 2, in f\n  File \"app.py\", line 2, in g";
    let out = scan::bug_locate(td.path().to_str().unwrap(), text);
    let hits = get_arr(&out, "hits");
    assert_eq!(hits.len(), 1, "同文件同行去重: {:?}", hits);
    assert_eq!(get_i128(&hits[0], "line"), 2);
}
