use std::{env, fmt::Write, fs, path::PathBuf};

// Embed the stdlib-only Python engine without requiring Python at build time.
// Python's own zipfile module materializes it when the executable first runs.
fn python_string(value: &str) -> String {
    let mut result = String::from("'");
    for character in value.chars() {
        match character {
            '\\' => result.push_str("\\\\"),
            '\'' => result.push_str("\\'"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            c if c.is_control() => write!(result, "\\U{:08x}", c as u32).unwrap(),
            c => result.push(c),
        }
    }
    result.push('\'');
    result
}

fn main() {
    println!("cargo:rerun-if-changed=src/uvlazy");
    println!("cargo:rerun-if-changed=scripts/launch.py");
    println!("cargo:rerun-if-changed=Cargo.toml");
    let mut paths: Vec<_> = fs::read_dir("src/uvlazy")
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "py"))
        .collect();
    paths.sort();
    let mut bootstrap = String::from("files = {\n");
    for path in paths {
        let name = format!("uvlazy/{}", path.file_name().unwrap().to_str().unwrap());
        let source = fs::read_to_string(&path).unwrap();
        writeln!(
            bootstrap,
            "    {}: {},",
            python_string(&name),
            python_string(&source)
        )
        .unwrap();
    }
    writeln!(
        bootstrap,
        "    'uvlazy/_version': {},\n}}",
        python_string(&env::var("CARGO_PKG_VERSION").unwrap())
    )
    .unwrap();
    bootstrap.push_str(&fs::read_to_string("scripts/launch.py").unwrap());
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    fs::write(output.join("bootstrap.py"), bootstrap).unwrap();
}
