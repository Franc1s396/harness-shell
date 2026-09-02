use std::{
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

use tokio::sync::mpsc::UnboundedSender;

use crate::runtime::RuntimeStatus;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeControl {
    Shutdown,
}

#[derive(Clone)]
pub struct RuntimeStateHandle {
    status: Arc<Mutex<RuntimeStatus>>,
    control: Arc<Mutex<Option<UnboundedSender<RuntimeControl>>>>,
}

impl RuntimeStateHandle {
    pub fn new(status: RuntimeStatus) -> Self {
        Self {
            status: Arc::new(Mutex::new(status)),
            control: Arc::new(Mutex::new(None)),
        }
    }

    pub fn status(&self) -> RuntimeStatus {
        self.status
            .lock()
            .expect("runtime status lock poisoned")
            .clone()
    }

    pub(crate) fn publish(&self, status: RuntimeStatus) {
        *self.status.lock().expect("runtime status lock poisoned") = status;
    }

    pub(crate) fn attach_control(&self, sender: UnboundedSender<RuntimeControl>) {
        *self.control.lock().expect("runtime control lock poisoned") = Some(sender);
    }

    pub fn request_shutdown(&self) {
        if let Some(sender) = self
            .control
            .lock()
            .expect("runtime control lock poisoned")
            .as_ref()
        {
            let _ = sender.send(RuntimeControl::Shutdown);
        }
    }

    pub fn wait_until_stopped(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.status().state == crate::runtime::RuntimeState::Stopped {
                return true;
            }
            thread::sleep(Duration::from_millis(25));
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shutdown_request_wakes_async_control_receiver() {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("build runtime")
            .block_on(async {
                let state = RuntimeStateHandle::new(RuntimeStatus::starting("test"));
                let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
                state.attach_control(sender);

                state.request_shutdown();

                assert_eq!(receiver.recv().await, Some(RuntimeControl::Shutdown));
            });
    }
}
