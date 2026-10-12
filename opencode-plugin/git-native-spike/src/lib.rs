use std::path::PathBuf;

use napi::{Error, Result};
use napi_derive::napi;

/// Git metadata needed by the OpenCode plugin without spawning `git`.
#[napi(object)]
pub struct GitRepositoryInfo {
    /// True when the path resolves to a non-bare Git worktree.
    pub is_git_worktree: bool,
    /// The short branch name, or null for detached/unborn HEADs.
    pub branch: Option<String>,
    /// The full HEAD object ID, or null for an unborn HEAD.
    pub commit: Option<String>,
    /// Fetch URLs for configured remotes, in Git's configured order.
    pub remotes: Vec<String>,
}

fn not_a_worktree() -> GitRepositoryInfo {
    GitRepositoryInfo {
        is_git_worktree: false,
        branch: None,
        commit: None,
        remotes: Vec::new(),
    }
}

fn native_error(error: impl std::fmt::Display) -> Error {
    Error::from_reason(error.to_string())
}

/// Inspect a path using gitoxide's repository implementation.
///
/// Discovery intentionally searches parent directories, matching the
/// `git -C <path> rev-parse` behavior used by the current plugin. A repository
/// without a worktree (for example a bare repository) is not an OpenCode
/// worktree for this API.
#[napi]
pub fn inspect_git_repository(directory: String) -> Result<GitRepositoryInfo> {
    let repository = match gix::discover(PathBuf::from(directory)) {
        Ok(repository) => repository,
        Err(_) => return Ok(not_a_worktree()),
    };

    if repository.workdir().is_none() {
        return Ok(not_a_worktree());
    }

    let head = repository.head().map_err(native_error)?;
    let commit = head.id().map(|id| id.to_string());
    // `gix` can report the symbolic name of an unborn HEAD, while the
    // current plugin treats `git rev-parse HEAD` failure as no revision.
    // Keep the probe's branch/commit pair consistent with that contract.
    let branch = commit
        .as_ref()
        .and_then(|_| head.referent_name().map(|name| name.shorten().to_string()));

    let remotes = repository
        .remote_names()
        .into_iter()
        .filter_map(|name| repository.find_remote(name).ok())
        .filter_map(|remote| {
            remote
                .url(gix::remote::Direction::Fetch)
                .map(ToString::to_string)
        })
        .collect();

    Ok(GitRepositoryInfo {
        is_git_worktree: true,
        branch,
        commit,
        remotes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn non_worktree_value_has_no_git_metadata() {
        let value = not_a_worktree();

        assert!(!value.is_git_worktree);
        assert_eq!(value.branch, None);
        assert_eq!(value.commit, None);
        assert!(value.remotes.is_empty());
    }

    #[test]
    fn missing_and_non_git_paths_are_safe() {
        let missing = inspect_git_repository(
            std::env::temp_dir()
                .join(format!("engram-git-native-missing-{}", std::process::id()))
                .to_string_lossy()
                .into_owned(),
        )
        .expect("repository discovery should degrade to a non-worktree result");
        assert!(!missing.is_git_worktree);

        let directory =
            std::env::temp_dir().join(format!("engram-git-native-non-git-{}", std::process::id()));
        fs::create_dir_all(&directory).expect("create temporary directory");
        let result = inspect_git_repository(directory.to_string_lossy().into_owned())
            .expect("non-Git discovery should not fail");
        fs::remove_dir_all(&directory).expect("remove temporary directory");
        assert!(!result.is_git_worktree);
    }

    #[test]
    fn discovers_the_repository_containing_the_crate() {
        let result = inspect_git_repository(env!("CARGO_MANIFEST_DIR").to_owned())
            .expect("the crate is checked out inside a Git repository");

        assert!(result.is_git_worktree);
        assert!(result.commit.is_some());
    }
}
