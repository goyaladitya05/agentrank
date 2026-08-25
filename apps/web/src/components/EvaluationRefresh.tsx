"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { REFRESH_SECONDS } from "@/lib/evaluation-refresh";

/**
 * Re-reading a launch that has not settled yet.
 *
 * The framework's own refresh on a fixed interval, and nothing else. There is no progress bar,
 * no estimated finish and no simulated movement: what the page shows is what the server said
 * when it last answered, and this only asks again. It stops as soon as the launch settles, so a
 * finished page makes no further requests.
 */
export function EvaluationRefresh({ active }: { active: boolean }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = setInterval(() => {
      router.refresh();
    }, REFRESH_SECONDS * 1000);
    return () => {
      clearInterval(timer);
    };
  }, [active, router]);
  return null;
}
