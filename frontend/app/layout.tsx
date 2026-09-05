import type { Metadata } from "next";
import { AuthProvider } from "../components/auth/AuthProvider";

export const metadata: Metadata = {
  title: "ProductLens 2.0",
  description: "Evidence-backed product demo generation",
};

export default function Layout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><AuthProvider>{children}</AuthProvider></body></html>;
}
