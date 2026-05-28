import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Signal Agent — Admin",
  description: "Tune the daily healthcare digest agent.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
