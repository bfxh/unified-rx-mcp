//! 零依赖 JSON 的单元测试：往返、敌意输入、深度限制、id 保真。

use rxrs::json::{parse, Value};

#[test]
fn roundtrip_basic() {
    let src = r#"{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"x","n":-1.5,"arr":[1,"two",null,true],"s":"中文\n\"引\"⚠️"}}"#;
    let v = parse(src).unwrap();
    let out = v.to_json();
    let v2 = parse(&out).unwrap();
    assert_eq!(v, v2);
    assert_eq!(v2.get("id"), Some(&Value::Int(7)));
}

#[test]
fn escapes_and_unicode() {
    let v = parse(r#""A😀é\n\t\\\/\b\f\r""#).unwrap();
    match v {
        Value::Str(s) => {
            assert!(s.contains('😀'));
            assert!(s.contains('\n'));
        }
        _ => panic!("应为字符串"),
    }
    // 孤代理落 U+FFFD 而不是报错/崩溃
    match parse(r#""\ud800""#).unwrap() {
        Value::Str(s) => assert_eq!(s, "\u{FFFD}"),
        _ => panic!(),
    }
    // 配对代理
    match parse(r#""😀""#).unwrap() {
        Value::Str(s) => assert_eq!(s, "😀"),
        _ => panic!(),
    }
}

#[test]
fn malformed_inputs_rejected_cleanly() {
    let bad = [
        "{", "}", "[", "{\"a\":", "{\"a\":}", "{\"a\" 1}", "{'a':1}", "", "   ",
        "01", "+1", "1.", ".5", "1e", "1e+", "-x", "NaN", "Infinity", "-Infinity",
        "\"未闭合", "\"\\q\"", "\"\\u12\"", "tru", "{\"a\":1,}", "[1,]", "[,]",
        "{\"a\":1}{\"b\":2}", "1 2", "\"a\" \"b\"",
    ];
    for b in bad {
        assert!(parse(b).is_err(), "应拒绝: {:?}", b);
    }
}

#[test]
fn deep_nesting_rejected_not_overflow() {
    for depth in [512usize, 513, 10_000] {
        let s = format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        if depth > 512 {
            assert!(parse(&s).is_err(), "深度 {} 应被限深拒绝", depth);
        } else {
            assert!(parse(&s).is_ok(), "深度 {} 应可解析", depth);
        }
    }
}

#[test]
fn number_kinds() {
    assert_eq!(parse("0").unwrap(), Value::Int(0));
    assert_eq!(parse("-12").unwrap(), Value::Int(-12));
    assert!(matches!(parse("9007199254740993").unwrap(), Value::Int(_))); // 2^53+1 精确保真
    assert!(matches!(parse("1.0").unwrap(), Value::Float(_)));
    assert!(matches!(parse("1e300").unwrap(), Value::Float(_)));
    // 2^70：JSON-RPC id 保真（i64 放不下，i128 精确；f64 会丢精度回显错 id）
    assert_eq!(parse("1180591620717411303424").unwrap(), Value::Int(2i128.pow(70)));
    // i128 溢出才落 Float
    assert!(matches!(
        parse("170141183460469231731687303715884105728").unwrap(),
        Value::Float(_)
    ));
}

#[test]
fn serialize_escapes_control_chars() {
    let v = Value::Str("a\u{01}b\"c\\d\n".into());
    let s = v.to_json();
    assert!(s.contains("\\u0001"));
    assert!(s.contains("\\\""));
    assert!(parse(&s).unwrap() == v);
}
