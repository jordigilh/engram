//! Loopback integration tests for the byte-transparent Hindsight proxy.
//!
//! Every test uses an ephemeral loopback port; the production `8888`/`18888`
//! ports are never bound.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, AtomicUsize, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use engram_proxy::{bind, read_active_backend_port, serve, ProxyConfig};
use tokio::io::{copy, AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let unique = format!(
            "engram-proxy-it-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        );
        let path = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&path).expect("temp dir");
        Self(path)
    }

    fn state_path(&self) -> PathBuf {
        self.0.join("active-backend.port")
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn write_state(path: &Path, port: u16) {
    // Mirror the blue/green script: write a sibling temp file, then rename.
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, format!("{port}\n")).expect("write state");
    std::fs::rename(&temporary, path).expect("rename state");
}

async fn spawn_proxy(
    state_file: PathBuf,
    default_backend_port: u16,
) -> (std::net::SocketAddr, JoinHandle<()>) {
    let config = ProxyConfig {
        listen_host: "127.0.0.1".to_owned(),
        listen_port: 0,
        state_file,
        default_backend_port,
    };
    let (listener, address) = bind(&config).await.expect("bind proxy");
    assert!(address.ip().is_loopback());
    let task = tokio::spawn(async move {
        let _ = serve(listener, Arc::new(config)).await;
    });
    (address, task)
}

async fn spawn_echo_backend() -> (u16, JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("bind");
    let port = listener.local_addr().expect("addr").port();
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(async move {
                let (mut reader, mut writer) = stream.into_split();
                let _ = copy(&mut reader, &mut writer).await;
            });
        }
    });
    (port, task)
}

/// Writes `tag` once on accept, then prefixes every echoed chunk with it.
async fn spawn_tagged_backend(tag: &'static [u8]) -> (u16, Arc<AtomicUsize>, JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("bind");
    let port = listener.local_addr().expect("addr").port();
    let accepted = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&accepted);
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            counter.fetch_add(1, Ordering::SeqCst);
            tokio::spawn(async move {
                let (mut reader, mut writer) = stream.into_split();
                if writer.write_all(tag).await.is_err() {
                    return;
                }
                let mut buffer = [0u8; 4096];
                loop {
                    match reader.read(&mut buffer).await {
                        Ok(0) => break,
                        Ok(read) => {
                            if writer.write_all(tag).await.is_err()
                                || writer.write_all(&buffer[..read]).await.is_err()
                            {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
            });
        }
    });
    (port, accepted, task)
}

async fn write_all_within(stream: &mut TcpStream, payload: &[u8]) {
    tokio::time::timeout(Duration::from_secs(5), stream.write_all(payload))
        .await
        .expect("write deadline")
        .expect("write");
}

async fn read_exact_within(stream: &mut TcpStream, buffer: &mut [u8]) {
    tokio::time::timeout(Duration::from_secs(5), stream.read_exact(buffer))
        .await
        .expect("read deadline")
        .expect("read");
}

/// Accepts and then never reads or writes; used to hold a relay open.
async fn spawn_silent_backend() -> (u16, JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("bind");
    let port = listener.local_addr().expect("addr").port();
    let task = tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((stream, _)) = listener.accept().await {
            held.push(stream);
        }
    });
    (port, task)
}

#[tokio::test]
async fn bytes_round_trip_unchanged() {
    let temp = TempDir::new();
    let (backend_port, backend) = spawn_echo_backend().await;
    write_state(&temp.state_path(), backend_port);
    let (address, proxy) = spawn_proxy(temp.state_path(), 18888).await;

    let mut payload: Vec<u8> = b"POST /mcp HTTP/1.1\r\nHost: example\r\n\r\n\
        {\"jsonrpc\":\"2.0\"}\x00\x01\xff\xfe\r\n"
        .to_vec();
    payload.extend((0..128 * 1024).map(|offset| (offset % 251) as u8));

    let mut stream = TcpStream::connect(address).await.expect("connect");
    stream.write_all(&payload).await.expect("write");
    let mut echoed = vec![0u8; payload.len()];
    tokio::time::timeout(Duration::from_secs(5), stream.read_exact(&mut echoed))
        .await
        .expect("read deadline")
        .expect("read");
    assert_eq!(echoed, payload);

    proxy.abort();
    backend.abort();
}

#[tokio::test]
async fn concurrent_clients_do_not_cross_talk() {
    let temp = TempDir::new();
    let (backend_port, backend) = spawn_echo_backend().await;
    write_state(&temp.state_path(), backend_port);
    let (address, proxy) = spawn_proxy(temp.state_path(), 18888).await;

    let mut clients = Vec::new();
    for index in 0u8..16 {
        clients.push(tokio::spawn(async move {
            let payload: Vec<u8> = (0..4096)
                .map(|offset| index.wrapping_add((offset % 251) as u8))
                .collect();
            let mut stream = TcpStream::connect(address).await.expect("connect");
            stream.write_all(&payload).await.expect("write");
            let mut echoed = vec![0u8; payload.len()];
            tokio::time::timeout(Duration::from_secs(5), stream.read_exact(&mut echoed))
                .await
                .expect("read deadline")
                .expect("read");
            assert_eq!(echoed, payload);
        }));
    }
    for client in clients {
        client.await.expect("client task");
    }

    proxy.abort();
    backend.abort();
}

#[tokio::test]
async fn blue_green_swap_pins_existing_client() {
    let temp = TempDir::new();
    let (blue_port, blue_connections, blue) = spawn_tagged_backend(b"B:").await;
    let (green_port, green_connections, green) = spawn_tagged_backend(b"G:").await;
    write_state(&temp.state_path(), blue_port);
    let (address, proxy) = spawn_proxy(temp.state_path(), blue_port).await;

    // Client A must complete a round-trip against blue *before* the flip so
    // the per-connection state read cannot race the state-file change.
    let mut client_a = TcpStream::connect(address).await.expect("connect A");
    let mut tag = [0u8; 2];
    read_exact_within(&mut client_a, &mut tag).await;
    assert_eq!(&tag, b"B:");
    write_all_within(&mut client_a, b"one").await;
    let mut reply = [0u8; 5];
    read_exact_within(&mut client_a, &mut reply).await;
    assert_eq!(&reply, b"B:one");

    write_state(&temp.state_path(), green_port);

    let mut client_b = TcpStream::connect(address).await.expect("connect B");
    let mut tag = [0u8; 2];
    read_exact_within(&mut client_b, &mut tag).await;
    assert_eq!(&tag, b"G:");
    write_all_within(&mut client_b, b"new").await;
    let mut reply = [0u8; 5];
    read_exact_within(&mut client_b, &mut reply).await;
    assert_eq!(&reply, b"G:new");

    // Existing client A stays on blue after the swap.
    write_all_within(&mut client_a, b"two").await;
    let mut reply = [0u8; 5];
    read_exact_within(&mut client_a, &mut reply).await;
    assert_eq!(&reply, b"B:two");

    assert!(blue_connections.load(Ordering::SeqCst) >= 1);
    assert!(green_connections.load(Ordering::SeqCst) >= 1);

    proxy.abort();
    blue.abort();
    green.abort();
}

#[tokio::test]
async fn backend_unreachable_closes_client_and_listener_survives() {
    let temp = TempDir::new();
    // Bind then drop to obtain a loopback port with nothing listening. Retry
    // if the kernel hands that same port to the proxy listener.
    let (address, proxy, unreachable_port) = loop {
        let unused = TcpListener::bind(("127.0.0.1", 0)).await.expect("bind");
        let candidate = unused.local_addr().expect("addr").port();
        drop(unused);

        write_state(&temp.state_path(), candidate);
        let (address, proxy) = spawn_proxy(temp.state_path(), candidate).await;
        if address.port() != candidate {
            break (address, proxy, candidate);
        }
        proxy.abort();
    };
    assert_eq!(
        read_active_backend_port(&temp.state_path(), 0),
        unreachable_port
    );

    let mut refused = TcpStream::connect(address).await.expect("connect");
    let mut buffer = [0u8; 1];
    let read = tokio::time::timeout(Duration::from_secs(5), refused.read(&mut buffer))
        .await
        .expect("client close deadline")
        .expect("read");
    assert_eq!(read, 0, "refused backend must close the client");

    // The listener must accept and serve a working backend afterwards.
    let (live_port, backend) = spawn_echo_backend().await;
    write_state(&temp.state_path(), live_port);
    let mut working = TcpStream::connect(address).await.expect("reconnect");
    working.write_all(b"alive").await.expect("write");
    let mut echoed = [0u8; 5];
    tokio::time::timeout(Duration::from_secs(5), working.read_exact(&mut echoed))
        .await
        .expect("read deadline")
        .expect("read");
    assert_eq!(&echoed, b"alive");

    proxy.abort();
    backend.abort();
}

#[tokio::test]
async fn missing_state_file_uses_configured_default_backend() {
    let temp = TempDir::new();
    let (backend_port, backend) = spawn_echo_backend().await;
    // No state file is written: the relay must resolve the configured default.
    let (address, proxy) = spawn_proxy(temp.state_path(), backend_port).await;

    let mut stream = TcpStream::connect(address).await.expect("connect");
    stream.write_all(b"fallback").await.expect("write");
    let mut echoed = [0u8; 8];
    tokio::time::timeout(Duration::from_secs(5), stream.read_exact(&mut echoed))
        .await
        .expect("read deadline")
        .expect("read");
    assert_eq!(&echoed, b"fallback");

    proxy.abort();
    backend.abort();
}

#[tokio::test]
async fn listener_responsive_while_client_parked_on_silent_backend() {
    let temp = TempDir::new();
    let (silent_port, silent) = spawn_silent_backend().await;
    write_state(&temp.state_path(), silent_port);
    let (address, proxy) = spawn_proxy(temp.state_path(), silent_port).await;

    // Client A resolves silent and is parked in the relay indefinitely.
    let _parked = TcpStream::connect(address).await.expect("connect A");
    tokio::time::sleep(Duration::from_millis(100)).await;

    let (live_port, backend) = spawn_echo_backend().await;
    write_state(&temp.state_path(), live_port);

    let mut client_b = TcpStream::connect(address).await.expect("connect B");
    client_b.write_all(b"fast").await.expect("write B");
    let mut echoed = [0u8; 4];
    tokio::time::timeout(Duration::from_secs(2), client_b.read_exact(&mut echoed))
        .await
        .expect("listener must not be serialized behind the parked client")
        .expect("read B");
    assert_eq!(&echoed, b"fast");

    proxy.abort();
    silent.abort();
    backend.abort();
}

#[tokio::test]
async fn client_abort_mid_relay_does_not_kill_listener() {
    let temp = TempDir::new();

    // Backend that drains its input, then writes a large response. A client
    // that disconnects mid-response makes the backend-to-client pump fail.
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("bind");
    let failing_port = listener.local_addr().expect("addr").port();
    let (write_sender, write_receiver) = tokio::sync::oneshot::channel();
    let failing = tokio::spawn(async move {
        if let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(async move {
                let (mut reader, mut writer) = stream.into_split();
                let mut sink = [0u8; 1024];
                while let Ok(read) = reader.read(&mut sink).await {
                    if read == 0 {
                        break;
                    }
                }
                let payload = vec![0xabu8; 1024 * 1024];
                let result = writer.write_all(&payload).await;
                let _ = write_sender.send(result);
            });
        }
    });

    write_state(&temp.state_path(), failing_port);
    let (address, proxy) = spawn_proxy(temp.state_path(), failing_port).await;

    {
        let mut aborting = TcpStream::connect(address).await.expect("connect");
        aborting.write_all(b"x").await.expect("write");
        // Dropping sends FIN; the backend then writes into a closed peer.
    }
    // A successful local write does not prove delivery, so only assert that
    // the backend finished its post-EOF write before checking the listener.
    let _write_result = tokio::time::timeout(Duration::from_secs(5), write_receiver)
        .await
        .expect("backend write deadline")
        .expect("backend write signal");

    let (live_port, backend) = spawn_echo_backend().await;
    write_state(&temp.state_path(), live_port);
    let mut next = TcpStream::connect(address).await.expect("reconnect");
    next.write_all(b"still-here").await.expect("write");
    let mut echoed = [0u8; 10];
    tokio::time::timeout(Duration::from_secs(5), next.read_exact(&mut echoed))
        .await
        .expect("read deadline")
        .expect("read");
    assert_eq!(&echoed, b"still-here");

    proxy.abort();
    failing.abort();
    backend.abort();
}

struct ChildGuard(Option<Child>);

impl ChildGuard {
    fn stop(&mut self) {
        if let Some(mut child) = self.0.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        self.stop();
    }
}

fn read_startup_line(stdout: std::process::ChildStdout) -> String {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut line = String::new();
        let _ = BufReader::new(stdout).read_line(&mut line);
        let _ = sender.send(line);
    });
    receiver
        .recv_timeout(Duration::from_secs(10))
        .expect("proxy startup line")
}

fn spawn_binary(port: u16, state: &Path, default_backend_port: u16) -> (ChildGuard, String) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_engram-proxy"))
        .env_clear()
        .env("HOME", state.parent().expect("state parent"))
        .env("HINDSIGHT_PROXY_HOST", "127.0.0.1")
        .env("HINDSIGHT_PROXY_PORT", port.to_string())
        .env("HINDSIGHT_ACTIVE_BACKEND_FILE", state)
        .env(
            "HINDSIGHT_DEFAULT_BACKEND_PORT",
            default_backend_port.to_string(),
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn proxy binary");
    let stdout = child.stdout.take().expect("stdout");
    // Guard exists before any assertion can panic and orphan the child.
    let guard = ChildGuard(Some(child));
    let line = read_startup_line(stdout);
    (guard, line)
}

fn start_fake_backend(connections: usize) -> (u16, thread::JoinHandle<()>) {
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("bind backend");
    let port = listener.local_addr().expect("addr").port();
    let handle = thread::spawn(move || {
        for _ in 0..connections {
            let Ok((mut socket, _)) = listener.accept() else {
                return;
            };
            let mut buffer = [0u8; 1024];
            loop {
                match socket.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(read) => {
                        if socket.write_all(&buffer[..read]).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        }
    });
    (port, handle)
}

fn echo_through(address: &str, payload: &[u8]) {
    let mut client = std::net::TcpStream::connect(address).expect("connect");
    client
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("timeout");
    client.write_all(payload).expect("write");
    let mut echoed = vec![0u8; payload.len()];
    client.read_exact(&mut echoed).expect("read");
    assert_eq!(&echoed, payload);
}

#[test]
fn binary_uses_documented_env_contract() {
    let temp = TempDir::new();
    let (backend_port, backend) = start_fake_backend(1);
    let state_path = temp.state_path();
    write_state(&state_path, backend_port);

    let (guard, line) = spawn_binary(0, &state_path, 18889);
    let address = line
        .trim()
        .strip_prefix("PROXY_LISTEN=")
        .expect("PROXY_LISTEN= line")
        .to_owned();

    echo_through(&address, b"binary-env-transparency\x00\xff");

    let _ = backend.join();
    drop(guard);
}

#[test]
fn binary_listener_rebinds_after_restart() {
    let temp = TempDir::new();
    let (backend_port, backend) = start_fake_backend(2);
    let state_path = temp.state_path();
    write_state(&state_path, backend_port);

    let probe = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("probe bind");
    let port = probe.local_addr().expect("addr").port();
    drop(probe);

    let (mut guard, line) = spawn_binary(port, &state_path, backend_port);
    let expected = format!("PROXY_LISTEN=127.0.0.1:{port}");
    assert_eq!(line.trim(), expected);
    echo_through(&format!("127.0.0.1:{port}"), b"before-restart");

    guard.stop();
    let (guard, line) = spawn_binary(port, &state_path, backend_port);
    assert_eq!(line.trim(), expected);
    echo_through(&format!("127.0.0.1:{port}"), b"after-restart");

    drop(guard);
    let _ = backend.join();
}
