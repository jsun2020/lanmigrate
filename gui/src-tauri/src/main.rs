// LanMigrate GUI shell: spawns the Python sidecar (`lanmigrate ipc`) and
// pipes JSON lines between it and the webview. All migration logic lives in
// the sidecar; this binary only does process plumbing and the folder dialog.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;

use tauri::{Emitter, Manager, RunEvent, State};

struct Backend {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
}

#[tauri::command]
fn ipc_send(line: String, backend: State<Backend>) -> Result<(), String> {
    let mut guard = backend.stdin.lock().unwrap();
    match guard.as_mut() {
        Some(stdin) => writeln!(stdin, "{line}").map_err(|e| e.to_string()),
        None => Err("backend not running".into()),
    }
}

fn backend_command() -> Command {
    if cfg!(debug_assertions) {
        // dev: run the Python package straight from the repo checkout
        let repo = concat!(env!("CARGO_MANIFEST_DIR"), "\\..\\..");
        let python = format!("{repo}\\.venv\\Scripts\\python.exe");
        let mut cmd = Command::new(python);
        cmd.args(["-m", "lanmigrate", "ipc"]).current_dir(repo);
        cmd
    } else {
        // release: the PyInstaller sidecar is bundled next to this binary
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_path_buf()))
            .unwrap_or_default();
        let mut cmd = Command::new(exe_dir.join("lanmigrate.exe"));
        cmd.arg("ipc");
        cmd
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let mut cmd = backend_command();
            cmd.stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
            }
            let mut child = cmd.spawn()?;
            let stdout = child.stdout.take().expect("piped stdout");
            let stderr = child.stderr.take().expect("piped stderr");
            let stdin = child.stdin.take();
            app.manage(Backend {
                stdin: Mutex::new(stdin),
                child: Mutex::new(Some(child)),
            });

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                    let _ = handle.emit("ipc", line);
                }
                let _ = handle.emit("ipc-closed", ());
            });
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    eprintln!("[backend] {line}");
                    let _ = handle.emit("ipc-stderr", line);
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![ipc_send])
        .build(tauri::generate_context!())
        .expect("error while building LanMigrate")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let backend: State<Backend> = app.state();
                // dropping stdin sends EOF: the sidecar stops its rclone
                // children (interrupt-safe by design) and exits
                backend.stdin.lock().unwrap().take();
                let child_opt = backend.child.lock().unwrap().take();
                if let Some(mut child) = child_opt {
                    let deadline =
                        std::time::Instant::now() + std::time::Duration::from_secs(3);
                    loop {
                        match child.try_wait() {
                            Ok(Some(_)) => break,
                            Ok(None) if std::time::Instant::now() < deadline => {
                                std::thread::sleep(std::time::Duration::from_millis(100))
                            }
                            _ => {
                                let _ = child.kill();
                                break;
                            }
                        }
                    }
                }
            }
        });
}
