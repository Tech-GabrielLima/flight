use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use flight_format::{CodeInfo, Event, EventKind, RingPayload};

use crate::ring::Ring;

/// The process-wide recorder: a global logical clock, one [`Ring`] per thread,
/// and a side map from `code_id` to [`CodeInfo`].
///
/// It never touches disk and never allocates on the hot path (`record`). The
/// only cross-thread coordination is a `fetch_add` on the clock and a rarely
/// taken lock when a *new* thread or a *new* code object first appears.
pub struct Recorder {
    /// Global logical clock. One `fetch_add` per event orders everything.
    clock: AtomicU64,
    /// Capacity of each per-thread ring.
    ring_cap: usize,
    /// Per-thread rings, keyed by the flight-assigned compact thread id.
    rings: Mutex<HashMap<u16, Box<Ring>>>,
    /// Resolves `code_id` -> (file, qualname, first_line). Filled lazily on
    /// the first PY_START of each code object.
    codes: Mutex<HashMap<u64, CodeInfo>>,
    /// Maps OS/Python thread ids to compact u16 ids.
    thread_ids: Mutex<HashMap<u64, u16>>,
    next_thread_id: AtomicU64,
}

impl Recorder {
    pub fn new(ring_cap: usize) -> Recorder {
        Recorder {
            clock: AtomicU64::new(0),
            ring_cap,
            rings: Mutex::new(HashMap::new()),
            codes: Mutex::new(HashMap::new()),
            thread_ids: Mutex::new(HashMap::new()),
            next_thread_id: AtomicU64::new(0),
        }
    }

    /// Compact thread id for `os_thread_id`, assigning a fresh one on first
    /// sight. u16 is plenty (65k threads) and keeps [`Event`] at 24 bytes.
    fn compact_thread(&self, os_thread_id: u64) -> u16 {
        let mut ids = self.thread_ids.lock().unwrap();
        if let Some(&id) = ids.get(&os_thread_id) {
            return id;
        }
        let id = (self.next_thread_id.fetch_add(1, Ordering::Relaxed) & 0xFFFF) as u16;
        ids.insert(os_thread_id, id);
        id
    }

    /// Record one execution event. This is the hot path: it stamps a logical
    /// time, finds the thread's ring, and pushes 24 bytes.
    ///
    /// `line` is meaningful only for LINE events; pass 0 otherwise.
    pub fn record(&self, kind: EventKind, os_thread_id: u64, code_id: u64, line: u32) {
        let tstamp = self.clock.fetch_add(1, Ordering::Relaxed);
        let thread = self.compact_thread(os_thread_id);
        let event = Event::new(kind, thread, line, code_id, tstamp);
        // The lock here is uncontended in the common case and never held
        // across a push; a per-thread cached raw pointer is the phase-2
        // optimization, unnecessary while events only flow at PY_START/LINE.
        let mut rings = self.rings.lock().unwrap();
        let ring = rings
            .entry(thread)
            .or_insert_with(|| Box::new(Ring::new(self.ring_cap)));
        ring.push(event);
    }

    /// Register the file/qualname of a code object the first time we see it.
    /// Returns `true` if this was the first registration (the caller can then
    /// decide DISABLE-vs-keep on the Python side).
    pub fn register_code(&self, code_id: u64, file: &str, qualname: &str, first_line: u32) -> bool {
        let mut codes = self.codes.lock().unwrap();
        if codes.contains_key(&code_id) {
            return false;
        }
        codes.insert(
            code_id,
            CodeInfo {
                file: file.to_string(),
                qualname: qualname.to_string(),
                first_line,
            },
        );
        true
    }

    /// Total events recorded across all threads.
    pub fn total_events(&self) -> u64 {
        self.clock.load(Ordering::Relaxed)
    }

    /// Number of distinct threads that have recorded at least one event.
    pub fn thread_count(&self) -> usize {
        self.rings.lock().unwrap().len()
    }

    /// Number of distinct code objects registered.
    pub fn code_count(&self) -> usize {
        self.codes.lock().unwrap().len()
    }

    /// Drain every thread's ring, merge by logical timestamp, and attach only
    /// the code map entries actually referenced. This is the EVENT_RING
    /// payload written at crash time.
    pub fn snapshot_ring(&self) -> RingPayload {
        let mut events = Vec::new();
        let mut wrapped = false;
        {
            let rings = self.rings.lock().unwrap();
            for ring in rings.values() {
                let (mut evs, w) = ring.drain();
                wrapped |= w;
                events.append(&mut evs);
            }
        }
        events.sort_by_key(|e| e.tstamp);

        let codes = self.codes.lock().unwrap();
        let mut used = HashMap::new();
        for e in &events {
            if let Some(info) = codes.get(&e.code_id) {
                used.entry(e.code_id).or_insert_with(|| info.clone());
            }
        }
        RingPayload {
            codes: used,
            events,
            wrapped,
        }
    }

    /// Reset all state (rings, codes, thread map, clock). Used by tests and by
    /// `flight.uninstall()`.
    pub fn reset(&self) {
        self.rings.lock().unwrap().clear();
        self.codes.lock().unwrap().clear();
        self.thread_ids.lock().unwrap().clear();
        self.clock.store(0, Ordering::Relaxed);
        self.next_thread_id.store(0, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_and_snapshots_in_logical_order() {
        let rec = Recorder::new(1024);
        rec.register_code(1, "app.py", "main", 1);
        rec.record(EventKind::PyStart, 100, 1, 0);
        rec.record(EventKind::Line, 100, 1, 10);
        rec.record(EventKind::Line, 100, 1, 11);

        let snap = rec.snapshot_ring();
        assert_eq!(snap.events.len(), 3);
        assert!(!snap.wrapped);
        assert!(snap.events.windows(2).all(|w| w[0].tstamp < w[1].tstamp));
        assert_eq!(snap.codes.len(), 1);
        assert_eq!(snap.codes[&1].qualname, "main");
    }

    #[test]
    fn register_code_is_first_write_wins() {
        let rec = Recorder::new(64);
        assert!(rec.register_code(7, "a.py", "f", 1));
        assert!(!rec.register_code(7, "a.py", "f", 1));
        assert_eq!(rec.code_count(), 1);
    }

    #[test]
    fn snapshot_only_includes_referenced_codes() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "used", 1);
        rec.register_code(2, "b.py", "unused", 1);
        rec.record(EventKind::Line, 1, 1, 5);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.codes.len(), 1);
        assert!(snap.codes.contains_key(&1));
        assert!(!snap.codes.contains_key(&2));
    }

    #[test]
    fn distinct_threads_get_distinct_compact_ids() {
        let rec = Recorder::new(64);
        rec.record(EventKind::Line, 1000, 1, 1);
        rec.record(EventKind::Line, 2000, 1, 1);
        rec.record(EventKind::Line, 1000, 1, 1);
        assert_eq!(rec.thread_count(), 2);
        assert_eq!(rec.total_events(), 3);
    }

    #[test]
    fn reset_clears_everything() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "f", 1);
        rec.record(EventKind::Line, 1, 1, 1);
        rec.reset();
        assert_eq!(rec.total_events(), 0);
        assert_eq!(rec.thread_count(), 0);
        assert_eq!(rec.code_count(), 0);
        assert!(rec.snapshot_ring().events.is_empty());
    }
}
