"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "../auth/AuthProvider";

const links = [["/dashboard", "Overview"], ["/create", "Create demo"], ["/demos", "Demo library"], ["/projects", "Projects"], ["/settings", "Settings"]] as const;
export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, ready, logout } = useAuth(); const router = useRouter(); const path = usePathname();
  useEffect(() => { if (ready && !user) router.replace("/login"); }, [ready, user, router]);
  if (!ready || !user) return <main className="appLoading">Opening your studio…</main>;
  return <div className="studio"><aside className="sidebar"><Link href="/dashboard" className="logo">PRODUCTLENS <i>2.0</i></Link><p className="sideLabel">WORKSPACE</p><nav>{links.map(([href, label]) => <Link className={path === href ? "active" : ""} href={href} key={href}>{label}</Link>)}</nav><div className="account"><span>{(user.display_name || user.email).slice(0, 1).toUpperCase()}</span><div><b>{user.display_name || "ProductLens user"}</b><small>{user.email}</small></div><button onClick={() => void logout()} aria-label="Log out">↗</button></div></aside><div className="studioBody"><header className="mobileHead"><Link href="/dashboard" className="logo">PRODUCTLENS <i>2.0</i></Link></header>{children}</div></div>;
}
