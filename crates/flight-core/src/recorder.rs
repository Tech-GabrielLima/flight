use std::cell::Cell;
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use flight_format::{CodeInfo, Event, EventKind, RingPayload};

use crate::ring::Ring;

/// A minimal hasher for our integer keys (code ids are pointers, thread ids are
/// small). The default `HashMap` uses SipHash — DoS-resistant but ~15 ns per
/// lookup, which shows up on the per-event hot path. Fibonacci mixing spreads
/// aligned pointers across buckets in ~1 ns and needs no external crate.
#[derive(Default)]
struct IntHasher(u64);

impl Hasher for IntHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write_u64(&mut self, n: u64) {
        self.0 = n.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    }
    #[inline]
    fn write_u16(&mut self, n: u16) {
        self.0 = (n as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    }
    fn write(&mut self, bytes: &[u8]) {
        // Not used for our integer keys, but kept correct as a fallback.
        for &b in bytes {
            self.0 = (self.0 ^ b as u64).wrapping_mul(0x0100_0000_01B3);
        }
    }
}

type IntMap<K, V> = HashMap<K, V, BuildHasherDefault<IntHasher>>;

/// A per-thread cache of the current recorder's ring, so the hot path takes no
/// lock at all. Keyed by `(recorder id, generation)`: a different recorder or a
/// `reset()` (which bumps the generation) invalidates the cache, and — crucially
/// — the stale ring pointer is *never dereferenced* because the generation check
/// happens first (so `reset()` freeing the ring is safe).
#[derive(Clone, Copy)]
struct Slot {
    rec_id: u64,
    generation: u64,
    thread: u16,
    ring: *const Ring,
}

thread_local! {
    static SLOT: Cell<Option<Slot>> = const { Cell::new(None) };
}

static NEXT_RECORDER_ID: AtomicU64 = AtomicU64::new(1);

/// The process-wide recorder: a global logical clock, one [`Ring`] per OS
/// thread, a side map from `code_id` to [`CodeInfo`], and the interesting/deny
/// policy cache.
///
/// The hot path (`record`) is lock-free in the common case: it stamps a logical
/// time (one atomic add) and pushes 24 bytes into a thread-local ring found
/// through the [`Slot`] cache. Locks are taken only when a *new* thread first
/// records, when a *new* code object first appears, or on drain/reset.
pub struct Recorder {
    id: u64,
    /// Bumped by `reset()`; invalidates every thread's [`Slot`] cache.
    generation: AtomicU64,
    clock: AtomicU64,
    ring_cap: usize,
    next_thread: AtomicU64,
    rings: Mutex<IntMap<u16, Box<Ring>>>,
    codes: Mutex<IntMap<u64, CodeInfo>>,
    /// `code_id -> is this code interesting to record?` (computed once).
    interesting: Mutex<IntMap<u64, bool>>,
    /// Path prefixes whose code is *not* recorded (stdlib, site-packages…).
    deny: Mutex<Vec<String>>,
    /// Substrings that force-include a path even under a denied prefix.
    force: Mutex<Vec<String>>,
}

impl Recorder {
    pub fn new(ring_cap: usize) -> Recorder {
        Recorder {
            id: NEXT_RECORDER_ID.fetch_add(1, Ordering::Relaxed),
            generation: AtomicU64::new(1),
            clock: AtomicU64::new(0),
            ring_cap,
            next_thread: AtomicU64::new(0),
            rings: Mutex::new(IntMap::default()),
            codes: Mutex::new(IntMap::default()),
            interesting: Mutex::new(IntMap::default()),
            deny: Mutex::new(Vec::new()),
            force: Mutex::new(Vec::new()),
        }
    }

    /// Assign this OS thread a compact id and ring (slow path, once per thread
    /// per generation). Returns the id and a pointer valid until `reset()`.
    fn make_thread_ring(&self) -> (u16, *const Ring) {
        let tid = (self.next_thread.fetch_add(1, Ordering::Relaxed) & 0xFFFF) as u16;
        let mut rings = self.rings.lock().unwrap();
        let ring = rings
            .entry(tid)
            .or_insert_with(|| Box::new(Ring::new(self.ring_cap)));
        (tid, &**ring as *const Ring)
    }

    /// Record one execution event. The hot path — no lock in the common case.
    ///
    /// `line` is meaningful only for LINE events; pass 0 otherwise.
    #[inline]
    pub fn record(&self, kind: EventKind, code_id: u64, line: u32) {
        let tstamp = self.clock.fetch_add(1, Ordering::Relaxed);
        let generation = self.generation.load(Ordering::Relaxed);
        SLOT.with(|cell| {
            let slot = match cell.get() {
                Some(s) if s.rec_id == self.id && s.generation == generation => s,
                _ => {
                    let (thread, ring) = self.make_thread_ring();
                    let s = Slot {
                        rec_id: self.id,
                        generation,
                        thread,
                        ring,
                    };
                    cell.set(Some(s));
                    s
                }
            };
            // SAFETY: `ring` points into a Box owned by `self.rings`; it is only
            // invalidated by `reset()`, which bumps the generation so the branch
            // above re-fetches before we ever dereference a freed pointer. Pushes
            // are single-writer per thread (this thread owns this ring).
            unsafe {
                (*slot.ring).push(Event::new(kind, slot.thread, line, code_id, tstamp));
            }
        });
    }

    // -- interesting / deny policy -----------------------------------------

    /// Replace the deny/force policy and clear the cached decisions.
    pub fn set_filter(&self, deny: Vec<String>, force: Vec<String>) {
        *self.deny.lock().unwrap() = deny;
        *self.force.lock().unwrap() = force;
        self.interesting.lock().unwrap().clear();
    }

    /// Cached interesting decision for a code id, if already computed.
    pub fn interesting_cached(&self, code_id: u64) -> Option<bool> {
        self.interesting.lock().unwrap().get(&code_id).copied()
    }

    /// Decide (and cache) whether code from `filename` is interesting. Mirrors
    /// the Python policy: synthetic files (`<...>`) and denied prefixes are out,
    /// unless force-included. Paths are canonicalized (like `realpath`) once.
    pub fn decide_interesting(&self, code_id: u64, filename: &str) -> bool {
        let value = self.compute_interesting(filename);
        self.interesting.lock().unwrap().insert(code_id, value);
        value
    }

    fn compute_interesting(&self, filename: &str) -> bool {
        if filename.is_empty() || filename.starts_with('<') {
            return false;
        }
        let real = std::fs::canonicalize(filename)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|_| filename.to_string());
        if self
            .force
            .lock()
            .unwrap()
            .iter()
            .any(|f| real.contains(f.as_str()))
        {
            return true;
        }
        !self
            .deny
            .lock()
            .unwrap()
            .iter()
            .any(|d| real.starts_with(d.as_str()))
    }

    /// Register the file/qualname of a code object the first time we see it.
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

    // -- introspection / drain ---------------------------------------------

    /// Current generation (bumped by `reset()`) — invalidates per-thread caches.
    pub fn generation(&self) -> u64 {
        self.generation.load(Ordering::Relaxed)
    }

    pub fn total_events(&self) -> u64 {
        self.clock.load(Ordering::Relaxed)
    }

    pub fn thread_count(&self) -> usize {
        self.rings.lock().unwrap().len()
    }

    pub fn code_count(&self) -> usize {
        self.codes.lock().unwrap().len()
    }

    /// Drain every thread's ring, merge by logical timestamp, and attach only
    /// the referenced code map entries. This is the EVENT_RING payload.
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

    /// Reset all state. Bumps the generation so stale thread-local ring
    /// pointers are re-fetched (never dereferenced) before the next push.
    pub fn reset(&self) {
        self.rings.lock().unwrap().clear();
        self.codes.lock().unwrap().clear();
        self.interesting.lock().unwrap().clear();
        self.clock.store(0, Ordering::Relaxed);
        self.next_thread.store(0, Ordering::Relaxed);
        self.generation.fetch_add(1, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn records_and_snapshots_in_logical_order() {
        let rec = Recorder::new(1024);
        rec.register_code(1, "app.py", "main", 1);
        rec.record(EventKind::PyStart, 1, 0);
        rec.record(EventKind::Line, 1, 10);
        rec.record(EventKind::Line, 1, 11);

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
        rec.record(EventKind::Line, 1, 5);
        let snap = rec.snapshot_ring();
        assert_eq!(snap.codes.len(), 1);
        assert!(snap.codes.contains_key(&1));
        assert!(!snap.codes.contains_key(&2));
    }

    #[test]
    fn distinct_os_threads_get_distinct_rings() {
        // Thread identity is the OS thread now: two real threads → two rings.
        let rec = Arc::new(Recorder::new(64));
        let mut handles = Vec::new();
        for _ in 0..2 {
            let r = Arc::clone(&rec);
            handles.push(std::thread::spawn(move || {
                r.record(EventKind::Line, 1, 1);
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(rec.thread_count(), 2);
        assert_eq!(rec.total_events(), 2);
    }

    #[test]
    fn reset_clears_everything() {
        let rec = Recorder::new(64);
        rec.register_code(1, "a.py", "f", 1);
        rec.record(EventKind::Line, 1, 1);
        rec.reset();
        assert_eq!(rec.total_events(), 0);
        assert_eq!(rec.thread_count(), 0);
        assert_eq!(rec.code_count(), 0);
        assert!(rec.snapshot_ring().events.is_empty());
        // Recording after reset works (slot is re-fetched, not a stale pointer).
        rec.record(EventKind::Line, 1, 2);
        assert_eq!(rec.total_events(), 1);
        assert_eq!(rec.snapshot_ring().events.len(), 1);
    }

    #[test]
    fn interesting_policy_denies_prefixes_and_synthetic_files() {
        let rec = Recorder::new(64);
        rec.set_filter(vec!["/usr/lib/python".to_string()], vec![]);
        assert!(!rec.compute_interesting("<string>"));
        assert!(!rec.compute_interesting(""));
        assert!(!rec.compute_interesting("/usr/lib/python3.13/json/__init__.py"));
        // A path that doesn't exist can't be canonicalized → uses the raw path;
        // not under a denied prefix → interesting.
        assert!(rec.compute_interesting("/home/me/project/app.py"));
    }

    #[test]
    fn interesting_decision_is_cached() {
        let rec = Recorder::new(64);
        rec.set_filter(vec![], vec![]);
        assert_eq!(rec.interesting_cached(42), None);
        rec.decide_interesting(42, "/home/me/app.py");
        assert_eq!(rec.interesting_cached(42), Some(true));
    }
}
