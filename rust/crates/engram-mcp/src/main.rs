use std::collections::BTreeMap;
use std::net::SocketAddr;

use clap::Parser;
use engram_mcp::{router, Project};

/// Native Engram MCP lifecycle server (issue #211, slice 1).
#[derive(Debug, Parser)]
#[command(name = "engram-mcp", version, about)]
struct Args {
    /// Comma-separated project routes to serve, for example `engram,kubernaut`.
    #[arg(long, value_delimiter = ',', default_value = "engram")]
    projects: Vec<String>,

    /// Loopback bind address. Deployment-local; never a public interface.
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    #[arg(long, default_value_t = 8898)]
    port: u16,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    let projects: BTreeMap<String, Project> = args
        .projects
        .into_iter()
        .map(|name| {
            let project = Project::new(name.clone());
            (name, project)
        })
        .collect();
    let app = router(projects.into_values());
    let address: SocketAddr = format!("{}:{}", args.host, args.port).parse()?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    println!("engram-mcp listening on http://{address}/mcp/<project>");
    axum::serve(listener, app).await?;
    Ok(())
}
