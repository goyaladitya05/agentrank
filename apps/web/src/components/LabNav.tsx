"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import styles from "@/app/(app)/lab/lab.module.css";

const LINKS = [
  { href: "/lab", label: "Lab" },
  { href: "/lab/runs", label: "Benchmark runs" },
  { href: "/lab/compiler", label: "Compiler runs" },
  { href: "/lab/status", label: "System status" },
];

export function LabNav() {
  const pathname = usePathname();
  return (
    <nav className={styles.nav} aria-label="Lab">
      {LINKS.map((link) => {
        const active =
          link.href === "/lab"
            ? pathname === "/lab"
            : pathname === link.href || pathname.startsWith(`${link.href}/`);
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
