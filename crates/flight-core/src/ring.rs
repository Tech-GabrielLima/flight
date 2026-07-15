use std::cell::UnsafeCell;
use std::sync::atomic::{AtomicUsize, Ordering};

use flight_format::Event;

pub struct Ring {
    buf: Box<[UnsafeCell<Event>]>,

    head: AtomicUsize,
    mask: usize,
}

unsafe impl Sync for Ring {}
unsafe impl Send for Ring {}

impl Ring {
    pub fn new(cap: usize) -> Ring {
        let cap = cap.max(16).next_power_of_two();
        let zero = Event {
            kind: 0,
            thread: 0,
            line: 0,
            code_id: 0,
            tstamp: 0,
        };
        let buf: Vec<UnsafeCell<Event>> = (0..cap).map(|_| UnsafeCell::new(zero)).collect();
        Ring {
            buf: buf.into_boxed_slice(),
            head: AtomicUsize::new(0),
            mask: cap - 1,
        }
    }

    #[allow(dead_code)]
    pub fn capacity(&self) -> usize {
        self.buf.len()
    }

    #[allow(dead_code)]
    pub fn pushed(&self) -> usize {
        self.head.load(Ordering::Relaxed)
    }

    #[inline(always)]
    pub fn push(&self, e: Event) {
        let i = self.head.fetch_add(1, Ordering::Relaxed) & self.mask;

        unsafe { *self.buf[i].get() = e };
    }

    pub fn drain(&self) -> (Vec<Event>, bool) {
        let head = self.head.load(Ordering::Relaxed);
        let cap = self.buf.len();
        let wrapped = head > cap;
        let start = if wrapped { head - cap } else { 0 };
        let mut out = Vec::with_capacity(head - start);
        for i in start..head {
            out.push(unsafe { *self.buf[i & self.mask].get() });
        }
        (out, wrapped)
    }

    #[allow(dead_code)]
    pub fn clear(&self) {
        self.head.store(0, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flight_format::EventKind;

    fn ev(t: u64) -> Event {
        Event::new(EventKind::Line, 0, t as u32, 1, t)
    }

    #[test]
    fn capacity_rounds_up_to_power_of_two() {
        assert_eq!(Ring::new(0).capacity(), 16);
        assert_eq!(Ring::new(1000).capacity(), 1024);
        assert_eq!(Ring::new(4096).capacity(), 4096);
    }

    #[test]
    fn drain_before_wrap_returns_everything_in_order() {
        let r = Ring::new(64);
        for t in 0..10 {
            r.push(ev(t));
        }
        let (events, wrapped) = r.drain();
        assert!(!wrapped);
        assert_eq!(events.len(), 10);
        assert!(events.windows(2).all(|w| w[0].tstamp < w[1].tstamp));
    }

    #[test]
    fn drain_after_wrap_returns_last_capacity_events_in_order() {
        let r = Ring::new(16);
        for t in 0..100 {
            r.push(ev(t));
        }
        let (events, wrapped) = r.drain();
        assert!(wrapped);
        assert_eq!(events.len(), 16);
        assert_eq!(events.first().unwrap().tstamp, 84);
        assert_eq!(events.last().unwrap().tstamp, 99);
        assert!(events.windows(2).all(|w| w[0].tstamp + 1 == w[1].tstamp));
    }

    #[test]
    fn clear_empties_the_ring() {
        let r = Ring::new(16);
        for t in 0..5 {
            r.push(ev(t));
        }
        r.clear();
        let (events, wrapped) = r.drain();
        assert!(events.is_empty());
        assert!(!wrapped);
    }

    #[test]
    fn one_ring_per_thread_pattern_merges_by_tstamp() {
        use std::sync::atomic::AtomicU64;
        use std::sync::Arc;

        let clock = Arc::new(AtomicU64::new(0));
        let rings: Vec<Arc<Ring>> = (0..4).map(|_| Arc::new(Ring::new(1024))).collect();
        let mut handles = Vec::new();
        for (tid, ring) in rings.iter().enumerate() {
            let ring = Arc::clone(ring);
            let clock = Arc::clone(&clock);
            handles.push(std::thread::spawn(move || {
                for _ in 0..500 {
                    let t = clock.fetch_add(1, Ordering::Relaxed);
                    ring.push(Event::new(EventKind::Line, tid as u16, 1, 1, t));
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        let mut all: Vec<Event> = Vec::new();
        for r in &rings {
            all.extend(r.drain().0);
        }
        all.sort_by_key(|e| e.tstamp);
        assert_eq!(all.len(), 2000);

        assert!(all.windows(2).all(|w| w[0].tstamp + 1 == w[1].tstamp));

        let threads: std::collections::HashSet<u16> = all.iter().map(|e| e.thread).collect();
        assert_eq!(threads.len(), 4);
    }
}

#[cfg(test)]
mod ring_ext {
    use super::*;
    use flight_format::EventKind;
    use std::sync::atomic::AtomicU64;
    use std::sync::Arc;

    fn ev(t: u64) -> Event {
        Event::new(EventKind::Line, 0, t as u32, 1, t)
    }

    #[test]
    fn capacity_min_is_sixteen() {
        assert_eq!(Ring::new(0).capacity(), 16);
        assert_eq!(Ring::new(1).capacity(), 16);
        assert_eq!(Ring::new(15).capacity(), 16);
        assert_eq!(Ring::new(16).capacity(), 16);
    }

    #[test]
    fn capacity_rounds_up_to_next_power_of_two() {
        assert_eq!(Ring::new(17).capacity(), 32);
        assert_eq!(Ring::new(33).capacity(), 64);
        assert_eq!(Ring::new(1023).capacity(), 1024);
        assert_eq!(Ring::new(1025).capacity(), 2048);
    }

    #[test]
    fn capacity_exact_power_of_two_is_unchanged() {
        for p in [16usize, 32, 64, 128, 256, 512, 1024, 65536] {
            assert_eq!(Ring::new(p).capacity(), p);
        }
    }

    #[test]
    fn pushed_starts_at_zero() {
        assert_eq!(Ring::new(64).pushed(), 0);
    }

    #[test]
    fn pushed_counts_every_push_even_past_capacity() {
        let r = Ring::new(16);
        for t in 0..100 {
            r.push(ev(t));
        }
        assert_eq!(r.pushed(), 100);
    }

    #[test]
    fn drain_empty_ring_is_empty_and_unwrapped() {
        let (evs, wrapped) = Ring::new(16).drain();
        assert!(evs.is_empty());
        assert!(!wrapped);
    }

    #[test]
    fn drain_single_event() {
        let r = Ring::new(16);
        r.push(ev(42));
        let (evs, wrapped) = r.drain();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].tstamp, 42);
        assert!(!wrapped);
    }

    #[test]
    fn drain_partial_fill_preserves_order() {
        let r = Ring::new(64);
        for t in 0..30 {
            r.push(ev(t));
        }
        let (evs, wrapped) = r.drain();
        assert!(!wrapped);
        assert_eq!(evs.len(), 30);
        for (i, e) in evs.iter().enumerate() {
            assert_eq!(e.tstamp, i as u64);
        }
    }

    #[test]
    fn drain_exactly_full_does_not_wrap() {
        let r = Ring::new(16);
        for t in 0..16 {
            r.push(ev(t));
        }
        let (evs, wrapped) = r.drain();
        assert!(!wrapped, "head == cap is not yet wrapped");
        assert_eq!(evs.len(), 16);
        assert_eq!(evs.first().unwrap().tstamp, 0);
        assert_eq!(evs.last().unwrap().tstamp, 15);
    }

    #[test]
    fn one_past_full_wraps_and_keeps_tail() {
        let r = Ring::new(16);
        for t in 0..17 {
            r.push(ev(t));
        }
        let (evs, wrapped) = r.drain();
        assert!(wrapped);
        assert_eq!(evs.len(), 16);
        assert_eq!(evs.first().unwrap().tstamp, 1);
        assert_eq!(evs.last().unwrap().tstamp, 16);
    }

    #[test]
    fn far_past_full_keeps_only_last_capacity_events() {
        let r = Ring::new(32);
        for t in 0..1000 {
            r.push(ev(t));
        }
        let (evs, wrapped) = r.drain();
        assert!(wrapped);
        assert_eq!(evs.len(), 32);
        assert_eq!(evs.first().unwrap().tstamp, 968);
        assert_eq!(evs.last().unwrap().tstamp, 999);
        assert!(evs.windows(2).all(|w| w[0].tstamp + 1 == w[1].tstamp));
    }

    #[test]
    fn drain_is_idempotent_and_nondestructive() {
        let r = Ring::new(16);
        for t in 0..10 {
            r.push(ev(t));
        }
        let a = r.drain();
        let b = r.drain();
        assert_eq!(a.0, b.0);
        assert_eq!(a.1, b.1);
    }

    #[test]
    fn clear_resets_pushed_and_drain() {
        let r = Ring::new(16);
        for t in 0..40 {
            r.push(ev(t));
        }
        r.clear();
        assert_eq!(r.pushed(), 0);
        let (evs, wrapped) = r.drain();
        assert!(evs.is_empty());
        assert!(!wrapped);
    }

    #[test]
    fn push_after_clear_starts_fresh() {
        let r = Ring::new(16);
        for t in 0..40 {
            r.push(ev(t));
        }
        r.clear();
        r.push(ev(777));
        let (evs, wrapped) = r.drain();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].tstamp, 777);
        assert!(!wrapped);
    }

    #[test]
    fn drain_preserves_all_event_fields() {
        let r = Ring::new(16);
        r.push(Event::new(EventKind::Raise, 5, 123, 0xDEAD, 9));
        let (evs, _) = r.drain();
        let e = evs[0];
        assert_eq!(e.kind, EventKind::Raise as u8);
        assert_eq!(e.thread, 5);
        assert_eq!(e.line, 123);
        assert_eq!(e.code_id, 0xDEAD);
        assert_eq!(e.tstamp, 9);
    }

    #[test]
    fn many_threads_each_own_ring_merge_is_dense() {
        let clock = Arc::new(AtomicU64::new(0));
        let rings: Vec<Arc<Ring>> = (0..8).map(|_| Arc::new(Ring::new(4096))).collect();
        let mut handles = Vec::new();
        for (tid, ring) in rings.iter().enumerate() {
            let ring = Arc::clone(ring);
            let clock = Arc::clone(&clock);
            handles.push(std::thread::spawn(move || {
                for _ in 0..250 {
                    let t = clock.fetch_add(1, Ordering::Relaxed);
                    ring.push(Event::new(EventKind::Line, tid as u16, 1, 1, t));
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        let mut all: Vec<Event> = Vec::new();
        for r in &rings {
            all.extend(r.drain().0);
        }
        all.sort_by_key(|e| e.tstamp);
        assert_eq!(all.len(), 8 * 250);
        assert!(all.windows(2).all(|w| w[0].tstamp + 1 == w[1].tstamp));
        let threads: std::collections::HashSet<u16> = all.iter().map(|e| e.thread).collect();
        assert_eq!(threads.len(), 8);
    }

    #[test]
    fn ring_is_send_across_thread_boundary() {
        let r = Ring::new(16);
        r.push(ev(1));
        let handle = std::thread::spawn(move || {
            r.push(ev(2));
            r.drain().0.len()
        });
        assert_eq!(handle.join().unwrap(), 2);
    }
}
