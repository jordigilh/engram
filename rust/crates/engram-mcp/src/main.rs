use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;

use clap::Parser;
use engram_mcp::{
    build_project_adapters, router_with_options, OriginPolicy, Project, Registry, SessionPolicy,
};

/// Native Engram MCP gateway (issue #211, slices 1-2).
#[derive(Debug, Parser)]
#[command(name = "engram-mcp", version, about)]
struct Args {
    /// Comma-separated project routes to serve when no registry is configured.
    #[arg(long, value_delimiter = ',')]
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

    let config = match &args.config {
        Some(path) => Some(path.clone()),
        None if args.projects.is_empty() => default_config_path(),
        None => None,
    };
    let projects: Vec<Project> = if let Some(path) = config {
        let registry = Registry::load(&path)?;
        let mut projects = Vec::new();
        for (name, spec) in &registry.projects {
            let adapters = build_project_adapters(name, spec)?;
            projects.push(Project::with_backends(name.clone(), adapters));
        }
        projects
    } else {
        if args.projects.is_empty() {
            return Err("provide --config or --projects".into());
        }
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
    let address = SocketAddr::new(host, args.port);
    let listener = tokio::net::TcpListener::bind(address).await?;

    // Bind first, then warm catalogs in the background. A slow backend must
    // not delay the listener or take down unrelated routes.
    for project in &projects {
        let project = project.clone();
        tokio::spawn(async move {
            project.prewarm().await;
        });
    }

    let app = router_with_options(
        projects,
        policy,
        OriginPolicy {
            allow_no_origin: true,
            allowed_origins: args.allow_origins,
        },
    );
    println!("engram-mcp listening on http://{address}/mcp/<project>");
    axum::serve(listener, app).await?;
    Ok(())
}

fn default_config_path() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    let path = PathBuf::from(home).join(".engram/runtime/native-instances.toml");
    path.is_file().then_some(path)
}
