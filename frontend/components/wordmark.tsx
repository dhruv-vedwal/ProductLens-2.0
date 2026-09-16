import Link from "next/link";

export function Wordmark({
  href = "/",
  className = "",
  markOnly = false,
}: {
  href?: string;
  className?: string;
  markOnly?: boolean;
}) {
  return (
    <Link href={href} className={`inline-flex items-center gap-2.5 font-bold tracking-tight ${className}`}>
      <i className="inline-block h-2 w-2 rounded-full bg-record shadow-[0_0_0_3px_color-mix(in_srgb,var(--record)_18%,transparent)]" />
      {markOnly ? <span className="sr-only">ProductLens</span> : "ProductLens"}
    </Link>
  );
}
