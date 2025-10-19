import { type ClassValue, clsx } from "clsx";
import { Metadata } from "next";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const AVATAR_GRADIENTS = [
  "from-sky-500 via-cyan-500 to-emerald-500",
  "from-violet-500 via-purple-500 to-fuchsia-500",
  "from-amber-500 via-orange-500 to-rose-500",
  "from-blue-600 via-indigo-500 to-purple-500",
  "from-emerald-500 via-teal-500 to-sky-500",
  "from-rose-500 via-pink-500 to-purple-500"
] as const;

function hashString(value: string) {
  return Array.from(value).reduce((acc, char) => acc + char.charCodeAt(0), 0);
}

export function generateAvatarFallback(string: string) {
  const names = string.split(" ").filter((name: string) => name);
  const mapped = names.map((name: string) => name.charAt(0).toUpperCase());

  return mapped.join("");
}

export function getAvatarGradient(value: string) {
  if (!value) {
    return AVATAR_GRADIENTS[0];
  }

  const index = Math.abs(hashString(value)) % AVATAR_GRADIENTS.length;

  return AVATAR_GRADIENTS[index];
}

export function generateMeta({
  title,
  description,
  canonical
}: {
  title: string;
  description: string;
  canonical: string;
}): Metadata {
  return {
    title: `${title} - Shadcn UI Kit Free`,
    description: description,
    openGraph: {
      title,
      description
    }
  };
}
