import type { ITheme } from "@xterm/xterm";

const readToken = (root: HTMLElement, name: string) => {
  const value = (
    root === document.documentElement
      ? getComputedStyle(root).getPropertyValue(name)
      : root.style.getPropertyValue(name)
  ).trim();
  if (!value) throw new Error(`Missing xterm theme token: ${name}`);
  return value;
};

export const createXtermTheme = (
  root: HTMLElement = document.documentElement,
): ITheme => ({
  background: readToken(root, "--color-app"),
  foreground: readToken(root, "--color-ink"),
  cursor: readToken(root, "--color-accent"),
  selectionBackground: readToken(root, "--color-accent-soft"),
});
