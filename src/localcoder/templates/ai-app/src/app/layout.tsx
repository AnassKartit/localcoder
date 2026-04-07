import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "{{APP_TITLE}}",
  description: "{{APP_DESCRIPTION}}",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <div className="mx-auto max-w-3xl px-4 py-8">
          {children}
        </div>
      </body>
    </html>
  );
}
