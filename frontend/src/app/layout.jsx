// The root layout: the <html> and <body> shared by every page.
//
// A server component (no "use client"): it renders once on the server.
// Everything interactive sits inside <Providers>, a client component.

import "./globals.css";

import { Providers } from "./providers";

export const metadata = {
  title: "Kharcha",
  description: "Upload your bank statements and see where your money goes.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en-IN" className="h-full antialiased">
      <body className="flex min-h-full flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
