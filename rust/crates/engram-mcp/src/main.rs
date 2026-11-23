use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;

use clap::Parser;
use engram_mcp::{
    build_project_adapters, router_with_options, OriginPolicy, Project, Registry, SessionPolicy,
};

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

    /// Deployment-local instance registry. When set, HTTP backends are
    /// aggregated and prewarmed before serving.
    #[arg(long)]
    config: Option<PathBuf>,

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

    let projects: Vec<Project> = if let Some(path) = &args.config {
        let registry = Registry::load(path)?;
        let mut projects = Vec::new();
        for (name, spec) in &registry.projects {
            let adapters = build_project_adapters(name, spec)?;
            projects.push(Project::with_backends(name.clone(), adapters));
        }
        futures::future::join_all(projects.iter().map(Project::prewarm)).await;
        projects
    } else {
        let mut projects = Vec::new();
        for name in args.projects {
            let name = name.trim();
            if name.is_empty() {
                return Err("--projects contains an empty name".into());
            }
            projects.push(Project::new(name));
        }
        projects
    };

    let policy = if args.allow_stateless {
        SessionPolicy::Optional
    } else {
        SessionPolicy::Required
    };
    let app = router_with_options(
        projects,
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
