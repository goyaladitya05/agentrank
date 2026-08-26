"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import styles from "./nav.module.css";

/**
 * The merchant navigation. Deliberately four entries.
 *
 * Everything else a merchant does, importing a store, preparing an evaluation, measuring
 * again, is reached contextually from these pages, so the navigation answers the four
 * questions a merchant actually returns for: how am I doing, what is wrong, what can I
 * approve, and what has happened. The technical surfaces live under /lab, behind the
 * separate link the layout renders beside sign out.
 */
const LINKS = [
  { href: "/overview", label: "Overview" },
  { href: "/issues", label: "Issues" },
  { href: "/fixes", label: "Fixes" },
  { href: "/history", label: "History" },
];

export function NavLinks() {
  const pathname = usePathname();
  return (
    <nav className={styles.nav} aria-label="Console">
      {LINKS.map((link) => {
        const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
        return (
          <Link
            key={link.href}
            href={link.href}
            className={styles.link}
            aria-current={active ? "page" : undefined}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
