"use client";

// "The server is waking up": shown when a wait that is usually instant
// (restoring the session, logging in) has taken more than a few seconds.
//
// Why it happens (ADR 023): the backend runs on Render's free plan, which
// puts it to sleep after 15 minutes without requests and needs about a
// minute to wake it. The request still succeeds, since Vercel's proxy waits up
// to two minutes, but a page that only says "Checking your session…" for a
// minute looks broken. Decided with Dhruv instead of a service that pings
// the backend to keep it awake.

import { useEffect, useState } from "react";

// A warm backend answers in well under a second; 5 s means it was asleep.
export const SLOW_AFTER_MS = 5000;

/**
 * True once `active` has been true for `ms` without a break; false again
 * as soon as it isn't. The timer is the effect's: it starts when the wait
 * starts and is cleared when the wait ends (or the component goes away).
 */
export function useSlowAfter(active, ms = SLOW_AFTER_MS) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) {
      return undefined;
    }
    const timer = setTimeout(() => setSlow(true), ms);
    return () => {
      clearTimeout(timer);
      setSlow(false);
    };
  }, [active, ms]);
  return active && slow;
}

/** The hint itself, while `waiting` has lasted longer than SLOW_AFTER_MS. */
export function WakingUpHint({ waiting }) {
  const slow = useSlowAfter(waiting);
  if (!slow) {
    return null;
  }
  return (
    // role="status": announced when it appears, politely.
    <p role="status" className="text-muted text-sm">
      Kharcha&apos;s server sleeps when nobody is using it and is waking up. This can take up
      to a minute.
    </p>
  );
}
