import { cn } from "@/lib/utils";
import Link from "next/link";

type LogoProps = {
  className?: string;
};

export default function Logo({ className }: LogoProps) {
  return (
    <Link
      href="/dashboard/crm"
      className={cn("flex items-center gap-2 px-5 py-4 font-semibold", className)}
    >
      <span className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-to-br from-sky-500 via-purple-500 to-pink-500 text-sm font-bold text-white">
        IS
      </span>
      INCIScraper CRM
    </Link>
  );
}
