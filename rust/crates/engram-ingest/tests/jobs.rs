//! Public job state-machine behavior (the module also has focused unit tests).

use engram_ingest::{Job, JobError, JobState};

#[test]
fn a_crashed_worker_lease_is_recoverable_without_duplication() {
    let mut job = Job::pending(7);
    job.claim("worker-a", 0, 1_000).unwrap();
    assert!(job.recover_expired(1_000));
    assert_eq!(job.state, JobState::Pending);
    assert_eq!(job.attempts, 1);
    let lease = job.claim("worker-b", 2_000, 1_000).unwrap();
    job.complete(&lease.token, 2_500).unwrap();
    assert_eq!(job.state, JobState::Succeeded);
    assert_eq!(
        job.claim("worker-c", 3_000, 1_000),
        Err(JobError::NotPending)
    );
}
