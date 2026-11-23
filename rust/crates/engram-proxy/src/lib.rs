//! Byte-transparent TCP proxy in front of the blue/green Hindsight API.
//!
//! This is the native replacement for `src/engram/pipeline/hindsight_proxy.py`
//! (issue #211, design Stage 1). It owns the public listener, resolves the
//! active backend from a state file for every new client connection, and pumps
//! raw bytes without parsing HTTP or MCP.
//!
//! Contract:
//! - public listener defaults to `0.0.0.0:8888`;
//! - backend host is always `127.0.0.1`;
//! - `~/.engram/state/active-backend.port` is re-read per connection;
//! - a missing, unreadable, or invalid state file falls back to port 18888;
//! - the backend connect has a five-second timeout;
//! - existing connections stay on the backend they resolved at accept time;
//! - a backend failure closes only the affected client.

use std::future::Future;
use std::io;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use thiserror::Error;
use tokio::io::{copy, AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, TcpStream};
use tokio::time::{sleep, timeout};

/// Hindsight always lives on loopback; only the blue/green port changes.
pub const BACKEND_HOST: &str = "127.0.0.1";
pub const DEFAULT_LISTEN_HOST: &str = "0.0.0.0";
pub const DEFAULT_LISTEN_PORT: u16 = 8888;
pub const DEFAULT_BACKEND_PORT: u16 = 18888;
pub const BACKEND_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Configuration for the proxy listener and state-file resolution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProxyConfig {
    pub listen_host: String,
    pub listen_port: u16,
    pub state_file: PathBuf,
    pub default_backend_port: u16,
}

impl ProxyConfig {
    /// Build a config from the process environment.
    pub fn from_env() -> Result<Self, ConfigError> {
        Self::from_lookup(|name| {
            std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
        })
    }

    /// Build a config from a name/value lookup. Kept separate from
    /// `from_env` so tests never mutate process-global environment state.
    pub fn from_lookup<F>(mut lookup: F) -> Result<Self, ConfigError>
    where
        F: FnMut(&str) -> Option<String>,
    {
        let listen_host = lookup("HINDSIGHT_PROXY_HOST")
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_LISTEN_HOST.to_owned());

        let listen_port = match lookup("HINDSIGHT_PROXY_PORT") {
            Some(value) => parse_port("HINDSIGHT_PROXY_PORT", &value)?,
            None => DEFAULT_LISTEN_PORT,
        };

        let default_backend_port = match lookup("HINDSIGHT_DEFAULT_BACKEND_PORT") {
            Some(value) => parse_port("HINDSIGHT_DEFAULT_BACKEND_PORT", &value)?,
            None => DEFAULT_BACKEND_PORT,
        };

        let state_file = match lookup("HINDSIGHT_ACTIVE_BACKEND_FILE") {
            Some(value) if !value.trim().is_empty() => PathBuf::from(value),
            _ => {
                let home = lookup("HOME").ok_or(ConfigError::MissingHome)?;
                default_state_path(Path::new(&home))
            }
        };

        Ok(Self {
            listen_host,
            listen_port,
            state_file,
            default_backend_port,
        })
    }
}

/// The documented default state-file location.
pub fn default_state_path(home: &Path) -> PathBuf {
    home.join(".engram/state/active-backend.port")
}

fn parse_port(name: &'static str, value: &str) -> Result<u16, ConfigError> {
    value
        .trim()
        .parse::<u16>()
        .map_err(|_| ConfigError::InvalidPort {
            name,
            value: value.to_owned(),
        })
}

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("{name}={value:?} is not a valid TCP port")]
    InvalidPort { name: &'static str, value: String },
    #[error("HOME is unset and HINDSIGHT_ACTIVE_BACKEND_FILE is not provided")]
    MissingHome,
}

/// Resolve the backend port for a new client connection.
///
/// Any IO/UTF-8/parse error, an empty value, or an unusable port falls back to
/// `default_port`. This mirrors the Python proxy's fallback for missing or
/// partially written state files; unlike Python `int()`, out-of-range and
/// underscore forms also fall back, and `0` is treated as unusable.
pub fn read_active_backend_port(path: &Path, default_port: u16) -> u16 {
    match std::fs::read_to_string(path) {
        Ok(text) => parse_backend_port(&text, default_port),
        Err(_) => default_port,
    }
}

async fn read_active_backend_port_async(path: &Path, default_port: u16) -> u16 {
    match tokio::fs::read_to_string(path).await {
        Ok(text) => parse_backend_port(&text, default_port),
        Err(_) => default_port,
    }
}

fn parse_backend_port(text: &str, default_port: u16) -> u16 {
    text.trim()
        .parse::<u16>()
        .ok()
        .filter(|port| *port != 0)
        .unwrap_or(default_port)
}

/// A byte stream that can be relayed in both directions.
pub trait RelayStream: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> RelayStream for T {}

pub type BoxStream = Box<dyn RelayStream>;

/// Opens the backend connection for a resolved port.
pub trait Connector: Send + Sync + 'static {
    fn connect(&self, port: u16) -> Pin<Box<dyn Future<Output = io::Result<BoxStream>> + Send>>;
}

/// Production connector: loopback TCP with `TCP_NODELAY`.
pub struct TcpConnector;

impl Connector for TcpConnector {
    fn connect(&self, port: u16) -> Pin<Box<dyn Future<Output = io::Result<BoxStream>> + Send>> {
        Box::pin(async move {
            let stream = connect_backend(port).await?;
            Ok(Box::new(stream) as BoxStream)
        })
    }
}

/// Connect to the loopback backend and disable Nagle, matching asyncio's
/// transport defaults.
pub async fn connect_backend(port: u16) -> io::Result<TcpStream> {
    let stream = TcpStream::connect((BACKEND_HOST, port)).await?;
    stream.set_nodelay(true)?;
    Ok(stream)
}

/// Apply the five-second backend connect timeout to any connector future.
pub async fn connect_with_timeout<F>(future: F) -> io::Result<BoxStream>
where
    F: Future<Output = io::Result<BoxStream>>,
{
    match timeout(BACKEND_CONNECT_TIMEOUT, future).await {
        Ok(result) => result,
        Err(_) => Err(io::Error::new(
            io::ErrorKind::TimedOut,
            format!(
                "backend connect timed out after {}s",
                BACKEND_CONNECT_TIMEOUT.as_secs()
            ),
        )),
    }
}

/// Resolve the listener address and bind. Only the first resolved address is
/// bound (documented divergence from asyncio, which binds every result).
pub async fn bind(config: &ProxyConfig) -> io::Result<(TcpListener, std::net::SocketAddr)> {
    let mut addresses =
        tokio::net::lookup_host((config.listen_host.as_str(), config.listen_port)).await?;
    let address = addresses.next().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::AddrNotAvailable,
            format!(
                "{}:{} resolved to no addresses",
                config.listen_host, config.listen_port
            ),
        )
    })?;
    let listener = TcpListener::bind(address).await?;
    let local = listener.local_addr()?;
    Ok((listener, local))
}

/// Relay one client to the backend selected for it.
pub async fn handle_client<S, C>(client: S, config: &ProxyConfig, connector: &C) -> io::Result<()>
where
    S: RelayStream,
    C: Connector,
{
    let port =
        read_active_backend_port_async(&config.state_file, config.default_backend_port).await;
    let backend = connect_with_timeout(connector.connect(port))
        .await
        .map_err(|error| with_backend_context(port, error))?;
    relay(client, backend)
        .await
        .map_err(|error| with_backend_context(port, error))
}

fn with_backend_context(port: u16, error: io::Error) -> io::Error {
    io::Error::new(
        error.kind(),
        format!("backend {BACKEND_HOST}:{port}: {error}"),
    )
}

/// Python parity: a `_pump` closes both sides as soon as either direction
/// reaches EOF, so a half-closed client cannot pin a relay task forever.
async fn relay<S: RelayStream>(client: S, backend: BoxStream) -> io::Result<()> {
    let (mut client_read, mut client_write) = tokio::io::split(client);
    let (mut backend_read, mut backend_write) = tokio::io::split(backend);

    let to_backend = copy(&mut client_read, &mut backend_write);
    let to_client = copy(&mut backend_read, &mut client_write);

    tokio::select! {
        result = to_backend => result.map(|_| ()),
        result = to_client => result.map(|_| ()),
    }
}

/// Routine disconnects are expected for persistent MCP clients and are not
/// logged as errors (the Python reference swallows the same kinds).
fn is_expected_disconnect(kind: io::ErrorKind) -> bool {
    matches!(
        kind,
        io::ErrorKind::ConnectionReset
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::BrokenPipe
            | io::ErrorKind::UnexpectedEof
            | io::ErrorKind::NotConnected
    )
}

/// Policy for `accept()` errors: `None` means retry immediately.
pub fn accept_error_delay(kind: io::ErrorKind, consecutive: u32) -> Option<Duration> {
    match kind {
        io::ErrorKind::Interrupted
        | io::ErrorKind::ConnectionAborted
        | io::ErrorKind::ConnectionReset
        | io::ErrorKind::WouldBlock
            if consecutive <= 8 =>
        {
            None
        }
        _ => {
            let exponent = consecutive.saturating_sub(1).min(3);
            Some(Duration::from_millis(100u64 << exponent))
        }
    }
}

/// Accept clients forever, spawning one relay task per connection.
///
/// `accept()` errors never terminate the listener: transient aborts retry
/// immediately and persistent errors back off so the listener can recover
/// without ever refusing the public port.
pub async fn serve(listener: TcpListener, config: Arc<ProxyConfig>) -> ! {
    let connector = Arc::new(TcpConnector);
    let mut consecutive_errors: u32 = 0;

    loop {
        match listener.accept().await {
            Ok((stream, peer)) => {
                consecutive_errors = 0;
                if let Err(error) = stream.set_nodelay(true) {
                    eprintln!("engram-proxy: set_nodelay failed for {peer}: {error}");
                    continue;
                }
                let config = Arc::clone(&config);
                let connector = Arc::clone(&connector);
                tokio::spawn(async move {
                    if let Err(error) = handle_client(stream, &config, connector.as_ref()).await {
                        if !is_expected_disconnect(error.kind()) {
                            eprintln!("engram-proxy: client {peer} relay failed: {error}");
                        }
                    }
                });
            }
            Err(error) => {
                consecutive_errors = consecutive_errors.saturating_add(1);
                let delay = accept_error_delay(error.kind(), consecutive_errors);
                if consecutive_errors == 1 || consecutive_errors.is_multiple_of(10) {
                    eprintln!(
                        "engram-proxy: accept failed ({consecutive_errors} consecutive): {error}"
                    );
                }
                if let Some(delay) = delay {
                    sleep(delay).await;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::sync::atomic::{AtomicU32, Ordering};

    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            static NEXT: AtomicU32 = AtomicU32::new(0);
            let unique = format!(
                "engram-proxy-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            );
            let path = std::env::temp_dir().join(unique);
            std::fs::create_dir_all(&path).expect("temp dir");
            Self(path)
        }

        fn file(&self, name: &str, contents: &str) -> PathBuf {
            let path = self.0.join(name);
            let mut file = std::fs::File::create(&path).expect("create");
            file.write_all(contents.as_bytes()).expect("write");
            path
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn config(state_file: PathBuf) -> ProxyConfig {
        ProxyConfig {
            listen_host: "127.0.0.1".to_owned(),
            listen_port: 0,
            state_file,
            default_backend_port: DEFAULT_BACKEND_PORT,
        }
    }

    #[test]
    fn reads_valid_port() {
        let dir = TempDir::new();
        let path = dir.file("active-backend.port", "18889\n");
        assert_eq!(read_active_backend_port(&path, 18888), 18889);
    }

    #[test]
    fn strips_surrounding_whitespace() {
        let dir = TempDir::new();
        let path = dir.file("active-backend.port", "  28888  \n");
        assert_eq!(read_active_backend_port(&path, 18888), 28888);
    }

    #[test]
    fn missing_file_falls_back() {
        let dir = TempDir::new();
        assert_eq!(
            read_active_backend_port(&dir.0.join("missing.port"), 18888),
            18888
        );
    }

    #[test]
    fn corrupt_file_falls_back() {
        let dir = TempDir::new();
        let path = dir.file("active-backend.port", "not-a-port-number");
        assert_eq!(read_active_backend_port(&path, 18888), 18888);
    }

    #[test]
    fn empty_file_falls_back() {
        let dir = TempDir::new();
        let path = dir.file("active-backend.port", " \n\t");
        assert_eq!(read_active_backend_port(&path, 18888), 18888);
    }

    #[test]
    fn unusable_ports_fall_back() {
        let dir = TempDir::new();
        for value in ["0", "-1", "65536", "999999999999", "1_8888"] {
            let path = dir.file("active-backend.port", value);
            assert_eq!(
                read_active_backend_port(&path, 18888),
                18888,
                "value {value:?}"
            );
        }
    }

    #[test]
    fn explicit_plus_sign_is_accepted() {
        let dir = TempDir::new();
        let path = dir.file("active-backend.port", "+18888");
        assert_eq!(read_active_backend_port(&path, 9999), 18888);
    }

    #[test]
    fn directory_path_falls_back() {
        let dir = TempDir::new();
        let path = dir.0.join("active-backend.port");
        std::fs::create_dir(&path).expect("mkdir");
        assert_eq!(read_active_backend_port(&path, 18888), 18888);
    }

    #[test]
    fn unreadable_file_falls_back() {
        let dir = TempDir::new();
        let path = dir.file("active-backend.port", "18889");
        let mut permissions = std::fs::metadata(&path).expect("metadata").permissions();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            permissions.set_mode(0o000);
            std::fs::set_permissions(&path, permissions).expect("chmod");
            if std::fs::read_to_string(&path).is_ok() {
                return; // running as root: permissions cannot deny the read
            }
            assert_eq!(read_active_backend_port(&path, 18888), 18888);
        }
        #[cfg(not(unix))]
        {
            let _ = permissions;
        }
    }

    fn lookup<'a>(values: &'a [(&'a str, &'a str)]) -> impl FnMut(&str) -> Option<String> + 'a {
        move |name| {
            values
                .iter()
                .find(|(key, _)| *key == name)
                .map(|(_, value)| (*value).to_owned())
        }
    }

    #[test]
    fn config_defaults_include_documented_state_path() {
        let config =
            ProxyConfig::from_lookup(lookup(&[("HOME", "/home/example")])).expect("config");
        assert_eq!(config.listen_host, DEFAULT_LISTEN_HOST);
        assert_eq!(config.listen_port, DEFAULT_LISTEN_PORT);
        assert_eq!(config.default_backend_port, DEFAULT_BACKEND_PORT);
        assert_eq!(
            config.state_file,
            PathBuf::from("/home/example/.engram/state/active-backend.port")
        );
    }

    #[test]
    fn config_overrides_from_lookup() {
        let config = ProxyConfig::from_lookup(lookup(&[
            ("HINDSIGHT_PROXY_HOST", "127.0.0.1"),
            ("HINDSIGHT_PROXY_PORT", "9888"),
            ("HINDSIGHT_ACTIVE_BACKEND_FILE", "/tmp/backend.port"),
            ("HINDSIGHT_DEFAULT_BACKEND_PORT", "19999"),
        ]))
        .expect("config");
        assert_eq!(config.listen_host, "127.0.0.1");
        assert_eq!(config.listen_port, 9888);
        assert_eq!(config.state_file, PathBuf::from("/tmp/backend.port"));
        assert_eq!(config.default_backend_port, 19999);
    }

    #[test]
    fn config_rejects_invalid_ports() {
        let error = ProxyConfig::from_lookup(lookup(&[
            ("HINDSIGHT_PROXY_PORT", "not-a-port"),
            ("HINDSIGHT_ACTIVE_BACKEND_FILE", "/tmp/backend.port"),
        ]))
        .unwrap_err();
        assert!(matches!(error, ConfigError::InvalidPort { .. }));

        let error = ProxyConfig::from_lookup(lookup(&[
            ("HINDSIGHT_DEFAULT_BACKEND_PORT", "70000"),
            ("HINDSIGHT_ACTIVE_BACKEND_FILE", "/tmp/backend.port"),
        ]))
        .unwrap_err();
        assert!(matches!(error, ConfigError::InvalidPort { .. }));
    }

    #[test]
    fn config_rejects_missing_home_without_explicit_state_file() {
        let error = ProxyConfig::from_lookup(lookup(&[])).unwrap_err();
        assert!(matches!(error, ConfigError::MissingHome));
    }

    #[test]
    fn empty_host_falls_back_to_default() {
        let config = ProxyConfig::from_lookup(lookup(&[
            ("HINDSIGHT_PROXY_HOST", "  "),
            ("HINDSIGHT_ACTIVE_BACKEND_FILE", "/tmp/backend.port"),
        ]))
        .expect("config");
        assert_eq!(config.listen_host, DEFAULT_LISTEN_HOST);
    }

    #[test]
    fn accept_error_policy_matches_kind() {
        for kind in [
            io::ErrorKind::Interrupted,
            io::ErrorKind::ConnectionAborted,
            io::ErrorKind::ConnectionReset,
            io::ErrorKind::WouldBlock,
        ] {
            assert_eq!(accept_error_delay(kind, 1), None);
        }
        assert_eq!(
            accept_error_delay(io::ErrorKind::PermissionDenied, 1),
            Some(Duration::from_millis(100))
        );
        assert_eq!(
            accept_error_delay(io::ErrorKind::PermissionDenied, 4),
            Some(Duration::from_millis(800))
        );
        assert_eq!(
            accept_error_delay(io::ErrorKind::PermissionDenied, 100),
            Some(Duration::from_millis(800))
        );
        assert_eq!(
            accept_error_delay(io::ErrorKind::ConnectionAborted, 9),
            Some(Duration::from_millis(800))
        );
    }

    #[test]
    fn expected_disconnect_kinds_are_quiet() {
        for kind in [
            io::ErrorKind::ConnectionReset,
            io::ErrorKind::ConnectionAborted,
            io::ErrorKind::BrokenPipe,
            io::ErrorKind::UnexpectedEof,
            io::ErrorKind::NotConnected,
        ] {
            assert!(is_expected_disconnect(kind));
        }
        assert!(!is_expected_disconnect(io::ErrorKind::TimedOut));
    }

    #[tokio::test]
    async fn bind_uses_loopback_port_zero() {
        let dir = TempDir::new();
        let mut config = config(dir.0.join("missing.port"));
        config.listen_host = "127.0.0.1".to_owned();
        let (_listener, address) = bind(&config).await.expect("bind");
        assert!(address.ip().is_loopback());
        assert_ne!(address.port(), 0);
    }

    #[tokio::test]
    async fn connect_backend_sets_nodelay() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("bind");
        let port = listener.local_addr().expect("addr").port();
        let accept = tokio::spawn(async move {
            let _ = listener.accept().await;
        });

        let stream = connect_backend(port).await.expect("connect");
        assert!(stream.nodelay().expect("nodelay"));
        accept.abort();
    }

    struct NeverConnector;

    impl Connector for NeverConnector {
        fn connect(
            &self,
            _port: u16,
        ) -> Pin<Box<dyn Future<Output = io::Result<BoxStream>> + Send>> {
            Box::pin(async {
                sleep(Duration::from_secs(60)).await;
                Err(io::Error::other("never reached"))
            })
        }
    }

    struct PendingReaderFailWriter;

    impl AsyncRead for PendingReaderFailWriter {
        fn poll_read(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
            _buffer: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Pending
        }
    }

    impl AsyncWrite for PendingReaderFailWriter {
        fn poll_write(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
            _buffer: &[u8],
        ) -> std::task::Poll<io::Result<usize>> {
            std::task::Poll::Ready(Err(io::Error::from(io::ErrorKind::BrokenPipe)))
        }

        fn poll_flush(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Ready(Ok(()))
        }

        fn poll_shutdown(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Ready(Ok(()))
        }
    }

    struct OneByteBackend {
        sent: bool,
    }

    impl AsyncRead for OneByteBackend {
        fn poll_read(
            mut self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
            buffer: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            if self.sent {
                return std::task::Poll::Ready(Ok(()));
            }
            self.sent = true;
            buffer.put_slice(b"x");
            std::task::Poll::Ready(Ok(()))
        }
    }

    impl AsyncWrite for OneByteBackend {
        fn poll_write(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
            buffer: &[u8],
        ) -> std::task::Poll<io::Result<usize>> {
            std::task::Poll::Ready(Ok(buffer.len()))
        }

        fn poll_flush(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Ready(Ok(()))
        }

        fn poll_shutdown(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            std::task::Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn relay_reports_client_write_failure() {
        let backend: BoxStream = Box::new(OneByteBackend { sent: false });
        let error = relay(PendingReaderFailWriter, backend)
            .await
            .expect_err("client write must fail");
        assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
        assert!(is_expected_disconnect(error.kind()));
    }

    #[tokio::test(start_paused = true)]
    async fn handle_client_timeout_closes_client() {
        use tokio::io::AsyncReadExt;

        let dir = TempDir::new();
        let config = config(dir.0.join("missing.port"));
        let (client, mut peer) = tokio::io::duplex(64);

        let started = tokio::time::Instant::now();
        let error = handle_client(client, &config, &NeverConnector)
            .await
            .expect_err("timeout");
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert_eq!(started.elapsed(), BACKEND_CONNECT_TIMEOUT);

        let mut byte = [0u8; 1];
        assert_eq!(peer.read(&mut byte).await.expect("read"), 0);
    }
}
