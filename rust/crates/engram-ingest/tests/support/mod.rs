//! Minimal one-shot HTTP server for sink tests.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

/// Start a one-request server. Returns its base URL and a shared request log.
#[allow(dead_code)]
pub fn one_shot(status: u16, body: &'static str) -> (String, Arc<Mutex<Vec<String>>>) {
    one_shot_delayed(status, body, Duration::ZERO)
}

/// Like [`one_shot`], but waits before responding (for timeout tests).
#[allow(dead_code)]
pub fn one_shot_delayed(
    status: u16,
    body: &'static str,
    delay: Duration,
) -> (String, Arc<Mutex<Vec<String>>>) {
    one_shot_with_location(status, body, delay, None)
}

/// One-shot server that sends a `Location` header (redirect regression tests).
#[allow(dead_code)] // not every test binary uses the redirect helper
pub fn one_shot_redirect(status: u16, location: &'static str) -> (String, Arc<Mutex<Vec<String>>>) {
    one_shot_with_location(status, "", Duration::ZERO, Some(location))
}

fn one_shot_with_location(
    status: u16,
    body: &'static str,
    delay: Duration,
    location: Option<&'static str>,
) -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock server");
    let address = listener.local_addr().expect("mock address");
    let log = Arc::new(Mutex::new(Vec::new()));
    let shared = Arc::clone(&log);
    thread::spawn(move || {
        let Ok((mut stream, _)) = listener.accept() else {
            return;
        };
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .expect("read timeout");
        let mut buffer = Vec::new();
        let mut chunk = [0u8; 4096];
        while let Ok(read) = stream.read(&mut chunk) {
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            if let Some(header_end) = find_headers_end(&buffer) {
                let headers = String::from_utf8_lossy(&buffer[..header_end]).to_string();
                let length = content_length(&headers);
                if buffer.len() >= header_end + length {
                    break;
                }
            }
        }
        shared
            .lock()
            .expect("request log")
            .push(String::from_utf8_lossy(&buffer).to_string());
        if !delay.is_zero() {
            thread::sleep(delay);
        }
        let location = location
            .map(|value| format!("Location: {value}\r\n"))
            .unwrap_or_default();
        let response = format!(
            "HTTP/1.1 {status} X\r\nContent-Length: {}\r\nContent-Type: application/json\r\n{location}Connection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes());
    });
    (format!("http://{address}"), log)
}

fn find_headers_end(buffer: &[u8]) -> Option<usize> {
    buffer
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|position| position + 4)
}

fn content_length(headers: &str) -> usize {
    headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            if name.eq_ignore_ascii_case("content-length") {
                value.trim().parse().ok()
            } else {
                None
            }
        })
        .unwrap_or(0)
}

/// Multi-request server for worker tests. Tracks in-flight requests per Hindsight
/// bank so a test can assert the per-bank concurrency bound.
#[allow(dead_code)]
pub struct TestServer {
    url: String,
    requests: Arc<Mutex<Vec<String>>>,
    max_in_flight: Arc<Mutex<std::collections::BTreeMap<String, usize>>>,
    stop: Arc<std::sync::atomic::AtomicBool>,
    handle: Option<thread::JoinHandle<()>>,
}

#[allow(dead_code)]
impl TestServer {
    pub fn start(status: u16, delay: Duration) -> Self {
        Self::start_with_hook(status, delay, None)
    }

    pub fn start_with_hook(
        status: u16,
        delay: Duration,
        hook: Option<Arc<dyn Fn() + Send + Sync>>,
    ) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind worker server");
        listener
            .set_nonblocking(true)
            .expect("nonblocking worker server");
        let address = listener.local_addr().expect("worker server address");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let max_in_flight = Arc::new(Mutex::new(std::collections::BTreeMap::new()));
        let in_flight = Arc::new(Mutex::new(std::collections::BTreeMap::new()));
        let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));

        let shared_requests = Arc::clone(&requests);
        let shared_max = Arc::clone(&max_in_flight);
        let shared_in_flight = Arc::clone(&in_flight);
        let shared_stop = Arc::clone(&stop);
        let handle = thread::spawn(move || {
            while !shared_stop.load(std::sync::atomic::Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let requests = Arc::clone(&shared_requests);
                        let max = Arc::clone(&shared_max);
                        let in_flight = Arc::clone(&shared_in_flight);
                        let hook = hook.clone();
                        thread::spawn(move || {
                            let request = read_http_request(&mut stream);
                            let bank = request_bank(&request);
                            {
                                let mut active = in_flight.lock().expect("in-flight map");
                                let count = active.entry(bank.clone()).or_insert(0usize);
                                *count += 1;
                                let mut peaks = max.lock().expect("max in-flight map");
                                let peak = peaks.entry(bank.clone()).or_insert(0usize);
                                *peak = (*peak).max(*count);
                            }
                            requests.lock().expect("request log").push(request);
                            if let Some(hook) = &hook {
                                hook();
                            }
                            if !delay.is_zero() {
                                thread::sleep(delay);
                            }
                            let response = format!(
                                "HTTP/1.1 {status} X\r\nContent-Length: 2\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{{}}"
                            );
                            // Hold the same lock used by the next request's
                            // increment until the response is written and this
                            // request is no longer in flight.
                            let mut active = in_flight.lock().expect("in-flight map");
                            let _ = stream.write_all(response.as_bytes());
                            if let Some(count) = active.get_mut(&bank) {
                                *count = (*count).saturating_sub(1);
                            }
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => break,
                }
            }
        });

        Self {
            url: format!("http://{address}"),
            requests,
            max_in_flight,
            stop,
            handle: Some(handle),
        }
    }

    pub fn url(&self) -> &str {
        &self.url
    }

    pub fn requests(&self) -> Vec<String> {
        self.requests.lock().expect("request log").clone()
    }

    pub fn max_in_flight(&self, bank: &str) -> usize {
        self.max_in_flight
            .lock()
            .expect("max in-flight map")
            .get(bank)
            .copied()
            .unwrap_or(0)
    }

    pub fn shutdown(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

#[allow(dead_code)]
impl Drop for TestServer {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[allow(dead_code)]
fn read_http_request(stream: &mut std::net::TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("read timeout");
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 4096];
    while let Ok(read) = stream.read(&mut chunk) {
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(header_end) = find_headers_end(&buffer) {
            let headers = String::from_utf8_lossy(&buffer[..header_end]).to_string();
            let length = content_length(&headers);
            if buffer.len() >= header_end + length {
                break;
            }
        }
    }
    String::from_utf8_lossy(&buffer).to_string()
}

#[allow(dead_code)]
fn request_bank(request: &str) -> String {
    let path = request
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("");
    path.split('/')
        .collect::<Vec<_>>()
        .windows(2)
        .find_map(|pair| (pair[0] == "banks").then(|| pair[1].to_string()))
        .unwrap_or_else(|| "unknown".to_string())
}
