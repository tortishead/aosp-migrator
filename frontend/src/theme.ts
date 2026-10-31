import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

/** Also read by the inline script in index.html, which applies the theme before first paint. */
export const THEME_KEY = "aosp-migrator.theme";

const darkQuery = () =>
  typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function loadSaved(): Theme | null {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

function save(theme: Theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}

function systemTheme(): Theme {
  return darkQuery()?.matches ? "dark" : "light";
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

/**
 * Day or night. Follows the system preference until the user picks one,
 * then remembers that choice across visits.
 */
export function useTheme(): [Theme, () => void] {
  const [saved, setSaved] = useState<Theme | null>(loadSaved);
  const [system, setSystem] = useState<Theme>(systemTheme);
  const theme = saved ?? system;

  useEffect(() => {
    const query = darkQuery();
    if (!query) return;
    const onChange = () => setSystem(query.matches ? "dark" : "light");
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useEffect(() => applyTheme(theme), [theme]);

  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    save(next);
    setSaved(next);
  };
  return [theme, toggle];
}
