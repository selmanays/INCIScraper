import { cn, generateAvatarFallback, getAvatarGradient } from "@/lib/utils";
import {
  Avatar,
  AvatarImage,
  AvatarFallback,
  AvatarIndicator,
  AvatarIndicatorProps
} from "./ui/avatar";

type AvatarProps = {
  image?: string;
  indicator?: AvatarIndicatorProps["variant"];
  fallback?: string;
  className?: string;
  name?: string;
};

export default function UserAvatar({
  image,
  indicator,
  fallback = "AB",
  className,
  name,
}: AvatarProps) {
  const label = name ?? fallback;

  return (
    <Avatar className={cn("h-12 w-12 border", className)}>
      {image ? <AvatarImage src={image} alt="avatar image" /> : null}
      <AvatarIndicator variant={indicator} />
      <AvatarFallback
        className={cn(
          "bg-gradient-to-br text-sm font-medium text-white",
          getAvatarGradient(label)
        )}
      >
        {generateAvatarFallback(label)}
      </AvatarFallback>
    </Avatar>
  );
}
