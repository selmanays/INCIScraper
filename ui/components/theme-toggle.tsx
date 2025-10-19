"use client";

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type ThemeMode = "light" | "dark";

function getInitialTheme(): ThemeMode {
  if (typeof window === "undefined") {
    return "light";
  }
  const stored = window.localStorage.getItem("theme");
  if (stored === "light" || stored === "dark") {
    return stored;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function ThemeToggle({ className }: { className?: string }) {
  const [theme, setTheme] = useState<ThemeMode | null>(null);

  useEffect(() => {
    const initial = getInitialTheme();
    setTheme(initial);
    const root = window.document.documentElement;
    root.classList.toggle("dark", initial === "dark");
  }, []);

  useEffect(() => {
    if (!theme) {
      return;
    }
    const root = window.document.documentElement;
    root.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem("theme", theme);
  }, [theme]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = () => {
      const stored = window.localStorage.getItem("theme");
      if (stored === "light" || stored === "dark") {
        return;
      }
      setTheme(media.matches ? "dark" : "light");
    };
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", listener);
      return () => media.removeEventListener("change", listener);
    }
    media.addListener(listener);
    return () => media.removeListener(listener);
  }, []);

  function toggleTheme() {
    setTheme((current) => (current === "light" ? "dark" : "light"));
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className={cn("relative", className)}
      onClick={toggleTheme}
      aria-label="Tema değiştir"
    >
      <Sun
        className={cn(
          "h-5 w-5 transition-opacity",
          theme === "dark" ? "opacity-0" : "opacity-100",
        )}
      />
      <Moon
        className={cn(
          "absolute inset-0 m-auto h-5 w-5 transition-opacity",
          theme === "dark" ? "opacity-100" : "opacity-0",
        )}
      />
    </Button>
  );
}
