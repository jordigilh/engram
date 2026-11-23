use std::io::Write;
use std::sync::Arc;

use engram_proxy::{bind, serve, ProxyConfig};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config = ProxyConfig::from_env()?;
    let (listener, address) = bind(&config).await?;

    // Machine-readable handshake for process-level tests and service logs.
    println!("PROXY_LISTEN={address}");
    std::io::stdout().flush()?;
    eprintln!(
        "engram-proxy listening on {address}, backend state file={}, default backend port={}",
        config.state_file.display(),
        config.default_backend_port
    );

    serve(listener, Arc::new(config)).await;
}
