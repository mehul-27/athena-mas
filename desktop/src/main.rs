#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    cell::RefCell,
    env,
    fs::{self, File},
    io::{BufRead, BufReader, Read, Write},
    net::{TcpStream, ToSocketAddrs},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicIsize, AtomicUsize, Ordering},
        mpsc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::WindowEvent;
use tauri::{webview::NewWindowResponse, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};
use tray_icon::{
    menu::{Menu, MenuItem},
    MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent,
};

const PORT: u16 = 41773;
const POLL_SECS: u64 = 60;
const TRAY_GUID: u128 = 0x9f6a3db2_6b52_4d18_a78c_7411d5b60a90;
static TRAY_HWND: AtomicIsize = AtomicIsize::new(0);
static REPORT_ID: AtomicUsize = AtomicUsize::new(0);

thread_local! {
    static SYSTEM_TRAY: RefCell<Option<tray_icon::TrayIcon>> = const { RefCell::new(None) };
}

#[derive(Default)]
struct NotificationState {
    sent: std::collections::BTreeSet<String>,
}

fn record_notification(path: &std::path::Path, state: &mut NotificationState, key: String) {
    use std::io::Write;
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        writeln!(file, "{key}")?;
        file.sync_data()
    })();
    match result {
        Ok(()) => {
            state.sent.insert(key);
        }
        Err(error) => eprintln!("Could not save Athena notification state: {error}"),
    }
}

fn fetch_json(path: &str) -> Result<serde_json::Value, String> {
    let addr = ("127.0.0.1", PORT)
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or("no loopback address")?;
    let mut stream =
        TcpStream::connect_timeout(&addr, Duration::from_secs(2)).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| e.to_string())?;
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nConnection: close\r\n\r\n"
    )
    .map_err(|e| e.to_string())?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|e| e.to_string())?;
    let split = response
        .windows(4)
        .position(|v| v == b"\r\n\r\n")
        .ok_or("invalid local API response")?;
    let headers = std::str::from_utf8(&response[..split]).map_err(|e| e.to_string())?;
    if !headers
        .lines()
        .next()
        .is_some_and(|status| status.contains(" 200 "))
    {
        return Err("local API request failed".into());
    }
    serde_json::from_slice(&response[split + 4..]).map_err(|e| e.to_string())
}

fn send_notification(title: &str, body: &str, log_path: &std::path::Path) -> bool {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::HWND;
        use windows_sys::Win32::UI::Shell::{
            Shell_NotifyIconW, NIF_GUID, NIF_INFO, NIIF_INFO, NIM_MODIFY, NOTIFYICONDATAW,
        };
        let hwnd = TRAY_HWND.load(Ordering::Relaxed) as HWND;
        if hwnd.is_null() {
            log_notification_error(log_path);
            return false;
        }
        let mut notification: NOTIFYICONDATAW = unsafe { std::mem::zeroed() };
        notification.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
        notification.hWnd = hwnd;
        notification.uFlags = NIF_GUID | NIF_INFO;
        notification.guidItem = windows_sys::core::GUID::from_u128(TRAY_GUID);
        notification.dwInfoFlags = NIIF_INFO;
        copy_wide(&mut notification.szInfoTitle, title);
        copy_wide(&mut notification.szInfo, body);
        let shown = unsafe { Shell_NotifyIconW(NIM_MODIFY, &notification) } != 0;
        if !shown {
            log_notification_error(log_path);
        }
        shown
    }
    #[cfg(not(windows))]
    {
        let _ = (title, body);
        log_notification_error(log_path);
        false
    }
}

fn copy_wide<const N: usize>(target: &mut [u16; N], text: &str) {
    for (slot, value) in target
        .iter_mut()
        .zip(text.encode_utf16())
        .take(N.saturating_sub(1))
    {
        *slot = value;
    }
}

fn log_notification_error(path: &std::path::Path) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(
            file,
            "Athena notification: Windows tray balloon could not be displayed."
        );
    }
}

fn poll_notifications(data_dir: PathBuf, stop: std::sync::Arc<AtomicBool>) {
    thread::spawn(move || {
        let state_path = data_dir.join("notification-state.json");
        let mut state = NotificationState {
            sent: fs::read_to_string(&state_path)
                .map(|text| text.lines().map(str::to_owned).collect())
                .unwrap_or_default(),
        };
        while !stop.load(Ordering::Relaxed) {
            if let Ok(result) = fetch_json("/api/personal/reminders?completed=false") {
                for reminder in result["reminders"].as_array().into_iter().flatten() {
                    if reminder["overdue"].as_bool() != Some(true) {
                        continue;
                    }
                    let id = reminder["id"].as_str().unwrap_or_default();
                    let due = reminder["due_at"].as_str().unwrap_or_default();
                    let key = format!("reminder:{id}:{due}");
                    if !id.is_empty()
                        && !state.sent.contains(&key)
                        && send_notification(
                            "Athena reminder",
                            reminder["title"].as_str().unwrap_or("Reminder"),
                            &data_dir.join("notifications.log"),
                        )
                    {
                        record_notification(&state_path, &mut state, key);
                    }
                }
            }
            let mut changes = Vec::new();
            if let Ok(result) = fetch_json("/api/personal/watchlists") {
                for watchlist in result["watchlists"].as_array().into_iter().flatten() {
                    for change in watchlist["recent_changes"].as_array().into_iter().flatten() {
                        let key = format!("change:{}", change["id"].as_str().unwrap_or_default());
                        if !state.sent.contains(&key) && key != "change:" {
                            changes.push((
                                key,
                                change["title"]
                                    .as_str()
                                    .unwrap_or("Watchlist update")
                                    .to_owned(),
                            ));
                        }
                    }
                }
            }
            // Send a few at a time so a long-offline app does not flood Windows notifications.
            for (key, title) in changes.into_iter().take(5) {
                if send_notification(
                    "Athena watchlist update",
                    &title,
                    &data_dir.join("notifications.log"),
                ) {
                    record_notification(&state_path, &mut state, key);
                }
            }
            for _ in 0..POLL_SECS {
                if stop.load(Ordering::Relaxed) {
                    break;
                }
                thread::sleep(Duration::from_secs(1));
            }
        }
    });
}

#[cfg(windows)]
fn contain_process_tree(child: &Child) -> std::io::Result<std::os::windows::io::OwnedHandle> {
    use std::{
        mem,
        os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
        ptr,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    // The job's owned handle closes if the app crashes or is terminated.
    unsafe {
        let raw = CreateJobObjectW(ptr::null(), ptr::null());
        if raw.is_null() {
            return Err(std::io::Error::last_os_error());
        }
        let job = OwnedHandle::from_raw_handle(raw);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            raw,
            JobObjectExtendedLimitInformation,
            ptr::from_ref(&limits).cast(),
            mem::size_of_val(&limits) as u32,
        ) == 0
            || AssignProcessToJobObject(raw, child.as_raw_handle()) == 0
        {
            return Err(std::io::Error::last_os_error());
        }
        Ok(job)
    }
}

#[derive(Default)]
struct Backend {
    child: Option<Child>,
    #[cfg(windows)]
    job: Option<std::os::windows::io::OwnedHandle>,
    exiting: bool,
    notification_stop: Option<std::sync::Arc<AtomicBool>>,
}

impl Backend {
    fn stop(&mut self) {
        #[cfg(windows)]
        let _job = self.job.take();
        if let Some(mut child) = self.child.take() {
            // EOF gives the API time to finish its lifespan shutdown.
            drop(child.stdin.take());
            let deadline = Instant::now() + Duration::from_secs(12);
            while Instant::now() < deadline {
                match child.try_wait() {
                    Ok(Some(_)) => return,
                    Ok(None) => thread::sleep(Duration::from_millis(50)),
                    Err(_) => break,
                }
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn repository() -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os("ATHENA_DESKTOP_ROOT") {
        let path = PathBuf::from(configured);
        return if path.join("scripts/athena_desktop.py").is_file() {
            path.canonicalize().map_err(|err| err.to_string())
        } else {
            Err("ATHENA_DESKTOP_ROOT does not point to an Athena checkout.".into())
        };
    }
    let executable = env::current_exe().map_err(|err| err.to_string())?;
    executable
        .ancestors()
        .find(|path| path.join("scripts/athena_desktop.py").is_file())
        .map(PathBuf::from)
        .ok_or_else(|| "Keep Athena.exe in the project folder or set ATHENA_DESKTOP_ROOT.".into())
}

fn start_backend(app: &tauri::AppHandle, repo: PathBuf) -> Result<(), String> {
    let python = repo.join(".venv/Scripts/python.exe");
    if !python.is_file() {
        return Err(
            "Athena needs the project's Python environment (.venv). See the README for setup."
                .into(),
        );
    }
    let log_dir = repo.join("data/desktop");
    fs::create_dir_all(&log_dir).map_err(|err| err.to_string())?;
    let log_file = log_dir.join("backend.log");
    let mut command = Command::new(python);
    command
        .args(["-u", "scripts/athena_desktop.py", "--data-dir"])
        .arg(repo.join("data"))
        .args(["--port", &PORT.to_string()])
        .current_dir(&repo)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(File::create(&log_file).map_err(|err| err.to_string())?);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }

    let state = app.state::<Mutex<Backend>>();
    let output = {
        let mut state = state.lock().expect("backend lock");
        if state.exiting {
            return Err("Athena is closing.".into());
        }
        let mut child = command.spawn().map_err(|err| err.to_string())?;
        #[cfg(windows)]
        {
            state.job = Some(match contain_process_tree(&child) {
                Ok(job) => job,
                Err(error) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!(
                        "Could not supervise Athena's Python process: {error}"
                    ));
                }
            });
        }
        let output = child.stdout.take().expect("backend stdout pipe");
        state.child = Some(child);
        output
    };

    let (ready, notification) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let marker = format!("ATHENA_DESKTOP_PORT={PORT}");
        let mut ready = Some(ready);
        for line in BufReader::new(output).lines().map_while(Result::ok) {
            if line.trim() == marker {
                if let Some(sender) = ready.take() {
                    let _ = sender.send(());
                }
            }
        }
    });
    notification
        .recv_timeout(Duration::from_secs(35))
        .map_err(|_| {
            state.lock().expect("backend lock").stop();
            format!(
                "Athena could not start. Another copy may be running. See {}.",
                log_file.display()
            )
        })
}

fn local_backend(url: &Url) -> bool {
    url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(PORT)
}

fn report_url(url: &Url) -> bool {
    local_backend(url)
        && matches!(
            url.path().split('/').collect::<Vec<_>>().as_slice(),
            ["", "api", "runs" | "replay", _, "report"]
        )
}

fn open_source(url: &Url) {
    if matches!(url.scheme(), "http" | "https")
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && !local_backend(url)
    {
        let _ = open::that_detached(url.as_str());
    }
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Mutex::new(Backend::default()))
        .setup(|app| {
            let repo = repository().map_err(std::io::Error::other)?;
            let webview_data = repo.join("data/desktop/webview");
            let handle = app.handle().clone();
            let report_handle = handle.clone();
            let report_data = webview_data.clone();
            let show_item = MenuItem::with_id("open", "Open Athena", true, None);
            let quit_item = MenuItem::with_id("quit", "Quit Athena", true, None);
            let menu = Menu::with_items(&[&show_item, &quit_item])?;
            let icon = tray_icon::Icon::from_path(repo.join("desktop/icons/icon.ico"), None)
                .map_err(std::io::Error::other)?;
            let tray = TrayIconBuilder::new()
                .with_id("athena-tray")
                .with_guid(TRAY_GUID)
                .with_icon(icon)
                .with_tooltip("Athena")
                .with_menu(Box::new(menu))
                .with_menu_on_left_click(false)
                .build()?;
            #[cfg(windows)]
            TRAY_HWND.store(tray.window_handle() as isize, Ordering::Relaxed);
            let tray_app = handle.clone();
            TrayIconEvent::set_event_handler(Some(move |event| {
                if let TrayIconEvent::Click {
                    id,
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                } = event
                {
                    if id.as_ref() == "athena-tray" {
                        let app = tray_app.clone();
                        let app_for_task = app.clone();
                        let _ = app.run_on_main_thread(move || show_main_window(&app_for_task));
                    }
                }
            }));
            SYSTEM_TRAY.with(|slot| *slot.borrow_mut() = Some(tray));
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Athena")
                .inner_size(1180.0, 790.0)
                .min_inner_size(640.0, 460.0)
                .background_color(tauri::window::Color(17, 17, 17, 255))
                .data_directory(webview_data)
                .enable_clipboard_access()
                .disable_drag_drop_handler()
                .initialization_script(format!(
                    "if (location.origin === 'http://127.0.0.1:{PORT}') window.ATHENA_DESKTOP = true;"
                ))
                .on_navigation(|url| {
                    local_backend(url) || url.as_str() == "about:blank"
                        || (url.host_str() == Some("tauri.localhost") && url.scheme() == "http")
                        || (url.host_str() == Some("localhost") && url.scheme() == "tauri")
                })
                .on_new_window(move |url, features| {
                    if !report_url(&url) {
                        open_source(&url);
                        return NewWindowResponse::Deny;
                    }
                    let label = format!("report-{}", REPORT_ID.fetch_add(1, Ordering::Relaxed));
                    let report = WebviewWindowBuilder::new(
                        &report_handle,
                        label,
                        WebviewUrl::External("about:blank".parse().expect("blank URL")),
                    )
                    .window_features(features)
                    .data_directory(report_data.clone())
                    .title("Athena Research Report")
                    .inner_size(960.0, 760.0)
                    .on_navigation(|url| report_url(url) || url.as_str() == "about:blank")
                    .on_new_window(|url, _| {
                        open_source(&url);
                        NewWindowResponse::Deny
                    })
                    .build();
                    match report {
                        Ok(window) => NewWindowResponse::Create { window },
                        Err(err) => {
                            eprintln!("Could not open report: {err}");
                            NewWindowResponse::Deny
                        }
                    }
                })
                .build()?;
            let notification_data = repo.join("data/desktop");
            let notification_stop = std::sync::Arc::new(AtomicBool::new(false));
            app.state::<Mutex<Backend>>().lock().expect("backend lock").notification_stop = Some(notification_stop.clone());
            thread::spawn(move || match start_backend(&handle, repo) {
                Ok(()) => {
                    poll_notifications(notification_data, notification_stop);
                    let url = format!("http://127.0.0.1:{PORT}/").parse().expect("loopback URL");
                    let _ = window.navigate(url);
                }
                Err(message) => {
                    let message = serde_json::to_string(&message).expect("serialize startup error");
                    let _ = window.eval(format!(
                        "(() => {{ const show = () => {{ document.getElementById('status').textContent = {message}; }}; if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show, {{ once: true }}); else show(); }})()"
                    ));
                }
            });
            Ok(())
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => show_main_window(app),
            "quit" => app.exit(0),
            _ => (),
        })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    if !window.app_handle().state::<Mutex<Backend>>().lock().expect("backend lock").exiting {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("start Athena desktop app");

    app.run(|app, event| {
        if matches!(event, RunEvent::Exit) {
            let state = app.state::<Mutex<Backend>>();
            let mut state = state.lock().expect("backend lock");
            state.exiting = true;
            if let Some(stop) = &state.notification_stop {
                stop.store(true, Ordering::Relaxed);
            }
            state.stop();
        }
    });
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}
