import type { Metadata, Viewport } from "next";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";

import { Header } from "@/ui/shell/Header";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Archaeo-Code", template: "%s · Archaeo-Code" },
  description:
    "Code archeology for legacy systems: a knowledge graph of calls, SQL and git history, with answers that cite the lines they come from.",
};

export const viewport: Viewport = {
  themeColor: "#0a0c10",
  colorScheme: "dark",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col">
        <Header />
        <div className="flex min-h-0 flex-1 flex-col">{children}</div>
      </body>
    </html>
  );
}
