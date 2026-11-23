use std::collections::BTreeMap;
use std::net::{IpAddr, SocketAddr};

use clap::Parser;
use engram_mcp::{router_with_options, OriginPolicy, Project, SessionPolicy};

/// Native Engram MCP lifecycle server (issue #211, slice 1).
#[derive(Debug, Parser)]
#[command(name = "engram-mcp", version, about)]
struct Args {
    /// Comma-separated project routes to serve, for example `engram,kubernaut`.
    #[arg(long, value_delimiter = ',', default_value = "engram")]
    projects: Vec<String>,

    /// Loopback bind address. Non-loopback binds are refused.
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    #[arg(long, default_value_t = 8898)]
    port: u16,

    /// Accept non-initialize requests without `Mcp-Session-Id`.
    /// Off by default (MCP behavior); on for Python-gateway parity.
    #[arg(long)]
    allow_stateless: bool,

    /// Additional browser Origin allowed to call the gateway. Loopback
    /// origins are always allowed; requests without Origin are native clients.
    #[arg(long = "allow-origin", value_name = "ORIGIN")]
    allow_origins: Vec<String>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();

    let host: IpAddr = args
        .host
        .parse()
        .map_err(|_| format!("invalid --host {:?}: expected an IP address", args.host))?;
    if !host.is_loopback() {
        return Err(format!(
            "refusing non-loopback bind {host}; the MCP catalog is unauthenticated"
        )
        .into());
    }

    let mut projects: BTreeMap<String, Project> = BTreeMap::new();
    for name in args.projects {
        let name = name.trim();
        if name.is_empty() {
            return Err("--projects contains an empty name".into());
        }
        projects.insert(name.to_string(), Project::new(name));
    }

    let policy = if args.allow_stateless {
        SessionPolicy::Optional
    } else {
        SessionPolicy::Required
    };
    let app = router_with_options(
        projects.into_values(),
        policy,
        OriginPolicy {
            allow_no_origin: true,
            allowed_origins: args.allow_origins,
        },
    );
    let address = SocketAddr::new(host, args.port);
    let listener = tokio::net::TcpListener::bind(address).await?;
    println!("engram-mcp listening on http://{address}/mcp/<project>");
    axum::serve(listener, app).await?;
    Ok(())
}
