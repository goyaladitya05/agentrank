import styles from "@/components/console.module.css";

/** Restrained static loading state. No spinners, no animation, no layout shift games. */
export default function ConsoleLoading() {
  return (
    <div className={styles.loadingRegion} aria-busy="true">
      <p className={styles.visuallyHidden}>Loading</p>
      <div className={`${styles.skeletonLine} ${styles.skeletonLineShort}`} />
      <div className={styles.skeletonPanel} />
      <div className={styles.skeletonLineMedium} />
      <div className={styles.skeletonPanel} />
    </div>
  );
}
