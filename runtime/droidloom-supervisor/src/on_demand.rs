//! Starting Droidloom for a launch that finds it stopped.
//!
//! An exported desktop entry runs `droidloomctl launch`. With Droidloom
//! stopped that failed at once, and the desktop went on waiting for a window
//! that never came. Such a launch now starts Droidloom as `droidloomctl start`
//! does and launches again. Starting on demand never enables the user
//! service: what a session starts by itself stays as the person set it.

use std::{
    fs, io,
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
    path::Path,
    thread,
    time::{Duration, Instant},
};

use crate::control::{CellState, ControlError, ControlResponse, daemon_absent};

/// How long a launch waits for another launch's start: the start's own
/// bounds, 160 seconds for the services and the 250 seconds of a readiness
/// request.
pub const START_LOCK_TIMEOUT: Duration = Duration::from_secs(160 + 250);

const LOCK_FILE: &str = "launch-start.lock";
const LOCK_POLL: Duration = Duration::from_millis(100);

/// Whether a launch found Droidloom stopped, so that starting it can help.
/// Any other answer or failure is the launch's own result.
#[must_use]
pub fn found_stopped(attempt: &Result<ControlResponse, ControlError>) -> bool {
    match attempt {
        // The session's unit stops droidloomd with Android, so a stopped
        // Droidloom has no daemon to answer.
        Err(error) => daemon_absent(error),
        // The daemon runs without a cell: the unit is between starting the
        // two, or the cell has exited.
        Ok(response) => !response.ok && response.state == CellState::Stopped,
    }
}

/// Launches with `attempt`, starting Droidloom with `start` first when the
/// launch finds it stopped.
///
/// A running cell costs one `attempt` and nothing else. Otherwise `exclusive`
/// admits one launch at a time: the first starts Droidloom, and the ones that
/// waited for it find Droidloom running and only launch. `start` returns once
/// Android is ready and runs at most once; the answer of the launch after it
/// is final, whatever it is.
///
/// # Errors
/// Returns the failures of `exclusive` and `start`, and the transport failure
/// of the launch that was final.
pub fn launch<Guard, Failure: From<ControlError>>(
    attempt: &mut dyn FnMut() -> Result<ControlResponse, ControlError>,
    exclusive: &mut dyn FnMut() -> Result<Guard, Failure>,
    start: &mut dyn FnMut() -> Result<(), Failure>,
) -> Result<ControlResponse, Failure> {
    let first = attempt();
    if !found_stopped(&first) {
        return Ok(first?);
    }
    let guard = exclusive()?;
    // Another launch may have started Droidloom while this one waited.
    let waited = attempt();
    if !found_stopped(&waited) {
        return Ok(waited?);
    }
    start()?;
    drop(guard);
    Ok(attempt()?)
}

/// Admits one starting launch at a time in a session. Dropping it admits the
/// next; the kernel drops it for a holder that dies.
#[derive(Debug)]
pub struct StartLock {
    _file: fs::File,
}

impl StartLock {
    /// Takes the lock in `directory`, the session's private Droidloom runtime
    /// directory. `waiting` is called once if another launch holds it.
    ///
    /// # Errors
    /// Returns the failure to open or lock the file, and `TimedOut` when the
    /// holder keeps the lock longer than `timeout`.
    pub fn acquire(
        directory: &Path,
        timeout: Duration,
        waiting: &mut dyn FnMut(),
    ) -> io::Result<Self> {
        let file = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(directory.join(LOCK_FILE))?;
        let deadline = Instant::now() + timeout;
        let mut told = false;
        loop {
            // SAFETY: file owns the descriptor throughout the call.
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                return Ok(Self { _file: file });
            }
            let error = io::Error::last_os_error();
            match error.kind() {
                io::ErrorKind::WouldBlock => {}
                io::ErrorKind::Interrupted => continue,
                _ => return Err(error),
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    format!(
                        "another launch has been starting Droidloom for more than {} seconds",
                        timeout.as_secs()
                    ),
                ));
            }
            if !told {
                waiting();
                told = true;
            }
            thread::sleep(LOCK_POLL);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::{ControlRequest, request};
    use std::cell::{Cell, RefCell};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

    type Failure = Box<dyn std::error::Error>;
    type Attempt = Result<ControlResponse, ControlError>;

    fn answer(ok: bool, state: &str, message: &str) -> Attempt {
        Ok(serde_json::from_value(serde_json::json!({
            "ok": ok,
            "state": state,
            "message": message,
        }))?)
    }

    fn launched() -> Attempt {
        answer(true, "running", "launched org.example")
    }

    fn no_cell() -> Attempt {
        answer(
            false,
            "stopped",
            "Droidloom's boot-managed Android cell is not running",
        )
    }

    /// The failure of a real request to a socket nobody has bound.
    fn no_daemon() -> Attempt {
        let directory = tempfile::tempdir().unwrap();
        request(
            &directory.path().join("control.sock"),
            &ControlRequest::Status,
        )
    }

    /// Runs `launch` over scripted attempts, recording what it does.
    fn run(
        attempts: Vec<Attempt>,
        exclusive: Result<(), &'static str>,
        start: Result<(), &'static str>,
    ) -> (Result<ControlResponse, Failure>, String) {
        let steps = RefCell::new(Vec::new());
        let mut attempts = attempts.into_iter();
        let result = launch(
            &mut || {
                steps.borrow_mut().push("launch");
                attempts.next().expect("no more launches than scripted")
            },
            &mut || {
                steps.borrow_mut().push("lock");
                exclusive.map_err(Failure::from)
            },
            &mut || {
                steps.borrow_mut().push("start");
                start.map_err(Failure::from)
            },
        );
        assert!(attempts.next().is_none(), "a scripted launch was not made");
        (result, steps.into_inner().join(" "))
    }

    #[test]
    fn only_a_missing_daemon_or_cell_asks_for_a_start() {
        assert!(found_stopped(&no_daemon()));
        assert!(found_stopped(&no_cell()));

        // A daemon that died without removing its socket refuses connections.
        let directory = tempfile::tempdir().unwrap();
        let socket = directory.path().join("control.sock");
        drop(UnixListener::bind(&socket).unwrap());
        let refused = request(&socket, &ControlRequest::Status);
        assert!(refused.is_err());
        assert!(found_stopped(&refused));

        assert!(!found_stopped(&launched()));
        // Android answered: an unknown package, a boot that stalled.
        assert!(!found_stopped(&answer(
            false,
            "running",
            "Android looks stuck"
        )));
        // A stopped cell that answered a request other than a failed launch.
        assert!(!found_stopped(&answer(true, "stopped", "cell is stopped")));
        // A busy or broken daemon is not a stopped one.
        for kind in [
            io::ErrorKind::WouldBlock,
            io::ErrorKind::PermissionDenied,
            io::ErrorKind::TimedOut,
        ] {
            assert!(!found_stopped(&Err(ControlError::Io {
                context: "connect to droidloomd".into(),
                source: kind.into(),
            })));
        }
        assert!(!found_stopped(&Err(ControlError::Io {
            context: "read lifecycle response".into(),
            source: io::ErrorKind::NotFound.into(),
        })));
        assert!(!found_stopped(&Err(ControlError::Invalid(
            "Droidloom did not finish within 250 seconds".into()
        ))));
    }

    #[test]
    fn a_running_cell_is_launched_once_without_lock_or_start() {
        let (result, steps) = run(vec![launched()], Ok(()), Ok(()));
        assert_eq!(result.unwrap().message, "launched org.example");
        assert_eq!(steps, "launch");

        // Android's refusal is the result; it is not launched a second time.
        let refused = answer(false, "running", "Activity class does not exist");
        let (result, steps) = run(vec![refused], Ok(()), Ok(()));
        let response = result.unwrap();
        assert!(!response.ok);
        assert_eq!(response.message, "Activity class does not exist");
        assert_eq!(steps, "launch");

        let broken = Err(ControlError::Invalid(
            "lifecycle response is too large".into(),
        ));
        let (result, steps) = run(vec![broken], Ok(()), Ok(()));
        assert_eq!(
            result.unwrap_err().to_string(),
            "lifecycle response is too large"
        );
        assert_eq!(steps, "launch");
    }

    #[test]
    fn a_stopped_droidloom_is_started_once_and_then_launched() {
        for stopped in [no_daemon, no_cell] {
            let (result, steps) = run(vec![stopped(), stopped(), launched()], Ok(()), Ok(()));
            let response = result.unwrap();
            assert!(response.ok);
            assert_eq!(response.state, CellState::Running);
            assert_eq!(steps, "launch lock launch start launch");
        }
    }

    #[test]
    fn a_launch_that_waited_for_another_start_only_launches() {
        let (result, steps) = run(vec![no_daemon(), launched()], Ok(()), Ok(()));
        assert!(result.unwrap().ok);
        assert_eq!(steps, "launch lock launch");

        // The other launch started Android, and Android refused this one.
        let refused = answer(false, "running", "Activity class does not exist");
        let (result, steps) = run(vec![no_cell(), refused], Ok(()), Ok(()));
        assert!(!result.unwrap().ok);
        assert_eq!(steps, "launch lock launch");
    }

    #[test]
    fn a_failed_start_fails_the_launch_without_launching_again() {
        let (result, steps) = run(
            vec![no_daemon(), no_daemon()],
            Ok(()),
            Err("start failed; see journalctl --user -u droidloom.service"),
        );
        assert_eq!(
            result.unwrap_err().to_string(),
            "start failed; see journalctl --user -u droidloom.service"
        );
        assert_eq!(steps, "launch lock launch start");

        let (result, steps) = run(vec![no_daemon()], Err("Droidloom is not set up"), Ok(()));
        assert_eq!(result.unwrap_err().to_string(), "Droidloom is not set up");
        assert_eq!(steps, "launch lock");
    }

    #[test]
    fn a_droidloom_still_stopped_after_its_start_is_not_started_again() {
        let (result, steps) = run(vec![no_daemon(), no_daemon(), no_cell()], Ok(()), Ok(()));
        let response = result.unwrap();
        assert!(!response.ok);
        assert!(response.message.contains("not running"));
        assert_eq!(steps, "launch lock launch start launch");

        let (result, steps) = run(vec![no_cell(), no_cell(), no_daemon()], Ok(()), Ok(()));
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("connect to droidloomd")
        );
        assert_eq!(steps, "launch lock launch start launch");
    }

    #[test]
    fn two_launches_at_once_start_droidloom_once_and_both_launch() {
        let directory = tempfile::tempdir().unwrap();
        let running = AtomicBool::new(false);
        let starts = AtomicU32::new(0);
        let waits = AtomicU32::new(0);
        let launches = AtomicU32::new(0);
        let one = || {
            launch::<StartLock, Failure>(
                &mut || {
                    if running.load(Ordering::SeqCst) {
                        launches.fetch_add(1, Ordering::SeqCst);
                        launched()
                    } else {
                        no_daemon()
                    }
                },
                &mut || {
                    Ok(StartLock::acquire(
                        directory.path(),
                        Duration::from_secs(30),
                        &mut || {
                            waits.fetch_add(1, Ordering::SeqCst);
                        },
                    )?)
                },
                &mut || {
                    starts.fetch_add(1, Ordering::SeqCst);
                    // Long enough for the other launch to find the lock held.
                    thread::sleep(Duration::from_millis(400));
                    running.store(true, Ordering::SeqCst);
                    Ok(())
                },
            )
            .map(|response| response.ok)
            .map_err(|error| error.to_string())
        };
        let (first, second) = thread::scope(|scope| {
            let first = scope.spawn(one);
            let second = scope.spawn(one);
            (first.join().unwrap(), second.join().unwrap())
        });
        assert_eq!(first, Ok(true));
        assert_eq!(second, Ok(true));
        assert_eq!(starts.load(Ordering::SeqCst), 1);
        assert_eq!(waits.load(Ordering::SeqCst), 1);
        assert_eq!(launches.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn a_held_start_lock_is_waited_for_and_then_given_up() {
        let directory = tempfile::tempdir().unwrap();
        let told = Cell::new(0);
        let mut waiting = || told.set(told.get() + 1);
        let held = StartLock::acquire(directory.path(), Duration::ZERO, &mut waiting).unwrap();
        assert_eq!(told.get(), 0);

        let error = StartLock::acquire(directory.path(), Duration::from_millis(250), &mut waiting)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert!(error.to_string().contains("another launch"));
        assert_eq!(told.get(), 1);

        drop(held);
        StartLock::acquire(directory.path(), Duration::ZERO, &mut waiting).unwrap();
        assert_eq!(told.get(), 1);

        // The lock is a file of the person's own in the private directory,
        // never one a link there points at.
        fs::remove_file(directory.path().join(LOCK_FILE)).unwrap();
        let target = directory.path().join("elsewhere");
        fs::write(&target, b"").unwrap();
        std::os::unix::fs::symlink(&target, directory.path().join(LOCK_FILE)).unwrap();
        assert!(StartLock::acquire(directory.path(), Duration::ZERO, &mut waiting).is_err());
    }
}
