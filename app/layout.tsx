import type { Metadata } from "next";
import { Instrument_Serif, JetBrains_Mono } from "next/font/google";
import "./globals.css";

// A characterful serif against a technical mono: the pairing says
// "research instrument" rather than "SaaS product", which is what this is.
const instrumentSerif = Instrument_Serif({
  variable: "--font-instrument-serif",
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
});

// latin-ext because the lead data is Bosnian - č, ć, š, ž, đ all appear in
// business names, and a subset without them renders tofu in the one place
// accuracy is the entire point.
const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin", "latin-ext"],
});

export const metadata: Metadata = {
  title: "Lead Research Agent",
  description:
    "An AI agent that researches businesses against real sources and records what it could not verify.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${instrumentSerif.variable} ${jetbrainsMono.variable}`}>
        {children}
      </body>
    </html>
  );
}
