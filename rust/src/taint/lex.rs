//! taint 子模块（S168 从 taint.rs 拆出；纯搬移，未改语义）。
use super::*;

/// 词法器：字符流 → 令牌流。缩进只在括号深度 0 时生效；反斜杠续行跳过；
/// f-string 的 {expr} 内层发普通令牌（污点可见），:spec / !conv 跳过。
pub(crate) fn lex(src: &str) -> Vec<Tok> {
    let cs: Vec<char> = src.chars().collect();
    let mut out: Vec<Tok> = Vec::new();
    let mut i = 0usize;
    let mut line = 1usize;
    let mut depth = 0usize;
    let mut indents: Vec<usize> = vec![0];
    let mut at_bol = true;

    while i < cs.len() {
        if at_bol && depth == 0 {
            // 行首：量缩进；空行/纯注释行不参与缩进逻辑
            let mut j = i;
            let mut col = 0usize;
            while j < cs.len() && (cs[j] == ' ' || cs[j] == '\t') {
                col += if cs[j] == '\t' { 8 - col % 8 } else { 1 };
                j += 1;
            }
            if j >= cs.len() {
                break;
            }
            if cs[j] == '\n' || cs[j] == '\r' || cs[j] == '#' {
                i = j;
                at_bol = false;
                continue; // 空行/注释行：主分发处理
            }
            let top = *indents.last().unwrap();
            if col > top {
                indents.push(col);
                out.push(Tok { tk: Tk::Indent, line });
            } else {
                while *indents.last().unwrap() > col {
                    indents.pop();
                    out.push(Tok { tk: Tk::Dedent, line });
                }
            }
            i = j;
            at_bol = false;
            continue;
        }

        let c = cs[i];
        match c {
            '\r' => i += 1,
            '\n' => {
                line += 1;
                i += 1;
                if depth == 0 {
                    out.push(Tok { tk: Tk::Newline, line });
                    at_bol = true;
                }
            }
            '\\' if i + 1 < cs.len() && cs[i + 1] == '\n' => {
                i += 2;
                line += 1;
            }
            ' ' | '\t' => i += 1,
            '#' => {
                while i < cs.len() && cs[i] != '\n' {
                    i += 1;
                }
            }
            '\'' | '"' => {
                let (ni, nl) = lex_string(&cs, i, &mut out, line, false, false);
                i = ni;
                line = nl;
            }
            c if c.is_ascii_alphabetic() || c == '_' => {
                let start = i;
                while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                    i += 1;
                }
                let word: String = cs[start..i].iter().collect();
                // 字符串前缀：r/b/u/f 的非空组合且紧跟引号
                if i < cs.len()
                    && (cs[i] == '\'' || cs[i] == '"')
                    && !word.is_empty()
                    && word.chars().all(|ch| matches!(ch, 'r'|'R'|'b'|'B'|'u'|'U'|'f'|'F'))
                {
                    let is_raw = word.contains('r') || word.contains('R');
                    let is_f = word.contains('f') || word.contains('F');
                    let (ni, nl) = lex_string(&cs, i, &mut out, line, is_raw, is_f);
                    i = ni;
                    line = nl;
                } else {
                    out.push(Tok { tk: Tk::Id(word), line });
                }
            }
            c if c.is_ascii_digit() => {
                while i < cs.len()
                    && (cs[i].is_ascii_alphanumeric() || cs[i] == '.' || cs[i] == '_')
                {
                    i += 1;
                }
                out.push(Tok { tk: Tk::Num, line });
            }
            _ => {
                let three: String = cs[i..(i + 3).min(cs.len())].iter().collect();
                let two: String = cs[i..(i + 2).min(cs.len())].iter().collect();
                if cs.len() - i >= 3 && OP3.contains(&three.as_str()) {
                    out.push(Tok { tk: Tk::Op(three), line });
                    i += 3;
                } else if cs.len() - i >= 2 && OP2.contains(&two.as_str()) {
                    out.push(Tok { tk: Tk::Op(two), line });
                    i += 2;
                } else {
                    if is_opener(&c.to_string()) {
                        depth += 1;
                    } else if is_closer(&c.to_string()) && depth > 0 {
                        depth -= 1;
                    }
                    out.push(Tok { tk: Tk::Op(c.to_string()), line });
                    i += 1;
                }
            }
        }
    }
    while indents.len() > 1 {
        indents.pop();
        out.push(Tok { tk: Tk::Dedent, line });
    }
    out
}

/// 字符串字面量消费（含三引号/raw/f-string 插值）。返回新索引。
/// f-string：{expr} 内层发 Id/Num/Op 令牌（行号取当前行）；:spec 进入后跳过到配对 }。
/// 返回 (消耗后的索引, 结束行号)。行号必须回传主循环——多行字符串（模块
/// docstring 等）内部换行不计入会让后续所有行号漂移（S73 重放首测实锤）。
pub(crate) fn lex_string(cs: &[char], mut i: usize, out: &mut Vec<Tok>, line: usize,
              is_raw: bool, is_f: bool) -> (usize, usize) {
    let quote = cs[i];
    let triple = i + 2 < cs.len() && cs[i + 1] == quote && cs[i + 2] == quote;
    i += if triple { 3 } else { 1 };
    let mut cur = line;
    let mut brace = 0usize; // f-string 插值深度
    let mut spec = false;   // 进了格式说明符
    loop {
        if i >= cs.len() {
            break;
        }
        let c = cs[i];
        if c == '\n' {
            cur += 1;
            i += 1;
            if !triple {
                break; // 单引号串未闭合到行尾：容忍
            }
            continue;
        }
        if c == quote {
            if triple {
                if i + 2 < cs.len() && cs[i + 1] == quote && cs[i + 2] == quote {
                    i += 3;
                    break;
                }
                i += 1;
                continue;
            }
            i += 1;
            break;
        }
        if c == '\\' && i + 1 < cs.len() {
            if cs[i + 1] == '\n' {
                cur += 1;
                i += 2;
                continue;
            }
            if !is_raw {
                i += 2;
                continue;
            }
            // raw 串：反斜杠保留在值里，但仍阻止紧随的引号终止字符串
            // （Python 语义 r"...\"..." 不在此收口；漏掉会把正则里的 \" 当
            // 终止符，整文件词法失配——S73 重放 appaudit.py 零命中实锤）
            if cs[i + 1] == quote {
                i += 2;
                continue;
            }
            i += 1;
            continue;
        }
        if is_f {
            if c == '{' {
                brace += 1;
                spec = false;
                i += 1;
                continue;
            }
            if c == '}' {
                if brace > 0 {
                    brace -= 1;
                    if brace == 0 {
                        spec = false;
                    }
                }
                i += 1;
                continue;
            }
            if brace > 0 {
                if spec {
                    i += 1; // 格式说明符内容不参与污点
                    continue;
                }
                if c == ':' && brace == 1 {
                    spec = true;
                    i += 1;
                    continue;
                }
                if c == '!' && i + 1 < cs.len() && cs[i + 1].is_ascii_alphabetic() {
                    i += 2; // !r/!s/!a 转换
                    continue;
                }
                if c.is_ascii_alphabetic() || c == '_' {
                    let st = i;
                    while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                        i += 1;
                    }
                    out.push(Tok { tk: Tk::Id(cs[st..i].iter().collect()), line: cur });
                    continue;
                }
                if c.is_ascii_digit() {
                    out.push(Tok { tk: Tk::Num, line: cur });
                    i += 1;
                    continue;
                }
                if !c.is_whitespace() {
                    out.push(Tok { tk: Tk::Op(c.to_string()), line: cur });
                }
                i += 1;
                continue;
            }
        }
        i += 1;
    }
    out.push(Tok { tk: Tk::Str, line: cur });
    (i, cur)
}

// ---------------------------------------------------------------- 分析器

pub(crate) const SANITIZERS: [&str; 5] = ["basename", "secure_filename", "int", "float", "_fs_resolve"];
pub(crate) const SOURCE_DOTTED: [&str; 4] = ["sys.argv", "os.environ", "sys.stdin", "sys.stdin.buffer"];
pub(crate) const SOURCE_CALLS: [&str; 3] = ["input", "os.getenv", "raw_input"];
pub(crate) const EXEC_SINKS: [&str; 10] = [
    "eval", "exec", "compile", "os.system", "os.popen", "pickle.loads", "pickle.load",
    "yaml.load", "yaml.unsafe_load", "marshal.loads",
];
pub(crate) const PATH_SINKS: [&str; 26] = [
    "open", "io.open", "os.remove", "os.unlink", "os.rename", "os.renames", "os.makedirs",
    "os.mkdir", "os.rmdir", "os.truncate", "os.chmod", "os.chown", "os.stat", "os.lstat",
    "os.listdir", "os.scandir", "os.walk", "shutil.copy", "shutil.copy2", "shutil.copyfile",
    "shutil.copytree", "shutil.move", "shutil.rmtree", "shutil.make_archive",
    "tarfile.open", "zipfile.ZipFile",
];
pub(crate) const METHOD_SINKS: [&str; 14] = [
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "unlink", "mkdir",
    "rmdir", "rename", "replace", "touch", "glob", "rglob", "iterdir",
];
pub(crate) const AUG_OPS: [&str; 12] = ["+=", "-=", "*=", "/=", "//=", "**=", "%=", "&=", "|=", "^=", ">>=", "<<="];
pub(crate) const KEYWORDS: [&str; 30] = [
    "def", "class", "return", "if", "else", "elif", "for", "while", "with", "as", "import",
    "from", "lambda", "try", "except", "finally", "raise", "in", "not", "and", "or", "is",
    "None", "True", "False", "pass", "break", "continue", "global", "yield",
];

