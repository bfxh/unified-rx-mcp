//! bug_scan 全量原生化的契约测试（S83）。
//! 直接打 rxrs::bug::bug_scan：Python 迷你 AST 规则（未定义变量/裸 except/
//! 导入遮蔽 asname 契约/动态执行分级）、f-string 区域切片冒号不触发 spec、
//! match 软关键字回退、Rust 生产规则分级与测试区降级（含 `]` 前环视的双层
//! 索引）、通用正则（`re.exec` 排除）、名额语义与路径不存在错误包络。
//! 逐字节对照（7 场景）由 S83 施工期的 oracle 实验承担；此处锁关键语义。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::json::Value;
use rxrs::bug;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-bug-test-{}-{}", tag, n));
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

fn get_i128<'a>(v: &'a Value, k: &str) -> i128 {
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

/// 按 rule 收集 issue（file 后缀过滤可选）。
fn issues_of<'a>(res: &'a Value, rule: &str) -> Vec<&'a Value> {
    get_arr(res, "issues")
        .iter()
        .filter(|i| get_str(i, "rule") == rule)
        .collect()
}

#[test]
fn python_core_rules_and_alias_contract() {
    let d = TempDir::new("core");
    // 导入遮蔽按 asname（oracle 契约 a.asname or a.name）；f-string 里切片
    // 冒号不得触发 format-spec（否则整个文件假报 syntax_error）
    write_rel(
        d.path(),
        "a.py",
        "import math as id\n\
         def f(lines, x):\n\
         \x20   raise ValueError(f\"bad: {lines[-1][:200]}\")\n\
         \x20   return id(x)\n\
         try:\n\
         \x20   pass\n\
         except:\n\
         \x20   pass\n",
    );
    let res = bug::bug_scan(d.path().to_str().unwrap(), 10);
    assert!(res.get("error").is_none(), "不应有错误包络: {:?}", res);
    let red = issues_of(&res, "redefined_import");
    assert_eq!(red.len(), 1, "asname 别名才进遮蔽检查: {:?}", red);
    assert_eq!(get_i128(red[0], "line"), 1);
    assert!(get_str(red[0], "msg").contains("'id'"));
    assert_eq!(issues_of(&res, "syntax_error").len(), 0, "切片冒号不是 spec");
    assert_eq!(issues_of(&res, "bare_except").len(), 1);
    assert_eq!(issues_of(&res, "undefined_name").len(), 0, "参数/别名均算定义");
}

#[test]
fn eval_exec_severity_vocabulary() {
    let d = TempDir::new("dyn");
    write_rel(d.path(), "d.py", "eval('1+1')\ncompile('1', 'f', 'exec')\n");
    let res = bug::bug_scan(d.path().to_str().unwrap(), 5);
    let hits = issues_of(&res, "eval_exec");
    assert_eq!(hits.len(), 2, "{:?}", hits);
    let eval = hits.iter().find(|i| get_i128(i, "line") == 1).unwrap();
    assert_eq!(get_str(eval, "severity"), "high");
    assert_eq!(get_str(eval, "kind"), "definite");
    let comp = hits.iter().find(|i| get_i128(i, "line") == 2).unwrap();
    assert_eq!(get_str(comp, "severity"), "med");
    assert_eq!(get_str(comp, "kind"), "clue");
}

#[test]
fn match_statement_semantics_via_pyast() {
    let d = TempDir::new("match");
    // 捕获（rest/vv/px）是 MatchAs 字符串字段非 Name 节点，不进定义集——
    // 其"使用"按旧 Python 契约报未定义（Point 类模式同理）；只锁语法与该契约。
    write_rel(
        d.path(),
        "m.py",
        concat!(
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
        ),
    );
    let res = bug::bug_scan(d.path().to_str().unwrap(), 5);
    assert_eq!(issues_of(&res, "syntax_error").len(), 0, "match 软关键字不能回退失败");
    let undef = issues_of(&res, "undefined_name");
    assert_eq!(undef.len(), 4, "{:?}", undef);
    let lines: Vec<i128> = undef.iter().map(|i| get_i128(i, "line")).collect();
    assert_eq!(lines, vec![4, 6, 7, 8], "捕获使用/类模式名按行序: {:?}", undef);
}

#[test]
fn rust_grading_and_test_region_downgrade() {
    let d = TempDir::new("rust");
    // 双层索引 [dir.x][ax]：第二个 [ax] 的左字节是 ']'（前环视 (?<=[\w)\])]）
    write_rel(
        d.path(),
        "prod.rs",
        concat!(
            "fn f(grid: &[Vec<f32>], dir: Dir, ax: usize) -> f32 {\n",
            "    let w = grid[dir.x][ax];\n",
            "    let s = dir.y as i32;\n",
            "    let u = maybe().unwrap();\n",
            "    panic!(\"boom\")\n",
            "}\n",
            "#[cfg(test)]\n",
            "mod tests {\n",
            "    #[test]\n",
            "    fn t() { assert_eq!(maybe().unwrap(), 1); }\n",
            "}\n",
        ),
    );
    let res = bug::bug_scan(d.path().to_str().unwrap(), 10);
    let unwraps = issues_of(&res, "unwrap");
    assert_eq!(unwraps.len(), 2, "{:?}", unwraps);
    let prod = unwraps.iter().find(|i| get_i128(i, "line") == 4).unwrap();
    assert_eq!(get_str(prod, "severity"), "info");
    assert_eq!(get_str(prod, "kind"), "clue");
    let test = unwraps.iter().find(|i| get_i128(i, "line") == 10).unwrap();
    assert_eq!(get_str(test, "severity"), "low");
    assert!(get_str(test, "msg").contains("降级"));
    let panics = issues_of(&res, "panic");
    assert_eq!(panics.len(), 1);
    assert_eq!(get_str(panics[0], "severity"), "high");
    let as_cast = issues_of(&res, "as_cast");
    assert_eq!(as_cast.len(), 1, "{:?}", as_cast);
    assert_eq!(get_i128(as_cast[0], "line"), 3);
    let idx = issues_of(&res, "indexing");
    assert!(idx.iter().any(|i| get_i128(i, "line") == 2), "']' 后索引必须命中: {:?}", idx);
}

#[test]
fn rust_test_dir_panic_downgraded() {
    let d = TempDir::new("rtd");
    write_rel(d.path(), "tests/t.rs", "fn t() { panic!(\"expect\") }\n");
    let res = bug::bug_scan(d.path().to_str().unwrap(), 10);
    let panics = issues_of(&res, "panic");
    assert_eq!(panics.len(), 1);
    assert_eq!(get_str(panics[0], "severity"), "low");
    assert!(get_str(panics[0], "msg").contains("测试"));
}

#[test]
fn generic_rules_and_exec_lookbehind() {
    let d = TempDir::new("generic");
    // re.exec( 的左字节是 '.'——(?<![.\w]) 排除成员调用（S44 dsml 教训）
    write_rel(
        d.path(),
        "g.js",
        "assert True\nif (a == 1.5) {}\nconst m = re.exec(\"x\");\neval(\"1\");\n",
    );
    let res = bug::bug_scan(d.path().to_str().unwrap(), 5);
    assert_eq!(issues_of(&res, "assert_always_true").len(), 1);
    assert_eq!(issues_of(&res, "equal_float").len(), 1);
    let ev = issues_of(&res, "eval_exec");
    assert_eq!(ev.len(), 1, "re.exec 不得误报: {:?}", ev);
    assert_eq!(get_i128(ev[0], "line"), 4);
}

#[test]
fn syntax_error_reports_line() {
    let d = TempDir::new("syn");
    write_rel(d.path(), "bad.py", "x = 1\ndef f(:\n");
    let res = bug::bug_scan(d.path().to_str().unwrap(), 5);
    let syn = issues_of(&res, "syntax_error");
    assert_eq!(syn.len(), 1);
    assert_eq!(get_i128(syn[0], "line"), 2);
    assert!(get_str(syn[0], "msg").contains("语法错误"));
}

#[test]
fn quota_and_noncode_files() {
    let d = TempDir::new("quota");
    write_rel(d.path(), "notes.md", "eval(\"x\")\n");
    write_rel(d.path(), "sub/a.py", "v = missing_x\n");
    // 名额 0 ≡ 不扫
    let res = bug::bug_scan(d.path().to_str().unwrap(), 0);
    assert_eq!(get_i128(&res, "files"), 0);
    assert_eq!(get_i128(&res, "total"), 0);
    // 非代码文件不占名额
    let res1 = bug::bug_scan(d.path().to_str().unwrap(), 1);
    assert_eq!(get_i128(&res1, "files"), 1);
    assert_eq!(issues_of(&res1, "undefined_name").len(), 1);
}

#[test]
fn path_not_exist_error_envelope() {
    let res = bug::bug_scan("Z:/rx-bug-test-no-such-dir", 5);
    match res.get("error") {
        Some(Value::Str(s)) => assert!(s.contains("路径不存在"), "{}", s),
        other => panic!("应为错误包络，实得 {:?}", other),
    }
}
