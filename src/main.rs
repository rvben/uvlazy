use std::{env, ffi::OsString, path::PathBuf, process::Command};

#[cfg(unix)]
use std::os::unix::{fs::PermissionsExt, process::CommandExt};

const BOOTSTRAP: &str = include_str!(concat!(env!("OUT_DIR"), "/bootstrap.py"));

fn find_uv() -> Option<PathBuf> {
    env::split_paths(&env::var_os("PATH")?)
        .map(|directory| directory.join("uv"))
        .find(|path| {
            let Ok(metadata) = path.metadata() else {
                return false;
            };
            #[cfg(unix)]
            if metadata.permissions().mode() & 0o111 == 0 {
                return false;
            }
            metadata.is_file()
        })
}

fn launch() -> Result<(), String> {
    let arguments: Vec<OsString> = env::args_os().skip(1).collect();
    if arguments.len() == 1 && (arguments[0] == "--version" || arguments[0] == "-V") {
        println!("uvlazy {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if !cfg!(unix) {
        return Err("this release supports macOS and Linux".into());
    }

    // Interpreter discovery must never sync the current project's dependencies.
    let uv = find_uv().ok_or("uv is required. Install uv and put it on PATH.")?;
    let request = env::var_os("UV_PYTHON").unwrap_or_else(|| OsString::from(">=3.11"));
    let found = Command::new(uv)
        .args(["python", "find", "--system", "--no-project"])
        .arg(request)
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .stderr(std::process::Stdio::inherit())
        .output()
        .map_err(|error| format!("could not run uv: {error}. Install uv and put it on PATH."))?;
    if !found.status.success() {
        return Err(
            "Python 3.11+ is required. Install it with `uv python install 3.11`, or set UV_PYTHON to a compatible interpreter.".into(),
        );
    }
    let python = String::from_utf8(found.stdout)
        .map_err(|_| "uv returned a Python path that is not UTF-8".to_string())?;
    if python.trim().is_empty() {
        return Err("uv returned an empty Python path".into());
    }
    let mut command = Command::new(python.trim());
    command
        .args(["-I", "-c", BOOTSTRAP])
        .args(arguments)
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH");

    // Keep the same PID through the launcher, Python engine, and selected tool.
    // This preserves CI signals, stdin, and the tool's exact exit status.
    #[cfg(unix)]
    return Err(format!("could not start Python: {}", command.exec()));
    #[cfg(not(unix))]
    Err("this release supports macOS and Linux".into())
}

fn main() {
    if let Err(message) = launch() {
        eprintln!("uvlazy: {message}");
        std::process::exit(1);
    }
}
