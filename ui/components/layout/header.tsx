import { Menu } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import Search from "./search";
import Logo from "./logo";
import { SidebarNavLink } from "./sidebar";
import { page_routes } from "@/lib/routes-config";
import { Fragment } from "react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { cn, generateAvatarFallback, getAvatarGradient } from "@/lib/utils";

export default function Header() {
  return (
    <div className="sticky top-0 z-50 flex flex-col">
      <header className="flex h-14 items-center gap-4 border-b bg-background px-4 lg:h-[60px]">
        <Sheet>
          <SheetTrigger asChild>
            <Button variant="outline" size="icon" className="shrink-0 lg:hidden">
              <Menu className="h-5 w-5" />
              <span className="sr-only">Toggle navigation menu</span>
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="flex flex-col overflow-auto">
            <Logo className="px-0" />
            <nav className="grid gap-2 text-lg font-medium">
              {page_routes.map((route) => (
                <Fragment key={route.title}>
                  <div className="px-2 py-4 font-medium">{route.title}</div>
                  <nav className="*:flex *:items-center *:gap-3 *:rounded-lg *:px-3 *:py-2 *:transition-all hover:*:bg-muted">
                    {route.items.map((item, key) => (
                      <SidebarNavLink key={key} item={item} />
                    ))}
                  </nav>
                </Fragment>
              ))}
            </nav>
            <div className="mt-auto rounded-lg bg-muted p-4 text-sm text-muted-foreground">
              <p className="font-medium text-foreground">Haftalık özet</p>
              <p>Yeni 47 lead oluşturuldu, 12 tanesi müzakere aşamasında.</p>
            </div>
          </SheetContent>
        </Sheet>
        <div className="w-full flex-1">
          <Search />
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="rounded-full focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
            >
              <Avatar className="h-10 w-10">
                <AvatarFallback
                  className={cn(
                    "bg-gradient-to-br text-sm font-medium text-white",
                    getAvatarGradient("Sofia Davis")
                  )}
                >
                  {generateAvatarFallback("Sofia Davis")}
                </AvatarFallback>
              </Avatar>
              <span className="sr-only">Open account menu</span>
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>My Account</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem>Settings</DropdownMenuItem>
            <DropdownMenuItem>Support</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem>Logout</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>
    </div>
  );
}
