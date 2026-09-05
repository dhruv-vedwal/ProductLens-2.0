import Link from "next/link";

type StudioHeaderProps = { label: string };

const links = [
  { href: "/", label: "Dashboard" },
  { href: "/create", label: "Create" },
  { href: "/demos", label: "Demos" },
  { href: "/projects", label: "Projects" },
  { href: "/settings", label: "Settings" },
];

export function StudioHeader({ label }: StudioHeaderProps) {
  return <header className="top">
    <Link className="brand" href="/">PRODUCTLENS <span>2.0</span></Link>
    <nav aria-label="Studio navigation">
      {links.map(link => <Link key={link.href} href={link.href}>{link.label}</Link>)}
    </nav>
    <p>{label}</p>
  </header>;
}
