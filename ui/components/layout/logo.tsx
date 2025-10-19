import { cn } from "@/lib/utils";
import Link from "next/link";
import { Badge } from "../ui/badge";

type LogoProps = {
  className?: string;
};

export default function Logo({ className }: LogoProps) {
  return (
    <Link href="/" className={cn("flex items-center gap-2 px-5 py-4 font-bold", className)}>
      <span className="flex h-6 w-6 items-center justify-center rounded-md bg-gradient-to-br from-indigo-500 via-purple-500 to-pink-500 text-xs font-semibold text-white">
        UI
      </span>
      Shadcn UI Kit <Badge variant="outline">Free</Badge>
    </Link>
  );
}
