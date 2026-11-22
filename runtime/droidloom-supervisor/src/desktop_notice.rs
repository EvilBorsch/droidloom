//! A desktop notification that a command keeps up to date: shown once, then
//! replaced in place as the command goes on, and ended with its result. It
//! goes through the session's notification service as the boot notice does;
//! without one nothing shows and the command is not held up.
use std::cell::Cell;

/// One notification; nothing is shown until `show`.
#[derive(Debug, Default)]
pub struct DesktopNotice {
    /// The service's ID for it; 0 while none was shown.
    id: Cell<u32>,
}

impl DesktopNotice {
    /// A notice not yet shown.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Shows `summary` and `body` in place of what this notice showed
    /// before, for `timeout_ms`; 0 keeps it until the person dismisses it.
    pub fn show(&self, summary: &str, body: &str, timeout_ms: u32) {
        if let Some(id) = crate::boot_notice::notify(self.id.get(), summary, body, timeout_ms) {
            self.id.set(id);
        }
    }
}
