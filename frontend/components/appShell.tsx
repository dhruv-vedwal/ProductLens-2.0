"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Bell,
  Clapperboard,
  CreditCard,
  FolderKanban,
  Home,
  LogOut,
  Menu,
  PanelLeft,
  Plus,
  Settings,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ThemeToggle } from "@/components/themeToggle";
import { toast } from "@/components/ui/toaster";
import { Wordmark } from "@/components/wordmark";
import { useAuthStore } from "@/flows/auth/store";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/home", label: "Home", icon: Home },
  { href: "/createDemo", label: "Create demo", icon: Plus },
  { href: "/myDemos", label: "My demos", icon: Clapperboard },
  { href: "/projects", label: "Projects", icon: FolderKanban },
  { href: "/whatsNew", label: "What's New", icon: Sparkles, dividerBefore: true },
  { href: "/pricing", label: "Pricing", icon: CreditCard },
  { href: "/settings", label: "Settings", icon: Settings },
];

const TITLES: Record<string, string> = {
  "/home": "Home",
  "/createDemo": "Create demo",
  "/myDemos": "My demos",
  "/projects": "Projects",
  "/whatsNew": "What's New",
  "/pricing": "Pricing",
  "/settings": "Settings",
  "/timeline": "Run detail",
};

function sectionTitle(pathname: string) {
  if (TITLES[pathname]) return TITLES[pathname];
  const hit = Object.keys(TITLES).find((key) => pathname.startsWith(`${key}/`));
  return hit ? TITLES[hit] : "ProductLens";
}

function NavLinks({
  pathname,
  collapsed,
  onNavigate,
}: {
  pathname: string;
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  return (
    <nav className="flex flex-1 flex-col gap-1">
      {NAV.map((item) => {
        const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
        const Icon = item.icon;
        const link = (
          <Link
            href={item.href}
            data-tour={`nav-${item.href.slice(1)}`}
            onClick={onNavigate}
            className={cn(
              "flex items-center gap-3 rounded-[calc(var(--radius)-4px)] px-3 py-2 text-sm transition-colors",
              collapsed && "justify-center px-0",
              active
                ? "bg-ink text-[var(--bg)]"
                : "text-soft hover:bg-[var(--bg)] hover:text-ink",
            )}
          >
            <Icon className="size-4 shrink-0" strokeWidth={1.75} />
            {!collapsed ? <span>{item.label}</span> : <span className="sr-only">{item.label}</span>}
          </Link>
        );
        return (
          <div key={item.href}>
            {item.dividerBefore ? <Separator className="my-3" /> : null}
            {collapsed ? (
              <Tooltip>
                <TooltipTrigger asChild>{link}</TooltipTrigger>
                <TooltipContent side="right">{item.label}</TooltipContent>
              </Tooltip>
            ) : (
              link
            )}
          </div>
        );
      })}
    </nav>
  );
}

function AccountFooter({
  collapsed,
  name,
  initial,
  onLogout,
}: {
  collapsed: boolean;
  name: string;
  initial: string;
  onLogout: () => void;
}) {
  return (
    <div className="mt-auto space-y-2 border-t border-[var(--line)] pt-4">
      <div className={cn("flex items-center gap-2.5 px-1", collapsed && "justify-center px-0")}>
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-ink text-[11px] font-medium text-[var(--bg)]">
          {initial}
        </span>
        {!collapsed ? (
          <p className="truncate text-xs font-medium text-soft">{name}</p>
        ) : null}
      </div>
      <Button
        type="button"
        variant="ghost"
        className={cn(
          "h-auto w-full justify-start px-2 text-xs text-faint hover:text-ink",
          collapsed && "justify-center px-0",
        )}
        onClick={onLogout}
      >
        <LogOut className="size-3.5" />
        {!collapsed ? "Log out" : <span className="sr-only">Log out</span>}
      </Button>
    </div>
  );
}

function SidebarBody({
  pathname,
  collapsed,
  userLabel,
  initial,
  onLogout,
  onNavigate,
}: {
  pathname: string;
  collapsed: boolean;
  userLabel: string;
  initial: string;
  onLogout: () => void;
  onNavigate?: () => void;
}) {
  return (
    <>
      <Wordmark
        href="/home"
        markOnly={collapsed}
        className={cn("mb-8 px-2 text-[15px]", collapsed && "justify-center px-0")}
      />
      <NavLinks pathname={pathname} collapsed={collapsed} onNavigate={onNavigate} />
      <AccountFooter
        collapsed={collapsed}
        name={userLabel}
        initial={initial}
        onLogout={onLogout}
      />
    </>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  const userLabel = user?.displayName || user?.email || "";
  const initial = (user?.displayName || user?.email || "P").trim().charAt(0).toUpperCase();
  const title = sectionTitle(pathname);

  function onLogout() {
    void logout().then(() => router.push("/login"));
  }

  return (
    <div className="flex min-h-screen">
      <aside
        className={cn(
          "sticky top-0 hidden h-screen shrink-0 flex-col border-r border-[var(--line)] bg-[var(--bg-elev)] py-5 lg:flex",
          collapsed ? "w-16 px-2" : "w-[var(--sidebar-width)] px-4",
        )}
      >
        <SidebarBody
          pathname={pathname}
          collapsed={collapsed}
          userLabel={userLabel}
          initial={initial}
          onLogout={onLogout}
        />
      </aside>

      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="px-4 py-5">
          <SheetHeader className="sr-only">
            <SheetTitle>Navigation</SheetTitle>
          </SheetHeader>
          <SidebarBody
            pathname={pathname}
            collapsed={false}
            userLabel={userLabel}
            initial={initial}
            onLogout={onLogout}
            onNavigate={() => setMobileOpen(false)}
          />
        </SheetContent>
      </Sheet>

      <div className="flex min-w-0 flex-1 flex-col bg-[var(--bg)]">
        <header className="flex items-center justify-between gap-3 border-b border-[var(--line)] bg-[color-mix(in_srgb,var(--bg)_92%,transparent)] px-4 py-3 backdrop-blur-md sm:px-8">
          <div className="flex min-w-0 items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="lg:hidden"
              aria-label="Open menu"
              onClick={() => setMobileOpen(true)}
            >
              <Menu className="size-4" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="hidden lg:inline-flex"
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              onClick={() => setCollapsed((v) => !v)}
            >
              <PanelLeft className="size-4" />
            </Button>
            <p className="truncate text-sm font-medium text-ink">{title}</p>
          </div>
          <div className="flex items-center gap-3">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              data-tour="notifications"
              aria-label="Notifications"
              title="Notifications"
              onClick={() =>
                toast("Coming soon", {
                  description: "Notifications aren’t available yet.",
                })
              }
            >
              <Bell className="size-4" strokeWidth={1.75} />
            </Button>
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1 px-4 py-6 sm:px-8 sm:py-8">{children}</main>
      </div>
    </div>
  );
}

export function AppShellSkeleton() {
  return (
    <div className="flex min-h-screen">
      <aside className="hidden h-screen w-[var(--sidebar-width)] border-r border-[var(--line)] bg-[var(--bg-elev)] px-4 py-5 lg:block">
        <div className="mb-8 h-5 w-28 rounded-md bg-[var(--line)]" />
        <div className="space-y-2">
          <div className="h-9 rounded-md bg-[var(--line)]" />
          <div className="h-9 rounded-md bg-[var(--line)]/70" />
          <div className="h-9 rounded-md bg-[var(--line)]/70" />
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="h-[3.25rem] border-b border-[var(--line)]" />
        <div className="flex-1 px-8 py-8">
          <div className="h-8 w-48 rounded-md bg-[var(--line)]" />
          <div className="mt-4 h-4 w-80 max-w-full rounded-md bg-[var(--line)]/70" />
        </div>
      </div>
    </div>
  );
}
