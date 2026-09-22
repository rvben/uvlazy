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

fn delegates_to_uv(arguments: &[OsString]) -> bool {
    let Some(command) = arguments.first() else {
        return false;
    };
    if command != "run" {
        return command != "--help" && command != "-h" && command != "--version" && command != "-V";
    }

    let mut index = 1;
    while index < arguments.len() {
        let argument = arguments[index].to_string_lossy();
        if matches!(argument.as_ref(), "--help" | "-h") {
            return false;
        }
        if matches!(
            argument.as_ref(),
            "--project" | "--from" | "--group" | "--with" | "--extra-index-url"
        ) {
            index += 2;
            continue;
        }
        if argument.starts_with("--project=")
            || argument.starts_with("--from=")
            || argument.starts_with("--group=")
            || argument.starts_with("--with=")
            || argument.starts_with("--extra-index-url=")
            || matches!(
                argument.as_ref(),
                "-q" | "--quiet" | "-m" | "--module" | "--locked"
            )
            || (argument.starts_with('-')
                && argument.len() > 1
                && argument[1..]
                    .chars()
                    .all(|character| matches!(character, 'q' | 'm')))
        {
            index += 1;
            continue;
        }
        // Everything after uvlazy's target belongs to the target. An option
        // uvlazy does not own before the target is an uv-native run request.
        return argument.starts_with('-');
    }
    false
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
    if delegates_to_uv(&arguments) {
        let mut command = Command::new(&uv);
        command.args(&arguments);
        #[cfg(unix)]
        return Err(format!("could not start uv: {}", command.exec()));
        #[cfg(not(unix))]
        return Err("this release supports macOS and Linux".into());
    }
    let request = env::var_os("UV_PYTHON").unwrap_or_else(|| OsString::from(">=3.11"));
    let found = Command::new(&uv)
        .args(["python", "find", "--system", "--no-project"])
        .arg(request)
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .stderr(std::process::Stdio::inherit())
        .output()
        .map_err(|error| format!("could not run uv: {error}. Install uv and put it on PATH."))?;
    if !found.status.success() {
        return Err(format!(
            "Python discovery failed: {} python find ({}); see its diagnostic above.",
            uv.display(),
            found.status
        ));
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
