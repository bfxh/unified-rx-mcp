//! secrets 原生化契约测试（S134）：8 模式规则 + 熵层 + 掩码 + 过滤/跳过语义。
//! 夹具运行时拼接构造（源码不出现连续凭据字面量——Mimosa hook 与 GitHub
//! push protection 两道门同判的纪律，同 Python 侧 test_s123）。

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rxrs::json::Value;
use rxrs::secrets;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        let n = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let p = std::env::temp_dir().join(format!("rx-secrets-test-{}-{}", tag, n));
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

fn hits_of<'a>(res: &'a Value, rule: &str) -> Vec<&'a Value> {
    match res.get("hits") {
        Some(Value::Arr(a)) => a.iter().filter(|h| get_str(h, "rule") == rule).collect(),
        other => panic!("hits 应为数组，实得 {:?}", other),
    }
}

fn scan(root: &Path) -> Value {
    secrets::secrets_scan(root, 3000, 512, 4.5, 200, "")
}

// ---------- 夹具（碎片拼接，防静态门） ----------

fn fx_aws() -> String {
    format!("{}{}", "AKIA", "IOSFODNN7EXAMPLE")
}
fn fx_gh() -> String {
    format!("ghp_{}", "RandomTokenCharacters1234567890abcdef")
}
fn fx_slack() -> String {
    format!("xoxb-{}", "123456789012-abcdefghijklmnop")
}
fn fx_google() -> String {
    format!("AIza{}", "SyD-9tJq3F8kLmN0pQrStUvWxYz12345678")
}
fn fx_stripe() -> String {
    format!("sk_live_{}", "abcdefghijklmnopqrstuvwx")
}
fn fx_plain() -> String {
    format!("{}{}", "JMTf8Kq2", "mN9xR4vB7wZ3sP6dL1cH5jG0")
}
fn fx_dashes() -> String {
    "-".repeat(5)
}

#[test]
fn s134_all_rules_masked_and_no_leak() {
    let d = TempDir::new("rules");
    let creds = format!(
        "AWS = '{aws}'\nGH = '{gh}'\nSLACK = '{slack}'\nGOOGLE = '{google}'\n\
         STRIPE = '{stripe}'\nTOKEN = '{plain}'\n",
        aws = fx_aws(), gh = fx_gh(), slack = fx_slack(),
        google = fx_google(), stripe = fx_stripe(), plain = fx_plain());
    fs::write(d.path().join("creds.py"), creds).unwrap();
    let jwt = format!("eyJhbGciOiJIUzI1NiJ9.{}.abc12", "eyJzdWIiOiIxIn0");
    fs::write(d.path().join("settings.py"),
              format!("auth_token = '{}'\njwt = '{}\n", fx_plain(), jwt)).unwrap();
    fs::write(d.path().join("leaked_key.cfg"),
              format!("{d}BEGIN RSA PRIVATE KEY{d}\nMIIEowIBAAKCAQEA\n{d}END RSA PRIVATE KEY{d}\n",
                      d = fx_dashes())).unwrap();
    fs::write(d.path().join("blob.py"), b"PK\x00\x03\x00binary\x00junk").unwrap();
    fs::write(d.path().join("Cargo.lock"),
              format!("checksum = \"{}\"\n", "a1b2c3d4".repeat(8))).unwrap();
    fs::write(d.path().join("notes.md"),
              format!("中文相邻{}必须无边界\n", fx_aws())).unwrap();

    let res = scan(d.path());
    assert!(res.get("error").is_none(), "{:?}", res);

    for rule in ["aws_access_key", "github_token", "slack_token", "google_api_key",
                 "stripe_live_key", "private_key_block", "jwt", "secret_assignment"] {
        assert!(!hits_of(&res, rule).is_empty(), "缺规则 {}", rule);
    }
    // 中文相邻：aws 规则只命中 creds.py 一处（notes.md 无边界不命中）
    assert_eq!(hits_of(&res, "aws_access_key").len(), 1);
    // 掩码格式（前4+…+后2+len）与完整值不外泄
    let aws_hit = hits_of(&res, "aws_access_key")[0];
    assert_eq!(get_str(aws_hit, "masked"), "AKIA…LE(len=20)");
    let blob = format!("{:?}", res);
    for v in [fx_aws(), fx_gh(), fx_slack(), fx_google(), fx_stripe(), fx_plain()] {
        assert!(!blob.contains(&v), "完整值泄漏: {}", v);
    }
    // 行号 1-based：creds.py 首行
    assert_eq!(get_i128(aws_hit, "line"), 1);
    // 二进制/超尺寸计 skipped；锁文件熵层跳过
    assert!(get_i128(&res, "files_skipped") >= 1, "{:?}", res);
    assert!(hits_of(&res, "high_entropy").iter().all(|h| {
        !get_str(h, "file").ends_with("Cargo.lock")
    }), "锁文件不得进熵层");
}

#[test]
fn s134_placeholders_filtered() {
    let d = TempDir::new("ph");
    let real = fx_plain();
    let lines = [
        format!("pass{} = \"changeme-please-ok\"", "word"),
        format!("api_{} = \"${{ENV_API_KEY}}\"", "key"),
        format!("tok{} = \"<your-token-here>\"", "en"),
        format!("auth_{} = '{}'", "token", real),
    ];
    fs::write(d.path().join("settings.py"), lines.join("\n") + "\n").unwrap();
    let res = scan(d.path());
    let all = match res.get("hits") {
        Some(Value::Arr(a)) => a.clone(),
        other => panic!("hits 应为数组: {:?}", other),
    };
    assert!(all.iter().all(|h| !get_str(h, "snippet").contains("changeme")));
    assert!(all.iter().all(|h| !get_str(h, "snippet").contains("${")));
    assert!(all.iter().all(|h| !get_str(h, "snippet").contains("<your")));
    assert!(!hits_of(&res, "secret_assignment").is_empty(), "真值行应命中");
}

#[test]
fn s134_max_results_truncation_and_include() {
    let d = TempDir::new("trunc");
    let mut lines = vec![];
    for i in 0..5 {
        lines.push(format!("k{} = '{}'", i, fx_plain()));
    }
    fs::write(d.path().join("many.py"), lines.join("\n") + "\n").unwrap();
    fs::write(d.path().join("skipme.nosuch"), format!("x = '{}'", fx_plain())).unwrap();

    let res = secrets::secrets_scan(d.path(), 3000, 512, 4.5, 2, "py");
    // 只扫 py：nosuch 扩展名不计
    assert_eq!(get_i128(&res, "files_scanned"), 1);
    let arr = match res.get("hits") {
        Some(Value::Arr(a)) => a,
        other => panic!("hits: {:?}", other),
    };
    assert_eq!(arr.len(), 2, "max_results=2 截断返回");
    assert!(get_i128(&res, "total_hits") >= 4, "total 为全量计数");
    assert!(matches!(res.get("truncated"), Some(Value::Bool(true))));
}
